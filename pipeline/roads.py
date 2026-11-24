"""Roads and paths from OpenStreetMap, draped exactly onto the terrain mesh.

Each OSM highway centreline is buffered to a carriageway width (OSM `width`/`lanes`
when tagged, otherwise typical Brisbane widths below), layered so that no two road
polygons overlap (major > minor > service > paths), then clipped against every
terrain triangle it touches so the road surface is coplanar with the terrain.
Bridges are built as straight decks between their end points instead of draping.
"""
from __future__ import annotations

import json
import re

import mapbox_earcut as earcut
import numpy as np
import shapely
from shapely.geometry import LineString, MultiLineString, Point, Polygon
from shapely.strtree import STRtree

from buildings import Local, srgb_to_linear
from common import RAW

GROUPS = [
    ("major", {"motorway", "trunk", "primary", "secondary", "tertiary", "motorway_link", "trunk_link",
               "primary_link", "secondary_link", "tertiary_link"}),
    ("minor", {"residential", "unclassified", "living_street", "road", "busway"}),
    ("service", {"service", "track"}),
    ("path", {"footway", "path", "cycleway", "pedestrian", "steps", "bridleway"}),
]
GROUP_OF = {hw: g for g, s in GROUPS for hw in s}
COLOURS = {  # sRGB
    "major": (62, 64, 68), "minor": (74, 76, 80), "service": (88, 90, 94),
    "path": (196, 191, 180), "path_unpaved": (156, 136, 100), "bridge_side": (170, 168, 160),
}
UNPAVED = {"dirt", "ground", "gravel", "grass", "unpaved", "compacted", "fine_gravel", "earth", "mud",
           "sand", "woodchips", "rock", "pebblestone"}
LIFT = 0.03  # m above terrain (plus polygon offset in the viewer)


def _num(v):
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)", str(v)) if v is not None else None
    return float(m.group(1)) if m else None


def road_width(t):
    w = _num(t.get("width"))
    if w and 1.0 <= w <= 30:
        return w
    hw = t["highway"]
    lanes = _num(t.get("lanes"))
    oneway = t.get("oneway") in ("yes", "1", "-1", "true")
    if hw in ("motorway", "trunk", "primary", "secondary"):
        return lanes * 3.4 + (1.0 if oneway else 2.0) if lanes else (7.5 if oneway else 11.0)
    if hw == "tertiary":
        return lanes * 3.3 + (1.0 if oneway else 2.0) if lanes else (6.0 if oneway else 9.5)
    if hw.endswith("_link"):
        return 6.0
    if hw in ("residential", "unclassified", "road", "busway"):
        return 5.5 if oneway else 7.5
    if hw == "living_street":
        return 6.0
    if hw == "service":
        return {"driveway": 3.0, "parking_aisle": 5.5, "alley": 4.0, "drive-through": 4.0}.get(t.get("service"), 4.5)
    if hw == "track":
        return 3.0
    if hw == "footway":
        return {"sidewalk": 1.6, "crossing": 3.0}.get(t.get("footway"), 1.8)
    return {"path": 1.5, "cycleway": 2.5, "pedestrian": 4.0, "steps": 1.8, "bridleway": 2.0}.get(hw, 1.8)


class Mesh:
    def __init__(self):
        self.pos, self.nrm, self.col, self.fid, self.idx = [], [], [], [], []
        self.n = 0

    def add(self, v3, tris, normals, colour_lin, fid):
        self.pos.append(v3)
        self.nrm.append(normals)
        self.col.append(np.repeat(np.asarray(colour_lin)[None], len(v3), 0))
        self.fid.append(np.full(len(v3), fid, np.float32))
        self.idx.append(np.asarray(tris, np.int64) + self.n)
        self.n += len(v3)

    def arrays(self):
        if not self.pos:
            return None
        return dict(pos=np.concatenate(self.pos), nrm=np.concatenate(self.nrm), col=np.concatenate(self.col),
                    fid=np.concatenate(self.fid), idx=np.concatenate(self.idx))


