"""Buildings: conflate OSM + Microsoft footprints, assign heights, build walls and hipped roofs.

Footprint sources (in priority order)
  1. OpenStreetMap building ways/multipolygons (hand-mapped, with tags and addresses)
  2. Microsoft Global ML Building Footprints, only where no OSM building overlaps

Height sources (in priority order; recorded per building as `height_source`)
  1. OSM `height` tag                       -> total height
  2. OSM `building:levels`                  -> eave = 3.0 m per level (+ plinth)
  3. Microsoft ML height estimate           -> total height (imagery-derived, approx. +/-2 m)
  4. default by building type               -> flagged as "default (no data)"
  (LiDAR-derived heights can be slotted in ahead of these later.)
"""
from __future__ import annotations

import json
import math
import re

import mapbox_earcut as earcut
import numpy as np
import shapely
from PIL import Image, ImageDraw
from pyproj import Transformer
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.geometry.polygon import orient
from shapely.strtree import STRtree

from common import CONFIG, RAW
from roofs import _areal, skeleton_faces

G = 1e-3  # overlay grid (m)

KIND = {
    "house": "house", "detached": "house", "semidetached_house": "house", "residential": "house",
    "terrace": "house", "bungalow": "house", "apartments": "civic", "dormitory": "civic",
    "garage": "outbuilding", "garages": "outbuilding", "shed": "outbuilding", "hut": "outbuilding",
    "greenhouse": "outbuilding", "carport": "canopy", "roof": "canopy",
}
ROOF = {  # pitch (deg), max rise (m), min eave (m), default total height (m)
    "house": (22.5, 3.2, 2.5, 5.5),
    "civic": (12.0, 2.4, 3.0, 7.0),
    "outbuilding": (12.0, 1.0, 2.1, 3.0),
    "canopy": (8.0, 0.6, 2.2, 2.9),
}
WALL_SRGB = np.array([231, 226, 215]) / 255.0   # neutral warm white (not data-derived)
POST_SRGB = np.array([200, 198, 192]) / 255.0
ROOF_FALLBACK_SRGB = np.array([168, 164, 156]) / 255.0


def srgb_to_linear(c):
    c = np.asarray(c, dtype=np.float64)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _num(v):
    if v is None:
        return None
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)", str(v))
    return float(m.group(1)) if m else None


class Local:
    """WGS84 lon/lat -> local (x east, north) metres relative to the anchor.

    `offset` (east, north) corrects sources that are systematically misaligned; OSM here
    was traced from GDA94-era imagery and sits ~1.5 m SW of the LiDAR (see config.json)."""

    def __init__(self, offset=(0.0, 0.0)):
        self.t = Transformer.from_crs("EPSG:4326", "EPSG:7856", always_xy=True)
        self.E0 = CONFIG["anchor"]["easting"] - float(offset[0])
        self.N0 = CONFIG["anchor"]["northing"] - float(offset[1])

    @classmethod
    def osm(cls):
        return cls(CONFIG.get("osm_offset_m", {}).get("east_north", (0.0, 0.0)))

    def __call__(self, lon, lat):
        x, y = self.t.transform(lon, lat)
        return np.asarray(x) - self.E0, np.asarray(y) - self.N0

    def ring(self, pts):
        a = np.asarray(pts, dtype=np.float64)
        x, n = self(a[:, 0], a[:, 1])
        return np.column_stack([x, n])


