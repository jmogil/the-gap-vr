"""Streetscape: cadastral lots and fences, kerbs, poles/street lights/wires, and street furniture.

  * lots            Queensland DCDB parcels (CC BY 4.0) -> lot info + boundary lines
  * fences          1.8 m fences along boundaries shared by neighbouring lots (inferred, not
                    surveyed), cut where buildings straddle the line; none on street frontages
  * kerbs           0.15 m concrete kerb along every carriageway edge, gapped at OSM driveways
  * poles / wires   power poles detected in the LiDAR's unclassified returns (power lines and
                    pole tops), wires strung between poles the LiDAR shows are connected, and a
                    street light assumed on every pole within reach of a street
  * signs           OSM traffic signals, give-way/stop signs, bus stops and benches, plus a
                    street-name sign at each intersection of named streets
"""
from __future__ import annotations

import json
import math

import numpy as np
import shapely
from scipy import ndimage
from scipy.spatial import cKDTree
from shapely.geometry import LineString, MultiLineString, Point, Polygon, shape
from shapely.geometry.polygon import orient
from shapely.strtree import STRtree

from buildings import Local, srgb_to_linear
from common import CONFIG, RAW, ROOT

RES = 0.5
FENCE_H = 1.8
KERB_H, KERB_W = 0.15, 0.3


# ----------------------------------------------------------------------------- helpers
def _lines(geom):
    out = []
    for g in getattr(geom, "geoms", [geom]):
        if g.is_empty:
            continue
        if g.geom_type == "LineString":
            out.append(g)
        elif g.geom_type in ("MultiLineString", "GeometryCollection"):
            out.extend(_lines(g))
    return out


def _polys(geom):
    return [g for g in getattr(geom, "geoms", [geom]) if g.geom_type == "Polygon" and not g.is_empty]


def _densify(line, step):
    n = max(2, int(math.ceil(line.length / step)) + 1)
    d = np.linspace(0, line.length, n)
    return np.array([line.interpolate(v).coords[0] for v in d]), d


class Strips:
    """Collects vertical/horizontal strips into one indexed mesh (local x, north, z)."""

    def __init__(self):
        self.pos, self.nrm, self.uv, self.col, self.idx, self.n = [], [], [], [], [], 0

    def add_strip(self, a, b, nrm, colour, uv=None):
        """a, b: (k,3) rows of vertices; quads between consecutive pairs."""
        k = len(a)
        if k < 2:
            return
        v = np.concatenate([a, b])
        tri = [[i, i + 1, k + i + 1] for i in range(k - 1)] + [[i, k + i + 1, k + i] for i in range(k - 1)]
        self.pos.append(v)
        self.nrm.append(np.concatenate([nrm, nrm]) if np.ndim(nrm) == 2 else np.repeat([nrm], 2 * k, 0))
        self.col.append(np.repeat([colour], 2 * k, 0))
        self.uv.append(uv if uv is not None else np.zeros((2 * k, 2)))
        self.idx.append(np.asarray(tri) + self.n)
        self.n += 2 * k

    def arrays(self):
        if not self.pos:
            return None
        return dict(pos=np.concatenate(self.pos), nrm=np.concatenate(self.nrm), uv=np.concatenate(self.uv),
                    col=np.concatenate(self.col), idx=np.concatenate(self.idx),
                    fid=np.zeros(self.n, np.float32))


