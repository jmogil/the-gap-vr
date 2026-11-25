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
from roof_fit import fit_roof
from roofs import RoofFrame, _areal

G = 1e-3  # overlay grid (m)
FIT_OK = 0.12  # m RMS: below this a clean fitted roof form is used, above it the measured surface

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
DOOR_SRGB = [np.array(c) / 255.0 for c in ((92, 62, 44), (238, 236, 230), (60, 70, 66), (120, 84, 56))]
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
        self.pos, self.nrm, self.col, self.fid, self.idx, self.fac = [], [], [], [], [], []
        self.n = 0

    def add(self, verts3, tris, colour_lin, fid, normal=None, fac=None):
        """verts3: (k,3) in local (x, north, z_abs); tris: (t,3) indices into verts3."""
        verts3 = np.asarray(verts3, dtype=np.float64)
        tris = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
        if len(tris) == 0:
            return
        if isinstance(normal, str) and normal == "smooth":   # area-weighted per-vertex normals
            p = verts3[tris]
            fn = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
            normal = np.zeros_like(verts3)
            for k in range(3):
                np.add.at(normal, tris[:, k], fn)
            normal /= np.maximum(np.linalg.norm(normal, axis=1, keepdims=True), 1e-12)
        elif normal is None:  # flat normal from first non-degenerate triangle
            for t in tris:
                a, b, c = verts3[t]
                nv = np.cross(b - a, c - a)
                if np.linalg.norm(nv) > 1e-9:
                    normal = nv / np.linalg.norm(nv)
                    break
            else:
                return
        normal = np.asarray(normal, dtype=np.float64)
        self.pos.append(verts3)
        self.nrm.append(normal if normal.ndim == 2 else np.repeat(normal[None, :], len(verts3), axis=0))
        self.col.append(np.repeat(np.asarray(colour_lin)[None, :], len(verts3), axis=0))
        self.fid.append(np.full(len(verts3), fid, dtype=np.float32))
        self.fac.append(np.asarray(fac, np.float32) if fac is not None
                        else np.tile(np.array([0, 0, 0, -1], np.float32), (len(verts3), 1)))
        self.idx.append(tris + self.n)
        self.n += len(verts3)

    def arrays(self):
        if not self.pos:
            return None
        return dict(pos=np.concatenate(self.pos), nrm=np.concatenate(self.nrm),
                    col=np.concatenate(self.col), fid=np.concatenate(self.fid),
                    idx=np.concatenate(self.idx), fac=np.concatenate(self.fac))


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


def _road_lines():
    local = Local.osm()
    osm = json.loads((RAW / "osm" / "osm.json").read_text())
    lines = []
    for e in osm["elements"]:
        t = e.get("tags", {})
        if e["type"] == "way" and t.get("highway") in ("primary", "secondary", "tertiary", "residential",
                                                       "unclassified", "living_street", "road"):
            lines.append(shapely.LineString(local.ring([(p["lon"], p["lat"]) for p in e["geometry"]])))
    return lines


def _front_door(ring, tree, lines):
    """(wall index, door centre) on the longest-enough wall facing the nearest street, or None."""
    best = None
    for i in range(len(ring) - 1):
        a, b = np.asarray(ring[i]), np.asarray(ring[i + 1])
        seg = np.linalg.norm(b - a)
        if seg < 2.4:
            continue
        mid = (a + b) / 2
        o = np.array([b[1] - a[1], -(b[0] - a[0])]) / seg            # outward normal
        road = lines[tree.nearest(shapely.Point(*mid))]
        q = np.array(road.interpolate(road.project(shapely.Point(*mid))).coords[0])
        to_road = q - mid
        dist = np.linalg.norm(to_road)
        facing = float(o @ to_road / max(dist, 1e-9))
        if facing < 0.3:
            continue
        score = dist - 6 * facing
        if best is None or score < best[0]:
            best = (score, i, mid)
    return None if best is None else (best[1], best[2])