def load_footprints(local: Local, H: float, local_ms: Local = None):
    """Return list of dicts with polygon (local coords) + source attributes.
    `local` converts OSM coordinates, `local_ms` the Microsoft footprints."""
    local_ms = local_ms or Local()
    inside = shapely.box(-H + 1, -H + 1, H - 1, H - 1)
    osm = json.loads((RAW / "osm" / "osm.json").read_text())
    out, addr_pts = [], []
    for e in osm["elements"]:
        tags = e.get("tags", {})
        if e["type"] == "node" and "addr:housenumber" in tags:
            x, n = local(e["lon"], e["lat"])
            addr_pts.append((float(x), float(n), f"{tags['addr:housenumber']} {tags.get('addr:street', '')}".strip()))
        if "building" not in tags or tags.get("building") in ("no", "construction", "demolished"):
            continue
        poly = None
        if e["type"] == "way" and len(e.get("geometry", [])) >= 4:
            poly = Polygon(local.ring([(p["lon"], p["lat"]) for p in e["geometry"]]))
        elif e["type"] == "relation":
            lines = [shapely.LineString(local.ring([(p["lon"], p["lat"]) for p in m["geometry"] if p]))
                     for m in e.get("members", []) if m.get("role") == "outer" and m.get("geometry")]
            polys = list(shapely.polygonize(lines).geoms) if lines else []
            poly = max(polys, key=lambda p: p.area) if polys else None
        if poly is None or not poly.is_valid or poly.area < 4:
            continue
        out.append({"poly": poly, "src": "osm", "osm_id": f"{e['type']}/{e['id']}", "tags": tags})

    ms = json.loads((RAW / "msbuildings" / "msbuildings.geojson").read_text())
    osm_tree = STRtree([b["poly"] for b in out])
    n_osm = len(out)
    ms_heights = {}
    for f in ms["features"]:
        g = shape(f["geometry"])
        if g.geom_type != "Polygon":
            continue
        poly = Polygon(local_ms.ring(g.exterior.coords))
        if not poly.is_valid:
            poly = poly.buffer(0)
            if poly.geom_type != "Polygon":
                continue
        h = f["properties"].get("height", -1)
        h = h if h is not None and h > 0 else None
        hits = osm_tree.query(poly, predicate="intersects")
        best, best_ov, total_ov = None, 0.0, 0.0
        for i in hits:
            ov = out[i]["poly"].intersection(poly).area
            total_ov += ov
            frac = ov / out[i]["poly"].area
            if ov > best_ov and (frac > 0.3 or ov / poly.area > 0.3):
                best, best_ov = i, ov
        if best is not None or total_ov > 0.2 * poly.area:
            if best is not None and h is not None and best_ov > ms_heights.get(best, (0, 0))[1]:
                ms_heights[best] = (h, best_ov)
            continue
        out.append({"poly": poly, "src": "ms", "osm_id": None, "tags": {}, "ms_height": h})
    for i, (h, _) in ms_heights.items():
        out[i]["ms_height"] = h

    kept = [b for b in out if inside.contains(b["poly"])]
    print(f"  footprints: {n_osm} OSM + {len(out) - n_osm} Microsoft (gap fill); "
          f"{len(kept)} fully inside the model square")
    return kept, addr_pts


def assign_addresses(blds, addr_pts):
    polys = [b["poly"] for b in blds]
    tree = STRtree(polys)
    for b in blds:
        b["addresses"] = []
        t = b["tags"]
        if "addr:housenumber" in t:
            b["addresses"].append(f"{t['addr:housenumber']} {t.get('addr:street', '')}".strip())
    for x, n, label in addr_pts:
        p = shapely.Point(x, n)
        hit = tree.query(p, predicate="intersects")
        if len(hit):
            i = int(hit[0])
        else:  # nearest building within 15 m, preferring main buildings over sheds
            near = tree.query(p.buffer(15), predicate="intersects")
            cands = [(polys[j].distance(p) - (3 if polys[j].area > 60 else 0), j) for j in near]
            if not cands:
                continue
            i = int(min(cands)[1])
        if label not in blds[i]["addresses"]:
            blds[i]["addresses"].append(label)


class OrthoSampler:
    def __init__(self, H):
        px = CONFIG["imagery"]["size_px"]
        self.img = Image.open(RAW / "imagery" / f"ortho_{px}.jpg").convert("RGB")
        self.arr = np.asarray(self.img)
        self.px, self.H = px, H

    def roof_colour(self, poly):
        s = self.px / (2 * self.H)
        inner = poly.buffer(-0.6)
        if inner.is_empty or inner.geom_type != "Polygon" or inner.area < 0.3 * poly.area:
            inner = poly
        ring = np.asarray(inner.exterior.coords)
        cols = (ring[:, 0] + self.H) * s
        rows = (self.H - ring[:, 1]) * s
        c0, c1 = int(max(cols.min() - 1, 0)), int(min(cols.max() + 2, self.px))
        r0, r1 = int(max(rows.min() - 1, 0)), int(min(rows.max() + 2, self.px))
        if c1 - c0 < 2 or r1 - r0 < 2:
            return None
        mask = Image.new("L", (c1 - c0, r1 - r0), 0)
        ImageDraw.Draw(mask).polygon(list(zip(cols - c0, rows - r0)), fill=1)
        m = np.asarray(mask, dtype=bool)
        pix = self.arr[r0:r1, c0:c1][m].astype(np.int32)
        if len(pix) < 20:
            return None
        r, g, b = pix[:, 0], pix[:, 1], pix[:, 2]
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        veg = (g > r + 6) & (g > b + 2)          # tree canopy over the roof
        keep = ~veg & (lum > 35)                  # drop vegetation and deep shadow
        if keep.sum() < 0.25 * len(pix):
            return None
        return np.median(pix[keep], axis=0) / 255.0

    def crown_colour(self, x, n, radius):
        """Median canopy colour (sRGB 0-1) in a disc around a treetop, or None."""
        s = self.px / (2 * self.H)
        cx, cy, rr = (x + self.H) * s, (self.H - n) * s, max(radius * s, 2)
        c0, c1 = int(max(cx - rr, 0)), int(min(cx + rr + 1, self.px))
        r0, r1 = int(max(cy - rr, 0)), int(min(cy + rr + 1, self.px))
        if c1 - c0 < 2 or r1 - r0 < 2:
            return None
        yy, xx = np.mgrid[r0:r1, c0:c1]
        pix = self.arr[r0:r1, c0:c1][(xx - cx) ** 2 + (yy - cy) ** 2 <= rr * rr].astype(np.int32)
        lum = 0.299 * pix[:, 0] + 0.587 * pix[:, 1] + 0.114 * pix[:, 2]
        pix = pix[(lum > 25) & (lum < 200)]
        return None if len(pix) < 6 else np.median(pix, axis=0) / 255.0


