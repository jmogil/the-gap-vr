"""Features derived from the Brisbane 2019 LiDAR grids (see prepare_lidar.py).

  * building_stats()  measured ridge / eave heights for a footprint (class 6 returns)
  * extra_buildings() outlines for buildings the LiDAR sees but OSM + Microsoft miss
  * trees()           individual trees from the canopy height model (classes 3-5)
"""
from __future__ import annotations

import json
import math

import numpy as np
import shapely
from PIL import Image, ImageDraw
from scipy import ndimage
from scipy.spatial import cKDTree
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient

from common import CONFIG, ROOT

GRID = ROOT / "data" / "derived" / "lidar_grids_0.5m.npz"
META = ROOT / "data" / "derived" / "lidar_meta.json"
RES = 0.5


def _planarize(z, valid, res=RES, min_cells=12, max_dg=0.1, max_dz=0.08):
    """Piecewise-planar roof heights: neighbouring cells with matching gradients and continuous
    heights form a plane; each plane is least-squares fitted and every cell takes the height of
    its own plane - or of the nearest large plane if its group is tiny (chimneys, vents, noise)."""
    from scipy import sparse
    from scipy.sparse.csgraph import connected_components

    h, w = z.shape
    g_rows, g_cols = np.gradient(z, res)
    gx, gn = g_cols.ravel(), -g_rows.ravel()          # slope east / north (rows run south)
    zr, vr = z.ravel(), valid.ravel()
    idx = np.arange(h * w).reshape(h, w)
    ea, eb = [], []
    for a, b, dx, dn in ((idx[:, :-1].ravel(), idx[:, 1:].ravel(), res, 0.0),
                         (idx[:-1, :].ravel(), idx[1:, :].ravel(), 0.0, -res)):
        dg = np.hypot(gx[a] - gx[b], gn[a] - gn[b])
        pred = zr[a] + 0.5 * ((gx[a] + gx[b]) * dx + (gn[a] + gn[b]) * dn)
        ok = vr[a] & vr[b] & (dg < max_dg) & (np.abs(zr[b] - pred) < max_dz)
        ea.append(a[ok])
        eb.append(b[ok])
    ea, eb = np.concatenate(ea), np.concatenate(eb)
    graph = sparse.coo_matrix((np.ones(len(ea)), (ea, eb)), shape=(h * w, h * w))
    _, lab = connected_components(graph, directed=False)
    cols, rows = np.meshgrid(np.arange(w), np.arange(h))
    X, Y = (cols * res).ravel(), (-rows * res).ravel()
    counts = np.bincount(lab[vr], minlength=lab.max() + 1)
    big = np.nonzero(counts >= min_cells)[0]
    if len(big) == 0:
        return z
    coef = np.zeros((lab.max() + 1, 3))
    for k in big:
        m = vr & (lab == k)
        A = np.column_stack([np.ones(m.sum()), X[m], Y[m]])
        coef[k] = np.linalg.lstsq(A, zr[m], rcond=None)[0]
    in_big = (vr & np.isin(lab, big)).reshape(h, w)
    near = ndimage.distance_transform_edt(~in_big, return_distances=False, return_indices=True)
    lab_full = lab.reshape(h, w)[tuple(near)].ravel()
    c = coef[lab_full]
    return (c[:, 0] + c[:, 1] * X + c[:, 2] * Y).reshape(h, w)