# ----------------------------------------------------------------------------- lots + fences
def lots_and_fences(terrain, H, building_polys):
    E0, N0 = CONFIG["anchor"]["easting"], CONFIG["anchor"]["northing"]
    square = shapely.box(-H, -H, H, H)
    feats = json.loads((RAW / "cadastre" / "parcels.geojson").read_text())["features"]
    lots = []
    for f in feats:
        p = f["properties"]
        if p.get("cover_typ") == "Easement" or p.get("parcel_typ") in ("Easement", "Unlinked parcel or inter"):
            continue
        g = shapely.affinity.translate(shape(f["geometry"]), -E0, -N0).buffer(0).intersection(square)
        if g.is_empty or g.area < 1:
            continue
        lots.append({"geom": g, "props": p})
    res = [l for l in lots if l["props"].get("parcel_typ") == "Lot Type Parcel"]
    # fence where a lot meets another lot or reserve (not a road or watercourse)
    fenceable = [l for l in lots if l["props"].get("parcel_typ") == "Lot Type Parcel" or l["props"].get("tenure") == "Reserve"]
    geoms = [l["geom"] for l in fenceable]
    tree = STRtree(geoms)
    shared = []
    for i, g in enumerate(geoms):
        for j in tree.query(g, predicate="intersects"):
            if j <= i:
                continue
            # both residential-sized? (no fences between two big parks / school sites)
            ai, aj = g.area, geoms[j].area
            if min(ai, aj) > 4000:
                continue
            line = shapely.intersection(g.boundary, geoms[j].boundary)
            shared += [l for l in _lines(line) if l.length > 0.5]
    fence_lines = shapely.line_merge(shapely.union_all(shared)) if shared else MultiLineString()
    blockers = shapely.union_all([b.buffer(0.35) for b in building_polys])
    fence_lines = fence_lines.difference(blockers).intersection(square.buffer(-0.5))
    fences = Strips()
    colour = srgb_to_linear(np.array([138, 116, 92]) / 255.0)     # weathered timber palings
    total = 0.0
    for ln in _lines(fence_lines):
        if ln.length < 1.0:
            continue
        pts, d = _densify(ln, 2.0)
        g = terrain.height(pts[:, 0], pts[:, 1])
        a = np.column_stack([pts, g - 0.05])
        b = np.column_stack([pts, g + FENCE_H])
        tang = np.gradient(pts, axis=0)
        tang /= np.maximum(np.linalg.norm(tang, axis=1, keepdims=True), 1e-9)
        nrm = np.column_stack([-tang[:, 1], tang[:, 0], np.zeros(len(pts))])
        uv = np.concatenate([np.column_stack([d, np.zeros(len(d))]), np.column_stack([d, np.ones(len(d))])])
        fences.add_strip(a, b, nrm, colour, uv)
        total += ln.length
    # lot records for the viewer (three.js x, z) and boundary polylines
    lot_out, bounds = [], []
    for l in lots:
        p = l["props"]
        for poly in _polys(l["geom"]):
            ring = np.asarray(orient(poly, 1.0).exterior.coords)[:-1]
            lot_out.append({"lotplan": p.get("lotplan"), "lot": p.get("lot"), "plan": p.get("plan"),
                            "tenure": p.get("tenure"), "type": p.get("parcel_typ"),
                            "area_m2": p.get("lot_area") or round(poly.area),
                            "name": p.get("feat_name") or p.get("alias_name"),
                            "accuracy": p.get("acc_code"),
                            "ring": [[round(float(x), 2), round(float(-n), 2)] for x, n in ring]})
    for ln in _lines(shapely.line_merge(shapely.union_all([l["geom"].boundary for l in res]))):
        pts, _ = _densify(ln, 2.0)
        bounds.append([round(float(v), 2) for x, n in pts for v in (x, -n)])
    print(f"  lots: {len(lot_out)} parcels ({len(res)} lots); fences {total / 1000:.1f} km")
    return lot_out, bounds, fences.arrays()


# ----------------------------------------------------------------------------- kerbs
def kerbs(terrain, H, carriageway, driveways, lift=0.03):
    square = shapely.box(-H + 0.5, -H + 0.5, H - 0.5, H - 0.5)
    rings = []
    for poly in _polys(carriageway):
        poly = orient(poly, 1.0)                       # road surface on the left of every ring
        rings += [poly.exterior] + list(poly.interiors)
    lines = MultiLineString([LineString(r.coords) for r in rings])
    if not driveways.is_empty:
        lines = lines.difference(driveways.buffer(0.4))   # crossovers at driveways
    lines = lines.intersection(square)
    out = Strips()
    colour = srgb_to_linear(np.array([186, 183, 176]) / 255.0)
    total = 0.0
    for ln in _lines(lines):
        if ln.length < 0.8:
            continue
        pts, _ = _densify(ln, 2.0)
        tang = np.gradient(pts, axis=0)
        tang /= np.maximum(np.linalg.norm(tang, axis=1, keepdims=True), 1e-9)
        outw = np.column_stack([tang[:, 1], -tang[:, 0]])            # right of travel = away from road
        q = pts + outw * KERB_W
        gp = terrain.height(pts[:, 0], pts[:, 1])
        gq = terrain.height(q[:, 0], q[:, 1])
        road = np.column_stack([pts, gp + lift])
        top_p = np.column_stack([pts, gp + KERB_H])
        top_q = np.column_stack([q, np.maximum(gq, gp) + KERB_H])
        n_front = np.column_stack([-outw, np.zeros(len(pts))])
        out.add_strip(road, top_p, n_front, colour)                    # face towards the road
        out.add_strip(top_p, top_q, np.repeat([[0, 0, 1.0]], len(pts), 0), colour)
        total += ln.length
    print(f"  kerbs: {total / 1000:.1f} km")
    return out.arrays()