def build(terrain, H, lidar=None):
    road_lines = _road_lines()
    road_tree = STRtree(road_lines)
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
    stats, roof_types = {}, {}
    for b in blds:
        P = orient(Polygon(b["poly"].exterior).simplify(0.15), 1.0)
        if not P.is_valid or P.area < 4:
            continue
        kind = KIND.get(b["tags"].get("building"), None)
        if kind is None:
            kind = "civic" if (P.area > 400 or b["tags"].get("building") not in (None, "yes")) else "house"
        frame = RoofFrame(P)
        faces, skel_h = frame.faces(())
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
        # Default roof: a hip between the eave and ridge heights (measured or estimated).
        roof = dict(type="hip" if h["slope"] > 0 else "flat", gables=(), faces=faces, slope=h["slope"],
                    cap=h["cap_d"] if h["slope"] > 0 else math.inf, eave_abs=ground + h["eave"],
                    rms=None, fitted=False, flat_top=False)
        if h["slope"] <= 0:
            roof["eave_abs"] = ground + h["total"]
        # LiDAR roofs: a clean fitted form (flat / hip / gable / skillion) where one explains the
        # returns to within FIT_OK; otherwise the measured roof surface itself.
        surface = None
        if lstats is not None:
            xy, zz = lidar.roof_samples(P)
            if len(zz) >= 12:
                fit = fit_roof(frame, xy, zz, P.area)
                fit_faces = [] if fit["type"] == "flat" else frame.faces(fit["gables"])[0]
                fit_ok = fit["eave"] >= ground + 1.2 and (fit["type"] == "flat" or fit_faces)
                if not (fit_ok and fit["rms"] <= FIT_OK):
                    surface = lidar.roof_surface(P)
                if surface is None and fit_ok:
                    roof = dict(type=fit["type"], gables=fit["gables"], faces=fit_faces, slope=fit["slope"],
                                cap=fit["cap"], eave_abs=fit["eave"], rms=fit["rms"], fitted=True,
                                flat_top=fit["flat_top"])
                elif surface is not None:
                    roof = dict(type="measured surface", gables=(), faces=[], slope=0.0, cap=math.inf,
                                eave_abs=float(np.percentile(surface.height_at(ring), 20)), rms=fit["rms"],
                                fitted=True, flat_top=False)
                roof_types[roof["type"]] = roof_types.get(roof["type"], 0) + 1
        eave_abs = roof["eave_abs"]

        rc = ortho.roof_colour(P)
        roof_lin = srgb_to_linear(ROOF_FALLBACK_SRGB if rc is None else np.clip(rc * 0.95, 0, 1))
        shade = 1.0 + 0.035 * (((fid * 2654435761) % 1000) / 1000.0 - 0.5)  # subtle variety
        wall_lin = srgb_to_linear(np.clip(WALL_SRGB * shade, 0, 1))

        # --- roof -------------------------------------------------------------
        R = math.hypot(P.bounds[2] - P.bounds[0], P.bounds[3] - P.bounds[1]) + 1
        slope, cap = roof["slope"], roof["cap"]
        top_z = eave_abs + slope * cap if math.isfinite(cap) else None
        zmax = eave_abs
        top_parts = []
        roof_faces = roof["faces"] if slope > 0 else [(P, None, None)]
        if surface is not None:          # measured roof: the clipped LiDAR TIN
            roof_faces = []
            roofs.add(surface.verts, _up_tris(surface.verts[:, :2], surface.tris), roof_lin, fid, normal="smooth")
            zmax = float(surface.verts[:, 2].max())
        for poly, a, n in roof_faces:
            if a is None:
                pieces = [(poly, None, None)]
            elif top_z is None:
                pieces = [(poly, a, n)]
            else:
                sloped = _areal(shapely.intersection(poly, _halfplane_poly(n, cap, R, a), grid_size=G))
                flat = _areal(shapely.difference(poly, sloped, grid_size=G))
                pieces = [(p, a, n) for p in getattr(sloped, "geoms", [sloped]) if p.geom_type == "Polygon"]
                top_parts += [p for p in getattr(flat, "geoms", [flat]) if p.geom_type == "Polygon" and p.area > 0.01]
            for p, aa, nn in pieces:
                if p.is_empty or p.area < 0.01:
                    continue
                v2, tris = _triangulate(p)
                tris = _up_tris(v2, tris)
                if aa is None:
                    z = np.full(len(v2), eave_abs)
                else:
                    z = eave_abs + slope * np.clip((v2[:, 0] - aa[0]) * nn[0] + (v2[:, 1] - aa[1]) * nn[1], 0, cap)
                zmax = max(zmax, float(z.max()))
                roofs.add(np.column_stack([v2, z]), tris, roof_lin, fid)
        if top_parts:
            top = _areal(shapely.union_all(top_parts, grid_size=G))
            for p in getattr(top, "geoms", [top]):
                if p.geom_type != "Polygon" or p.area < 0.01:
                    continue
                v2, tris = _triangulate(p)
                tris = _up_tris(v2, tris)
                zmax = max(zmax, top_z)
                roofs.add(np.column_stack([v2, np.full(len(v2), top_z)]), tris, roof_lin, fid, normal=(0, 0, 1))

        def roof_z(pts2):
            """Roof height above points (k, 2) - used to run walls up to the roof line."""
            if surface is not None:
                return surface.height_at(pts2) - 0.05     # tuck just under the measured roof
            if slope <= 0:
                return np.full(len(pts2), eave_abs)
            D = frame.distance_field(pts2, roof["gables"])
            return eave_abs + slope * np.minimum(np.where(np.isfinite(D), D, 0.0), cap)

        # --- walls (inset under the eaves) or posts for open canopies -----------
        pitched = slope > 0 or surface is not None
        overhang = 0.45 if kind in ("house", "civic") and skel_h > 2.5 and pitched else 0.0
        wall_poly = P
        if overhang:
            wp = P.buffer(-overhang, join_style="mitre", mitre_limit=5)
            if wp.geom_type == "Polygon" and wp.area > 0.6 * P.area:
                wall_poly = orient(wp, 1.0)
            else:
                overhang = 0.0
        wall_top = eave_abs + slope * min(overhang, cap)
        if kind == "canopy":
            post_lin = srgb_to_linear(POST_SRGB)
            pb = P.buffer(-0.25, join_style="mitre")
            post_pts = np.asarray(pb.exterior.coords)[:-1] if pb.geom_type == "Polygon" and not pb.is_empty else ring
            for (px, pn) in post_pts:
                gz0 = float(terrain.height(px, pn)) - 0.2
                _box(walls, px, pn, 0.06, gz0, eave_abs, post_lin, fid)
        else:
            wr = np.asarray(wall_poly.exterior.coords)
            # windows only on homes and civic buildings tall enough to have them
            win_eave = eave_abs - ground if kind in ("house", "civic") and P.area >= 35 and eave_abs - ground >= 2.3 else -1.0
            door = _front_door(wr, road_tree, road_lines) if win_eave > 0 and P.area >= 45 else None
            for i in range(len(wr) - 1):
                (x0, n0), (x1, n1) = wr[i], wr[i + 1]
                seg = math.hypot(x1 - x0, n1 - n0)
                if seg < 1e-3:
                    continue
                out_n = np.array([n1 - n0, -(x1 - x0), 0.0])
                out_n /= np.linalg.norm(out_n)
                edge = frame.edge_for((x0, n0), (x1, n1)) if roof["gables"] else None
                if surface is not None or (edge is not None and edge in roof["gables"]):
                    # Gable, skillion high side or measured roof: the wall rises to meet the roof line.
                    step = 0.25 if surface is not None else 0.05
                    t = np.linspace(0.0, 1.0, max(2, int(seg / step) + 1))
                    pts2 = np.column_stack([x0 + t * (x1 - x0), n0 + t * (n1 - n0)])
                    zt = roof_z(pts2) - (0.02 if surface is not None else 0.0)
                    keep = _simplify_profile(t * seg, zt, 0.04 if surface is not None else 0.002)
                    k = len(keep)                      # one shared strip: bottom/top vertex pairs
                    v3 = np.concatenate([np.column_stack([pts2[keep], np.full(k, base)]),
                                         np.column_stack([pts2[keep], zt[keep]])])
                    tr = [[i, i + 1, k + i + 1] for i in range(k - 1)] + [[i, k + i + 1, k + i] for i in range(k - 1)]
                    u = np.r_[t[keep], t[keep]] * seg
                    walls.add(v3, tr, wall_lin, fid, normal=out_n,
                              fac=np.column_stack([u, v3[:, 2] - ground, np.full(2 * k, seg), np.full(2 * k, win_eave)]))
                else:
                    v3 = np.array([[x0, n0, base], [x1, n1, base], [x1, n1, wall_top], [x0, n0, wall_top]])
                    walls.add(v3, [[0, 1, 2], [0, 2, 3]], wall_lin, fid, normal=out_n,
                              fac=np.column_stack([[0, seg, seg, 0], v3[:, 2] - ground, np.full(4, seg), np.full(4, win_eave)]))
                if door is not None and door[0] == i:
                    dm, o = door[1], out_n[:2]
                    tdir = np.array([x1 - x0, n1 - n0]) / seg
                    gz = float(terrain.height(*dm))
                    da, db = dm - tdir * 0.48 + o * 0.03, dm + tdir * 0.48 + o * 0.03
                    dv = np.array([[*da, gz - 0.1], [*db, gz - 0.1], [*db, gz + 2.1], [*da, gz + 2.1]])
                    dcol = DOOR_SRGB[fid % len(DOOR_SRGB)]
                    walls.add(dv, [[0, 1, 2], [0, 2, 3]], srgb_to_linear(dcol), fid, normal=out_n)

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
            "height_m": round(zmax - ground, 2),
            "eave_m": round(eave_abs - ground, 2),
            "height_source": h["src"],
            "roof": roof["type"] + (" with flat top" if roof["flat_top"] else ""),
            "roof_pitch_deg": round(surface.pitch_deg() if surface is not None else math.degrees(math.atan(slope)), 1),
            "roof_from": "LiDAR surface" if surface is not None else "LiDAR fit" if roof["fitted"] else ("modelled" if not lstats else "LiDAR heights, modelled hip"),
            "roof_fit_rms_m": None if roof["rms"] is None else round(roof["rms"], 3),
            "roof_colour": None if rc is None else "#%02x%02x%02x" % tuple(int(v * 255) for v in rc),
            "footprint_m2": round(P.area, 1),
            "ground_ahd": round(ground, 2),
            "centroid": [round(float(c[0]), 2), round(float(c[1]), 2)],
            "footprint": np.round(ring, 2).tolist(),
        })
    print(f"  buildings: {len(feats)}; height sources: {stats}")
    print(f"  LiDAR-fitted roof forms: {roof_types}")
    return walls.arrays(), roofs.arrays(), feats