class RoofSurface:
    """Clipped roof mesh (verts (k,3) local x/north/z AHD, tris (t,3)) plus the full TIN for height queries."""

    def __init__(self, verts, tris, tin_v, tin_t):
        self.verts, self.tris = verts, tris
        self._v, self._t = tin_v, tin_t
        self._a, self._b, self._c = tin_v[tin_t[:, 0]], tin_v[tin_t[:, 1]], tin_v[tin_t[:, 2]]

    def height_at(self, pts):
        """Roof height (m AHD) at points (k, 2) from the TIN (nearest TIN vertex if outside)."""
        a, b, c = self._a, self._b, self._c
        v0x, v0y = b[:, 0] - a[:, 0], b[:, 1] - a[:, 1]
        v1x, v1y = c[:, 0] - a[:, 0], c[:, 1] - a[:, 1]
        den = v0x * v1y - v1x * v0y
        out = np.empty(len(pts))
        for i, (x, y) in enumerate(np.asarray(pts, dtype=np.float64)):
            px, py = x - a[:, 0], y - a[:, 1]
            u = (px * v1y - v1x * py) / den
            w = (v0x * py - px * v0y) / den
            hit = np.nonzero((u >= -1e-6) & (w >= -1e-6) & (u + w <= 1 + 1e-6))[0]
            if len(hit):
                j = hit[0]
                out[i] = a[j, 2] + u[j] * (b[j, 2] - a[j, 2]) + w[j] * (c[j, 2] - a[j, 2])
            else:
                out[i] = self._v[np.argmin((self._v[:, 0] - x) ** 2 + (self._v[:, 1] - y) ** 2), 2]
        return out

    def pitch_deg(self):
        """Area-weighted median pitch of the sloped roof triangles."""
        p = self.verts[self.tris]
        nrm = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
        area = 0.5 * np.linalg.norm(nrm, axis=1)
        tilt = np.degrees(np.arccos(np.clip(np.abs(nrm[:, 2]) / np.maximum(2 * area, 1e-12), 0, 1)))
        m = tilt > 3
        if area[m].sum() < 0.2 * area.sum():
            return 0.0
        o = np.argsort(tilt[m])
        cw = np.cumsum(area[m][o])
        return float(tilt[m][o][np.searchsorted(cw, cw[-1] / 2)])