# ----------------------------------------------------------------------------- poles, lights, wires
def poles_and_wires(terrain, H, road_lines, building_polys):
    path = ROOT / "data" / "derived" / "lidar_unclassified.npz"
    if not path.exists():
        return [], []
    xyz = np.load(path)["xyz"].astype(np.float64)
    hag = xyz[:, 2] - terrain.height(xyz[:, 0], xyz[:, 1])
    m = (hag > 2.0) & (hag < 25)
    x, n_, h = xyz[m, 0], xyz[m, 1], hag[m]
    nn = int(2 * H / RES)
    c = np.clip(((x + H) / RES).astype(int), 0, nn - 1)
    r = np.clip(((H - n_) / RES).astype(int), 0, nn - 1)
    cnt = np.zeros((nn, nn), np.float32)
    hmax = np.zeros((nn, nn), np.float32)
    hmin = np.full((nn, nn), 99.0, np.float32)
    np.add.at(cnt, (r, c), 1)
    np.maximum.at(hmax, (r, c), h)
    np.minimum.at(hmin, (r, c), h)
    score = ndimage.uniform_filter(cnt, 3) * 9                  # points within ~0.75 m
    span = ndimage.maximum_filter(hmax, 3) - ndimage.minimum_filter(hmin, 3)
    peak = (score == ndimage.maximum_filter(score, 25)) & (score >= 14) & (span >= 2.5)
    pr, pc = np.nonzero(peak)
    bindex = STRtree(building_polys) if building_polys else None
    rtree = STRtree(road_lines)
    poles = []
    for rr, cc in zip(pr, pc):
        px, pn = -H + (cc + 0.5) * RES, H - (rr + 0.5) * RES
        pt = Point(px, pn)
        if bindex is not None and len(bindex.query(pt, predicate="intersects")):
            continue
        road = road_lines[rtree.nearest(pt)]
        dist = road.distance(pt)
        if dist > 25:
            continue
        top = float(ndimage.maximum_filter(hmax, 5)[rr, cc])
        q = np.array(road.interpolate(road.project(pt)).coords[0])
        arm = math.atan2(q[0] - px, q[1] - pn)                  # bearing (rad, from north) to the street
        poles.append({"x": px, "n": pn, "h": float(np.clip(top + 0.3, 6.0, 14.0)), "road_dist": dist,
                      "arm": arm, "light": False})
    # Street lights are not in any open dataset here: assume one on street-side poles, ~30 m apart.
    lit = []
    for p in sorted(poles, key=lambda p: p["road_dist"]):
        if p["road_dist"] < 12 and all(math.hypot(p["x"] - q["x"], p["n"] - q["n"]) > 30 for q in lit):
            p["light"] = True
            lit.append(p)
    # wires: connect poles where the LiDAR has line returns along the span
    wpts = np.column_stack([x[(h > 4) & (h < 16)], n_[(h > 4) & (h < 16)]])
    wtree = cKDTree(wpts) if len(wpts) else None
    P = np.array([[p["x"], p["n"]] for p in poles]) if poles else np.zeros((0, 2))
    ptree = cKDTree(P) if len(P) else None
    edges = []
    if ptree is not None and wtree is not None:
        for i, j in sorted(ptree.query_pairs(75)):
            a, b = P[i], P[j]
            L = np.linalg.norm(b - a)
            if L < 8:
                continue
            t = np.linspace(0.15, 0.85, 12)
            s = a + (b - a) * t[:, None]
            support = np.array([len(wtree.query_ball_point(p, 2.0)) > 0 for p in s])
            if support.mean() < 0.5:
                continue
            # skip spans that pass another pole (keep the chain a-b-c, not a-c)
            mid = [k for k in ptree.query_ball_point((a + b) / 2, L / 2 + 1) if k not in (i, j)]
            if any(LineString([a, b]).distance(Point(P[k])) < 2.5 for k in mid):
                continue
            edges.append((i, j))
    # crossarm orientation: perpendicular to the mean wire direction at each pole
    for k, p in enumerate(poles):
        dirs = [P[j] - P[k] if i == k else P[i] - P[k] for i, j in edges if k in (i, j)]
        if dirs:
            v = sum(d / np.linalg.norm(d) * (1 if d @ dirs[0] >= 0 else -1) for d in dirs)
            p["cross"] = math.atan2(v[0], v[1]) + math.pi / 2
        else:
            p["cross"] = p["arm"]
    wires = []
    for i, j in edges:
        a, b = poles[i], poles[j]
        za = terrain.height(a["x"], a["n"]) + a["h"] - 0.4
        zb = terrain.height(b["x"], b["n"]) + b["h"] - 0.4
        wires.append([a["x"], a["n"], float(za), b["x"], b["n"], float(zb)])
    print(f"  poles: {len(poles)} from LiDAR ({sum(p['light'] for p in poles)} with street lights), "
          f"{len(wires)} wire spans")
    return poles, wires