def _simplify_profile(s, z, tol):
    """Douglas-Peucker on a wall-top profile (s along the wall, z height): indices to keep."""
    keep = np.zeros(len(s), bool)
    keep[[0, -1]] = True
    stack = [(0, len(s) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        zi = z[i] + (z[j] - z[i]) * (s[i + 1:j] - s[i]) / max(s[j] - s[i], 1e-9)
        k = int(np.argmax(np.abs(z[i + 1:j] - zi))) + i + 1
        if abs(z[k] - (z[i] + (z[j] - z[i]) * (s[k] - s[i]) / max(s[j] - s[i], 1e-9))) > tol:
            keep[k] = True
            stack += [(i, k), (k, j)]
    return np.nonzero(keep)[0]


def _box(mb, x, n, hw, z0, z1, colour, fid):
    """Thin vertical post (square, half-width hw) as 4 side quads."""
    corners = [(x - hw, n - hw), (x + hw, n - hw), (x + hw, n + hw), (x - hw, n + hw), (x - hw, n - hw)]
    for i in range(4):
        (x0, n0), (x1, n1) = corners[i], corners[i + 1]
        v3 = np.array([[x0, n0, z0], [x1, n1, z0], [x1, n1, z1], [x0, n0, z1]])
        out_n = np.array([n1 - n0, -(x1 - x0), 0.0])
        out_n /= np.linalg.norm(out_n)
        mb.add(v3, [[0, 1, 2], [0, 2, 3]], colour, fid, normal=out_n)