def build(terrain, H):
    local = Local.osm()
    osm = json.loads((RAW / "osm" / "osm.json").read_text())
    square = shapely.box(-H, -H, H, H)
    ways, circles = [], []
    for e in osm["elements"]:
        t = e.get("tags", {})
        if e["type"] == "node" and t.get("highway") in ("turning_circle", "turning_loop"):
            x, n = local(e["lon"], e["lat"])
            circles.append(Point(float(x), float(n)))
        if e["type"] != "way" or t.get("highway") not in GROUP_OF or t.get("area") == "yes":
            continue
        if len(e.get("geometry", [])) < 2:
            continue
        line = LineString(local.ring([(p["lon"], p["lat"]) for p in e["geometry"]]))
        if not line.intersects(square):
            continue
        ways.append({"line": line, "tags": t, "osm_id": f"way/{e['id']}", "group": GROUP_OF[t["highway"]],
                     "width": road_width(t), "bridge": t.get("bridge") not in (None, "no")})

    # Draping set-up: terrain triangles (x, north) and their planes z = A x + B n + C.
    V, T = terrain.vertices, terrain.triangles
    tri_xy = V[T][:, :, :2]
    tri_polys = shapely.polygons(np.concatenate([tri_xy, tri_xy[:, :1]], axis=1))
    tree = STRtree(tri_polys)
    p0, p1, p2 = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
    e1, e2 = p1 - p0, p2 - p0
    nz = np.cross(e1, e2)
    nz /= np.linalg.norm(nz, axis=1, keepdims=True)
    A = -nz[:, 0] / nz[:, 2]
    B = -nz[:, 1] / nz[:, 2]
    C = p0[:, 2] - A * p0[:, 0] - B * p0[:, 1]

    mesh = Mesh()
    feats = []
    taken = Polygon()
    order = {g: i for i, (g, _) in enumerate(GROUPS)}
    ways.sort(key=lambda w: (order[w["group"]], -w["width"], -w["line"].length))
    # Add turning circles (cul-de-sac bulbs) to the nearest minor road.
    minor_lines = [w for w in ways if w["group"] == "minor" and not w["bridge"]]
    bulbs = {}
    for c in circles:
        if minor_lines:
            w = min(minor_lines, key=lambda w: w["line"].distance(c))
            if w["line"].distance(c) < 3:
                bulbs.setdefault(id(w), []).append(c.buffer(8.0, quad_segs=8))

    for w in ways:
        fid = len(feats)
        t = w["tags"]
        group = w["group"]
        colour_key = "path_unpaved" if group == "path" and t.get("surface") in UNPAVED else group
        colour = srgb_to_linear(np.array(COLOURS[colour_key]) / 255.0)
        feats.append({
            "id": fid, "osm_id": w["osm_id"], "name": t.get("name"), "ref": t.get("ref"),
            "highway": t["highway"], "group": group, "width_m": round(w["width"], 1),
            "width_source": "OSM width" if _num(t.get("width")) else ("OSM lanes" if _num(t.get("lanes")) else "typical"),
            "surface": t.get("surface"), "lanes": t.get("lanes"), "maxspeed": t.get("maxspeed"),
            "oneway": t.get("oneway"), "sidewalk": t.get("sidewalk"), "lit": t.get("lit"),
            "bridge": w["bridge"], "length_m": round(w["line"].intersection(square).length, 1),
        })
        if w["bridge"]:
            _bridge(mesh, terrain, w, colour, fid)
            continue
        poly = w["line"].buffer(w["width"] / 2, quad_segs=6)
        for b in bulbs.get(id(w), []):
            poly = poly.union(b)
        poly = poly.intersection(square)
        piece = poly.difference(taken) if not taken.is_empty else poly
        taken = taken.union(poly)
        if piece.is_empty:
            continue
        _drape(mesh, piece, tree, tri_polys, A, B, C, colour, fid)
    print(f"  roads: {len(feats)} OSM highway ways "
          f"({sum(1 for f in feats if f['bridge'])} bridges), {mesh.n} vertices")
    return mesh.arrays(), feats, ways


def _drape(mesh, piece, tree, tri_polys, A, B, C, colour, fid):
    idx = tree.query(piece, predicate="intersects")
    if len(idx) == 0:
        return
    parts = shapely.intersection(tri_polys[idx], piece)
    verts, tris, tri_of_vert = [], [], []
    nv = 0
    for k, g in zip(idx, parts):
        for p in getattr(g, "geoms", [g]):
            if p.geom_type != "Polygon" or p.is_empty or p.area < 1e-4:
                continue
            rings = [np.asarray(p.exterior.coords)[:-1]] + [np.asarray(r.coords)[:-1] for r in p.interiors]
            v = np.concatenate(rings)
            ends = np.cumsum([len(r) for r in rings]).astype(np.uint32)
            tr = earcut.triangulate_float64(v, ends).reshape(-1, 3).astype(np.int64)
            if len(tr) == 0:
                continue
            a, b, c = v[tr[:, 0]], v[tr[:, 1]], v[tr[:, 2]]
            cr = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
            tr[cr < 0] = tr[cr < 0][:, [0, 2, 1]]
            verts.append(v)
            tris.append(tr + nv)
            tri_of_vert.append(np.full(len(v), k))
            nv += len(v)
    if not verts:
        return
    v = np.concatenate(verts)
    k = np.concatenate(tri_of_vert)
    z = A[k] * v[:, 0] + B[k] * v[:, 1] + C[k] + LIFT
    n = np.column_stack([-A[k], -B[k], np.ones(len(k))])
    n /= np.linalg.norm(n, axis=1, keepdims=True)
    mesh.add(np.column_stack([v, z]), np.concatenate(tris), n, colour, fid)


