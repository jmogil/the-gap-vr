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