def _height_info(b, kind, skel_h, lidar=None):
    pitch, max_rise, min_eave, default_total = ROOF[kind]
    t = b["tags"]
    slope = math.tan(math.radians(pitch))
    cap_d = max_rise / slope
    rise = slope * min(skel_h, cap_d)
    levels = _num(t.get("building:levels"))
    if lidar is not None:
        # Measured: ridge and eave straight from the 2019 LiDAR; the hip roof is fitted to them.
        total, eave = max(lidar["total"], 2.0), max(min(lidar["eave"], lidar["total"]), 1.8)
        rise = total - eave
        if rise < 0.5 or skel_h <= 0.2:
            return dict(total=total, eave=total, rise=0.0, slope=0.0, cap_d=0.0, levels=levels,
                        src="LiDAR 2019 (measured)")
        slope = min(rise / skel_h, math.tan(math.radians(38)))
        return dict(total=total, eave=eave, rise=rise, slope=slope, cap_d=rise / slope, levels=levels,
                    src="LiDAR 2019 (measured)")
    if _num(t.get("height")):
        total, src = _num(t.get("height")), "OSM height tag"
    elif levels:
        eave = levels * 3.0 + (0.4 if kind == "house" else 0.2)
        total, src = eave + rise, "OSM building:levels"
    elif b.get("ms_height"):
        total, src = float(b["ms_height"]), "Microsoft ML estimate"
    else:
        total, src = default_total, "default (no data)"
    total = max(total, min_eave + 0.2)
    eave = total - rise
    if eave < min_eave:  # too low for this pitch: flatten the roof instead of sinking the eave
        rise = max(total - min_eave, 0.0)
        eave = total - rise
        slope = rise / min(skel_h, cap_d) if rise > 0.05 else 0.0
    return dict(total=total, eave=eave, rise=rise, slope=slope, cap_d=cap_d, levels=levels, src=src)


class MeshBuilder:
    def __init__(self):
        self.pos, self.nrm, self.col, self.fid, self.idx = [], [], [], [], []
        self.n = 0

    def add(self, verts3, tris, colour_lin, fid, normal=None):
        """verts3: (k,3) in local (x, north, z_abs); tris: (t,3) indices into verts3."""
        verts3 = np.asarray(verts3, dtype=np.float64)
        tris = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
        if len(tris) == 0:
            return
        if normal is None:  # flat normal from first non-degenerate triangle
            for t in tris:
                a, b, c = verts3[t]
                nv = np.cross(b - a, c - a)
                if np.linalg.norm(nv) > 1e-9:
                    normal = nv / np.linalg.norm(nv)
                    break
            else:
                return
        self.pos.append(verts3)
        self.nrm.append(np.repeat(np.asarray(normal)[None, :], len(verts3), axis=0))
        self.col.append(np.repeat(np.asarray(colour_lin)[None, :], len(verts3), axis=0))
        self.fid.append(np.full(len(verts3), fid, dtype=np.float32))
        self.idx.append(tris + self.n)
        self.n += len(verts3)

    def arrays(self):
        if not self.pos:
            return None
        return dict(pos=np.concatenate(self.pos), nrm=np.concatenate(self.nrm),
                    col=np.concatenate(self.col), fid=np.concatenate(self.fid),
                    idx=np.concatenate(self.idx))