class LidarGrids:
    @staticmethod
    def available():
        return GRID.exists()

    def __init__(self, terrain):
        d = np.load(GRID)
        self.H = CONFIG["half_size_m"]
        self.n = d["bld_max_cm"].shape[0]

        def f(a):
            return np.where(a > 0, a.astype(np.float32) / 100.0, np.nan)

        self.bld, self.veg, self.gnd = f(d["bld_max_cm"]), f(d["veg_max_cm"]), f(d["gnd_mean_cm"])
        self.n_bld, self.n_veg = d["n_bld"], d["n_veg"]
        c = -self.H + (np.arange(self.n) + 0.5) * RES
        X, N = np.meshgrid(c, -c)                 # row 0 = north edge
        self.dtm = terrain.height(X, N).astype(np.float32)
        self.meta = json.loads(META.read_text()) if META.exists() else {}

    # -- rasterising helpers -------------------------------------------------
    def _window(self, poly: Polygon):
        x0, n0, x1, n1 = poly.bounds
        c0 = max(int(math.floor((x0 + self.H) / RES)) - 1, 0)
        c1 = min(int(math.ceil((x1 + self.H) / RES)) + 1, self.n)
        r0 = max(int(math.floor((self.H - n1) / RES)) - 1, 0)
        r1 = min(int(math.ceil((self.H - n0) / RES)) + 1, self.n)
        return r0, r1, c0, c1

    def _mask(self, poly: Polygon, win):
        r0, r1, c0, c1 = win
        img = Image.new("L", (max(c1 - c0, 1), max(r1 - r0, 1)), 0)
        if poly.is_empty:
            return np.zeros((r1 - r0, c1 - c0), bool)
        for p in getattr(poly, "geoms", [poly]):
            ring = np.asarray(p.exterior.coords)
            pts = list(zip((ring[:, 0] + self.H) / RES - c0, (self.H - ring[:, 1]) / RES - r0))
            ImageDraw.Draw(img).polygon(pts, fill=1)
        return np.asarray(img, bool)

    # -- buildings -----------------------------------------------------------
    def building_stats(self, poly: Polygon, ground_ref: float):
        """Measured heights above `ground_ref`, or None if the roof is not seen in the LiDAR."""
        win = self._window(poly)
        inner = poly.buffer(-0.3, join_style="mitre")
        core = self._mask(inner if not inner.is_empty else poly, win)
        r0, r1, c0, c1 = win
        z = self.bld[r0:r1, c0:c1]
        cells = int(core.sum())
        if cells < 8:
            return None
        h = z[core] - ground_ref
        h = h[np.isfinite(h)]
        coverage = len(h) / cells
        if coverage < 0.25 or len(h) < 6:
            return None
        total = float(np.percentile(h, 97))
        band_poly = poly.buffer(-1.4, join_style="mitre")
        band = core & ~self._mask(band_poly, win) if not band_poly.is_empty else core
        he = z[band] - ground_ref
        he = he[np.isfinite(he)]
        eave = float(np.percentile(he, 30)) if len(he) >= 4 else float(np.percentile(h, 10))
        return {"total": total, "eave": min(eave, total), "coverage": round(coverage, 2)}

    def roof_surface(self, poly: Polygon, max_error=0.06):
        """The measured roof itself: an adaptive TIN of the building returns (spikes removed,
        gaps filled from neighbours) clipped exactly to the footprint. Returns RoofSurface or None."""
        import mapbox_earcut as earcut
        from pydelatin import Delatin

        win = self._window(poly.buffer(1.0))
        r0, r1, c0, c1 = win
        if r1 - r0 < 3 or c1 - c0 < 3:
            return None
        z = self.bld[r0:r1, c0:c1].astype(np.float64)
        foot = self._mask(poly, win)
        valid = np.isfinite(z) & self._mask(poly.buffer(0.5), win)
        if valid.sum() < 8 or (valid & foot).sum() < 0.25 * max(int(foot.sum()), 1):
            return None
        idx = ndimage.distance_transform_edt(~valid, return_distances=False, return_indices=True)
        zf = z[tuple(idx)]
        med = ndimage.median_filter(zf, size=3)
        zf = np.where(np.abs(zf - med) > 0.5, med, zf)          # chimneys, aerials, stray returns
        zf = ndimage.gaussian_filter(zf, 0.6)                    # LiDAR noise
        zf = _planarize(zf, valid)                               # snap cells to fitted roof planes
        tin = Delatin(np.ascontiguousarray(zf, dtype=np.float32), max_error=max_error)
        v = tin.vertices.astype(np.float64)                      # x = col, y = (rows-1) - row
        rows = (zf.shape[0] - 1) - v[:, 1]
        V = np.column_stack([-self.H + (c0 + v[:, 0] + 0.5) * RES, self.H - (r0 + rows + 0.5) * RES, v[:, 2]])
        T = tin.triangles.astype(np.int64)
        # plane z = A x + B n + C for every TIN triangle
        p0, p1, p2 = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
        nz = np.cross(p1 - p0, p2 - p0)
        good = np.abs(nz[:, 2]) > 1e-9
        T, p0, nz = T[good], p0[good], nz[good]
        A, B = -nz[:, 0] / nz[:, 2], -nz[:, 1] / nz[:, 2]
        C = p0[:, 2] - A * p0[:, 0] - B * p0[:, 1]
        xy = V[T][:, :, :2]
        parts = shapely.intersection(shapely.polygons(np.concatenate([xy, xy[:, :1]], axis=1)), poly)
        verts, tris, key = [], [], {}
        for k, g in enumerate(parts):
            for p in getattr(g, "geoms", [g]):
                if p.geom_type != "Polygon" or p.is_empty or p.area < 1e-4:
                    continue
                rings = [np.asarray(p.exterior.coords)[:-1]] + [np.asarray(r.coords)[:-1] for r in p.interiors]
                pv = np.concatenate(rings)
                tr = earcut.triangulate_float64(pv, np.cumsum([len(r) for r in rings]).astype(np.uint32))
                tr = tr.reshape(-1, 3)
                ids = []
                for x, nn in pv:
                    kk = (round(x, 3), round(nn, 3))
                    if kk not in key:
                        key[kk] = len(verts)
                        verts.append((x, nn, A[k] * x + B[k] * nn + C[k]))
                    ids.append(key[kk])
                ids = np.asarray(ids)
                tris.extend(ids[tr].tolist())
        if not tris:
            return None
        return RoofSurface(np.asarray(verts), np.asarray(tris, dtype=np.int64), V, T)

    def roof_samples(self, poly: Polygon, shrink=0.25):
        """Cell centres (local x, north) and roof heights (m AHD) of building returns inside a footprint."""
        win = self._window(poly)
        inner = poly.buffer(-shrink, join_style="mitre")
        mask = self._mask(inner if not inner.is_empty else poly, win)
        r0, r1, c0, c1 = win
        z = self.bld[r0:r1, c0:c1]
        ok = mask & np.isfinite(z)
        rr, cc = np.nonzero(ok)
        x = -self.H + (c0 + cc + 0.5) * RES
        nn = self.H - (r0 + rr + 0.5) * RES
        return np.column_stack([x, nn]), z[ok].astype(np.float64)

    def extra_buildings(self, existing, min_area=20.0, max_area=4000.0):
        """Roof-like clusters of building returns not covered by any known footprint."""
        import rasterio.features
        from rasterio.transform import Affine

        roof = (self.n_bld > 0) & ((self.bld - self.dtm) > 2.2)
        known = np.zeros_like(roof)
        full = (0, self.n, 0, self.n)
        for p in existing:
            win = self._window(p.buffer(1.0))
            m = self._mask(p.buffer(1.0), win)
            known[win[0]:win[1], win[2]:win[3]] |= m
        cand = ndimage.binary_closing(roof & ~known, iterations=2)
        cand = ndimage.binary_opening(cand, iterations=1)
        lab, nlab = ndimage.label(cand)
        sizes = ndimage.sum(np.ones_like(lab), lab, index=np.arange(1, nlab + 1)) * RES * RES
        tf = Affine(RES, 0, -self.H, 0, -RES, self.H)        # grid -> local (x, north)
        out = []
        for k, area in enumerate(sizes, start=1):
            if not (min_area <= area <= max_area):
                continue
            comp = lab == k
            polys = [shapely.geometry.shape(g) for g, v in
                     rasterio.features.shapes(comp.astype(np.uint8), mask=comp, transform=tf) if v == 1]
            if not polys:
                continue
            poly = max(polys, key=lambda p: p.area).simplify(0.4)
            poly = Polygon(poly.exterior).buffer(0)
            if poly.geom_type != "Polygon" or poly.area < min_area:
                continue
            if 4 * math.pi * poly.area / poly.length ** 2 < 0.25:   # long thin strips: walls, fences
                continue
            rect = poly.minimum_rotated_rectangle
            if poly.area / rect.area > 0.78:
                poly = rect
            out.append(orient(poly, 1.0))
        return out

    # -- trees -----------------------------------------------------------------
    def trees(self, buildings, ortho=None, min_height=3.0):
        """Treetops = local maxima of the smoothed canopy height model; crowns = canopy cells
        assigned to their nearest treetop. Returns a list of dicts in local (x, north) metres."""
        chm = np.nan_to_num(self.veg - self.dtm, nan=0.0)
        chm = np.clip(chm, 0, 60)
        chm_s = ndimage.gaussian_filter(chm, 1.0)
        small = ndimage.maximum_filter(chm_s, size=5)          # 2.5 m window
        large = ndimage.maximum_filter(chm_s, size=9)          # 4.5 m window for big trees
        top = (chm_s >= min_height) & np.where(chm_s < 12, chm_s >= small - 1e-4, chm_s >= large - 1e-4)
        lab, nlab = ndimage.label(top)
        if nlab == 0:
            return []
        rc = np.array(ndimage.center_of_mass(top, lab, np.arange(1, nlab + 1)))
        tops_rc = np.round(rc).astype(int)
        heights = ndimage.maximum(chm, lab, np.arange(1, nlab + 1))
        canopy = chm_s >= 2.0
        cr, cc = np.nonzero(canopy)
        tree = cKDTree(tops_rc.astype(float))
        dist, owner = tree.query(np.column_stack([cr, cc]).astype(float), distance_upper_bound=16)
        owner = owner[np.isfinite(dist)]
        area = np.bincount(owner, minlength=len(tops_rc)) * RES * RES
        bindex = shapely.STRtree(buildings) if buildings else None
        out = []
        for i, (r, c) in enumerate(tops_rc):
            h = float(heights[i])
            radius = float(np.clip(math.sqrt(max(area[i], 1.0) / math.pi), 0.8, 9.0))
            x = -self.H + (c + 0.5) * RES
            nn = self.H - (r + 0.5) * RES
            on_roof = False
            if bindex is not None:
                hit = bindex.query(shapely.Point(x, nn), predicate="intersects")
                on_roof = len(hit) > 0
            colour = ortho.crown_colour(x, nn, radius * 0.6) if ortho is not None else None
            out.append({"x": x, "n": nn, "ground": float(self.dtm[r, c]), "h": h, "r": radius,
                        "on_roof": on_roof, "colour": colour})
        return out