# ----------------------------------------------------------------------------- OSM furniture
def furniture(terrain, H, ways):
    local = Local.osm()
    osm = json.loads((RAW / "osm" / "osm.json").read_text())
    square = shapely.box(-H + 1, -H + 1, H - 1, H - 1)
    roads = [w for w in ways if w["group"] in ("major", "minor") and not w["bridge"]]
    rtree = STRtree([w["line"] for w in roads])

    def road_at(pt):
        w = roads[rtree.nearest(pt)]
        s = w["line"].project(pt)
        a = np.array(w["line"].interpolate(max(s - 2, 0)).coords[0])
        b = np.array(w["line"].interpolate(min(s + 2, w["line"].length)).coords[0])
        d = b - a
        d /= max(np.linalg.norm(d), 1e-9)
        return w, s, d

    def left(d):
        return np.array([-d[1], d[0]])

    def bearing(d):
        return math.atan2(d[0], d[1])

    signals, signs, stops, benches = [], [], [], []
    for e in osm["elements"]:
        if e["type"] != "node":
            continue
        t = e.get("tags", {})
        kind = t.get("highway") or t.get("amenity")
        if kind not in ("traffic_signals", "give_way", "stop", "bus_stop", "bench"):
            continue
        x, nn = local(e["lon"], e["lat"])
        pt = Point(float(x), float(nn))
        if not square.contains(pt):
            continue
        w, s, d = road_at(pt)
        off = w["width"] / 2 + 0.7
        p = np.array([pt.x, pt.y])
        if kind == "traffic_signals":
            for side in (1, -1):                              # a signal pole on each side, facing traffic
                q = p + left(d * side) * off
                signals.append({"x": q[0], "n": q[1], "yaw": bearing(-d * side), "osm": e["id"]})
        elif kind in ("give_way", "stop"):
            # traffic approaches the nearer end of the way (the intersection)
            to_end = 1 if (w["line"].length - s) < s else -1
            q = p + left(d * to_end) * off
            signs.append({"type": kind, "x": q[0], "n": q[1], "yaw": bearing(-d * to_end), "osm": e["id"]})
        elif kind == "bus_stop":
            dist = w["line"].distance(pt)
            if dist < w["width"] / 2 + 0.3:                   # on the road: move to the kerb
                side = 1 if (np.array([pt.x, pt.y]) - np.array(w["line"].interpolate(s).coords[0])) @ left(d) >= 0 else -1
                p = np.array(w["line"].interpolate(s).coords[0]) + left(d) * side * (w["width"] / 2 + 0.8)
            stops.append({"x": p[0], "n": p[1], "yaw": bearing(d), "osm": e["id"], "name": t.get("name")})
        else:
            q = np.array(w["line"].interpolate(s).coords[0])
            benches.append({"x": p[0], "n": p[1], "yaw": bearing(q - p), "osm": e["id"]})
    # street-name signs where differently named streets meet
    names = {}
    for e in osm["elements"]:
        t = e.get("tags", {})
        if e["type"] == "way" and t.get("name") and t.get("highway") in (
                "primary", "secondary", "tertiary", "residential", "unclassified", "living_street"):
            g = e["geometry"]
            for k, p in enumerate(g):
                nb = g[k + 1] if k + 1 < len(g) else g[k - 1]
                names.setdefault((round(p["lon"], 7), round(p["lat"], 7)), []).append(
                    (t["name"], (nb["lon"], nb["lat"]), e["id"]))
    width = {w["osm_id"]: w["width"] for w in ways}
    seen, name_signs = [], []
    for (lon, lat), refs in names.items():
        uniq = {}
        for nm, nb, wid in refs:
            uniq.setdefault(nm, (nb, wid))
        if len(uniq) < 2:
            continue
        x, nn = local(lon, lat)
        c = np.array([float(x), float(nn)])
        if not square.contains(Point(*c)) or any(np.linalg.norm(c - s) < 18 and set(uniq) == ns for s, ns in seen):
            continue
        seen.append((c, set(uniq)))
        dirs = []
        for nm, (nb, wid) in uniq.items():
            bx, bn = local(*nb)
            d = np.array([float(bx), float(bn)]) - c
            dirs.append((nm, d / max(np.linalg.norm(d), 1e-9), width.get(f"way/{wid}", 7.5)))
        dirs.sort(key=lambda v: math.atan2(v[1][0], v[1][1]))
        a, b = dirs[0][1], dirs[1][1]
        bis = a + b
        bis = bis / np.linalg.norm(bis) if np.linalg.norm(bis) > 1e-6 else left(a)
        off = max(v[2] for v in dirs) / 2 + 1.6
        q = c + bis * off / max(math.sin(math.acos(np.clip(a @ b, -1, 1)) / 2), 0.35)
        name_signs.append({"x": q[0], "n": q[1], "blades": [[nm, bearing(d)] for nm, d, _ in dirs[:2]]})
    print(f"  furniture: {len(signals)} signal poles, {len(signs)} give-way/stop signs, {len(stops)} bus stops, "
          f"{len(benches)} benches, {len(name_signs)} street-name signs")
    return signals, signs, stops, benches, name_signs