def _triangulate(poly):
    rings = [np.asarray(poly.exterior.coords)[:-1]] + [np.asarray(r.coords)[:-1] for r in poly.interiors]
    verts = np.concatenate(rings)
    ends = np.cumsum([len(r) for r in rings]).astype(np.uint32)
    tris = earcut.triangulate_float64(verts, ends).reshape(-1, 3)
    return verts, tris


def _up_tris(verts2, tris):
    """Ensure counter-clockwise (upward-facing) winding in the x/north plane."""
    a, b, c = verts2[tris[:, 0]], verts2[tris[:, 1]], verts2[tris[:, 2]]
    cross = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    tris = tris.copy()
    flip = cross < 0
    tris[flip] = tris[flip][:, [0, 2, 1]]
    return tris[np.abs(cross) > 1e-10]


def _halfplane_poly(n, d, R, centre):
    """Polygon of {p : n.(p - centre) <= d} around centre (n unit)."""
    t = np.array([-n[1], n[0]])
    p0 = centre + n * d
    L = 4 * R
    return Polygon([p0 + L * t, p0 - L * t, p0 - L * t - L * n, p0 + L * t - L * n])


SOURCE_NAMES = {"osm": "OpenStreetMap", "ms": "Microsoft ML footprint", "lidar": "LiDAR 2019 outline"}