def _bridge(mesh, terrain, w, colour, fid):
    """Straight deck between the bridge ends (no draping), with side faces."""
    line = w["line"]
    L = line.length
    if L < 1:
        return
    s = np.linspace(0, L, max(2, int(L / 1.0) + 1))
    pts = np.array([line.interpolate(d).coords[0] for d in s])
    z0 = float(terrain.height(*pts[0])) + LIFT
    z1 = float(terrain.height(*pts[-1])) + LIFT
    z = z0 + (z1 - z0) * s / L
    # Keep the deck above the ground it spans (e.g. a creek bank that rises mid-span).
    zg = terrain.height(pts[:, 0], pts[:, 1])
    z = np.maximum(z, zg + 0.15)
    d = np.gradient(pts, axis=0)
    d /= np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1e-9)
    nrm2 = np.column_stack([-d[:, 1], d[:, 0]])
    hw = w["width"] / 2
    left, right = pts + nrm2 * hw, pts - nrm2 * hw
    m = len(pts)
    top = np.concatenate([np.column_stack([left, z]), np.column_stack([right, z])])
    tris = []
    for i in range(m - 1):
        a, b, c, dd = i, i + 1, m + i + 1, m + i
        tris += [[a, dd, c], [a, c, b]]
    tris = np.array(tris)
    # orient upward
    va, vb, vc = top[tris[:, 0]], top[tris[:, 1]], top[tris[:, 2]]
    up = np.cross(vb - va, vc - va)[:, 2]
    tris[up < 0] = tris[up < 0][:, [0, 2, 1]]
    mesh.add(top, tris, np.repeat([[0, 0, 1.0]], len(top), 0), colour, fid)
    side_col = srgb_to_linear(np.array(COLOURS["bridge_side"]) / 255.0)
    for edge, sign in ((left, 1), (right, -1)):
        for i in range(m - 1):
            q = np.array([[*edge[i], z[i]], [*edge[i + 1], z[i + 1]],
                          [*edge[i + 1], z[i + 1] - 0.6], [*edge[i], z[i] - 0.6]])
            out = np.array([*(nrm2[i] * sign), 0.0])
            tri = np.array([[0, 1, 2], [0, 2, 3]])
            nv = np.cross(q[1] - q[0], q[2] - q[0])
            if np.dot(nv, out) < 0:
                tri = tri[:, [0, 2, 1]]
            mesh.add(q, tri, np.repeat(out[None], 4, 0), side_col, fid)


def street_labels(ways, terrain, H):
    """One label every ~220 m along each named street (merged by name)."""
    square = shapely.box(-H + 20, -H + 20, H - 20, H - 20)
    by_name = {}
    for w in ways:
        name = w["tags"].get("name")
        if name and w["group"] in ("major", "minor"):
            by_name.setdefault(name, []).append(w["line"])
    labels = []
    for name, lines in by_name.items():
        merged = shapely.line_merge(MultiLineString(lines)).intersection(square)
        for part in getattr(merged, "geoms", [merged]):
            if part.geom_type != "LineString" or part.length < 35:
                continue
            n = max(1, int(part.length // 220))
            for i in range(n):
                dpos = part.length * (i + 0.5) / n
                p = np.array(part.interpolate(dpos).coords[0])
                a = np.array(part.interpolate(max(dpos - 6, 0)).coords[0])
                b = np.array(part.interpolate(min(dpos + 6, part.length)).coords[0])
                ang = float(np.arctan2(b[1] - a[1], b[0] - a[0]))
                labels.append({"text": name, "x": round(float(p[0]), 2), "n": round(float(p[1]), 2),
                               "z": round(float(terrain.height(*p)), 2), "angle": round(ang, 4)})
    return labels