def build(terrain, H, lidar=None):
    blds, addr_pts = load_footprints(Local.osm(), H, Local())
    if lidar is not None:
        inside = shapely.box(-H + 1, -H + 1, H - 1, H - 1)
        extra = [p for p in lidar.extra_buildings([b["poly"] for b in blds]) if inside.contains(p)]
        blds += [{"poly": p, "src": "lidar", "osm_id": None, "tags": {}} for p in extra]
        print(f"  LiDAR found {len(extra)} buildings missing from OSM and Microsoft")
    assign_addresses(blds, addr_pts)
    ortho = OrthoSampler(H)
    walls, roofs = MeshBuilder(), MeshBuilder()
    feats = []
    stats = {}
    for b in blds:
        P = orient(Polygon(b["poly"].exterior).simplify(0.15), 1.0)
        if not P.is_valid or P.area < 4:
            continue
        kind = KIND.get(b["tags"].get("building"), None)
        if kind is None:
            kind = "civic" if (P.area > 400 or b["tags"].get("building") not in (None, "yes")) else "house"
        faces, skel_h = skeleton_faces(P)
        if faces is None:
            faces, skel_h = [], 0.0
        fid = len(feats)
        ring = np.asarray(P.exterior.coords)[:-1]
        c = np.asarray(P.centroid.coords[0])
        gz = terrain.height(np.r_[ring[:, 0], c[0]], np.r_[ring[:, 1], c[1]])
        ground = float(np.median(gz))
        base = float(gz.min()) - 0.4
        lstats = lidar.building_stats(P, ground) if lidar is not None else None
        h = _height_info(b, kind, skel_h, lstats)
        if not faces:
            h.update(slope=0.0, rise=0.0, eave=h["total"])
        eave_abs = ground + h["eave"]

        rc = ortho.roof_colour(P)
        roof_lin = srgb_to_linear(ROOF_FALLBACK_SRGB if rc is None else np.clip(rc * 0.95, 0, 1))
        shade = 1.0 + 0.035 * (((fid * 2654435761) % 1000) / 1000.0 - 0.5)  # subtle variety
        wall_lin = srgb_to_linear(np.clip(WALL_SRGB * shade, 0, 1))

        # --- roof -------------------------------------------------------------
        R = math.hypot(P.bounds[2] - P.bounds[0], P.bounds[3] - P.bounds[1]) + 1
        top_parts = []
        roof_faces = faces if h["slope"] > 0 else [(P, None, None)]
        for poly, a, n in roof_faces:
            if a is None:
                pieces = [(poly, None, None)]
            else:
                sloped = _areal(shapely.intersection(poly, _halfplane_poly(n, h["cap_d"], R, a), grid_size=G))
                flat = _areal(shapely.difference(poly, sloped, grid_size=G))
                pieces = [(p, a, n) for p in getattr(sloped, "geoms", [sloped]) if p.geom_type == "Polygon"]
                top_parts += [p for p in getattr(flat, "geoms", [flat]) if p.geom_type == "Polygon" and p.area > 0.01]
            for p, aa, nn in pieces:
                if p.is_empty or p.area < 0.01:
                    continue
                v2, tris = _triangulate(p)
                tris = _up_tris(v2, tris)
                if aa is None:
                    z = np.full(len(v2), eave_abs + h["rise"])
                else:
                    z = eave_abs + h["slope"] * np.clip((v2 - aa) @ nn, 0, h["cap_d"])
                v3 = np.column_stack([v2, z])
                roofs.add(v3, tris, roof_lin, fid)
        if top_parts:
            top = _areal(shapely.union_all(top_parts, grid_size=G))
            for p in getattr(top, "geoms", [top]):
                if p.geom_type != "Polygon" or p.area < 0.01:
                    continue
                v2, tris = _triangulate(p)
                tris = _up_tris(v2, tris)
                roofs.add(np.column_stack([v2, np.full(len(v2), eave_abs + h["rise"])]), tris, roof_lin, fid,
                          normal=(0, 0, 1))

        # --- walls (inset under the eaves) or posts for open canopies -----------
        overhang = 0.45 if kind in ("house", "civic") and skel_h > 2.5 else 0.0
        wall_poly = P
        if overhang:
            wp = P.buffer(-overhang, join_style="mitre", mitre_limit=5)
            if wp.geom_type == "Polygon" and wp.area > 0.6 * P.area:
                wall_poly = orient(wp, 1.0)
            else:
                overhang = 0.0
        wall_top = eave_abs + h["slope"] * min(overhang, h["cap_d"])
        if kind == "canopy":
            post_lin = srgb_to_linear(POST_SRGB)
            pb = P.buffer(-0.25, join_style="mitre")
            post_pts = np.asarray(pb.exterior.coords)[:-1] if pb.geom_type == "Polygon" and not pb.is_empty else ring
            for (px, pn) in post_pts:
                gz0 = float(terrain.height(px, pn)) - 0.2
                _box(walls, px, pn, 0.06, gz0, eave_abs, post_lin, fid)
        else:
            wr = np.asarray(wall_poly.exterior.coords)
            for i in range(len(wr) - 1):
                (x0, n0), (x1, n1) = wr[i], wr[i + 1]
                if math.hypot(x1 - x0, n1 - n0) < 1e-3:
                    continue
                v3 = np.array([[x0, n0, base], [x1, n1, base], [x1, n1, wall_top], [x0, n0, wall_top]])
                out_n = np.array([n1 - n0, -(x1 - x0), 0.0])
                out_n /= np.linalg.norm(out_n)
                walls.add(v3, [[0, 1, 2], [0, 2, 3]], wall_lin, fid, normal=out_n)

        stats[h["src"]] = stats.get(h["src"], 0) + 1
        feats.append({
            "id": fid,
            "source": SOURCE_NAMES[b["src"]],
            "lidar_coverage": lstats["coverage"] if lstats else None,
            "osm_id": b["osm_id"],
            "type": b["tags"].get("building", "yes"),
            "kind": kind,
            "name": b["tags"].get("name"),
            "addresses": b["addresses"][:6],
            "levels": h["levels"],
            "height_m": round(h["total"], 2),
            "eave_m": round(h["eave"], 2),
            "height_source": h["src"],
            "roof": "hipped" if h["slope"] > 0 else "flat",
            "roof_colour": None if rc is None else "#%02x%02x%02x" % tuple(int(v * 255) for v in rc),
            "footprint_m2": round(P.area, 1),
            "ground_ahd": round(ground, 2),
            "centroid": [round(float(c[0]), 2), round(float(c[1]), 2)],
            "footprint": np.round(ring, 2).tolist(),
        })
    print(f"  buildings: {len(feats)}; height sources: {stats}")
    return walls.arrays(), roofs.arrays(), feats


def _box(mb, x, n, hw, z0, z1, colour, fid):
    """Thin vertical post (square, half-width hw) as 4 side quads."""
    corners = [(x - hw, n - hw), (x + hw, n - hw), (x + hw, n + hw), (x - hw, n + hw), (x - hw, n - hw)]
    for i in range(4):
        (x0, n0), (x1, n1) = corners[i], corners[i + 1]
        v3 = np.array([[x0, n0, z0], [x1, n1, z0], [x1, n1, z1], [x0, n0, z1]])
        out_n = np.array([n1 - n0, -(x1 - x0), 0.0])
        out_n /= np.linalg.norm(out_n)
        mb.add(v3, [[0, 1, 2], [0, 2, 3]], colour, fid, normal=out_n)
