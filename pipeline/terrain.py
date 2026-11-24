"""Terrain: 1 m height grid + adaptive triangle mesh (TIN) from the LiDAR DTM.

Grid convention (used everywhere, including the web viewer):
    samples at local x = -H..H and north = -H..H in 1 m steps (H = half_size_m)
    grid[row, col]: col = x + H, row = H - north   (row 0 is the northern edge)
"""
from __future__ import annotations

import numpy as np
import rasterio
from pydelatin import Delatin

from common import CONFIG, RAW


class Terrain:
    def __init__(self, max_error: float = 0.10):
        H = CONFIG["half_size_m"]
        self.H = H
        self.n = 2 * H + 1
        self.grid = self._load_grid()
        self.zmin = float(self.grid.min())
        self.zmax = float(self.grid.max())
        self.vertices, self.triangles = self._tin(max_error)

    def _load_grid(self) -> np.ndarray:
        res = CONFIG["dem"]["resolution_m"]
        path = RAW / "dem" / f"dem_{res:g}m.tif"
        with rasterio.open(path) as src:
            dem = src.read(1).astype(np.float64)
            left, top = src.transform.c, src.transform.f
        E0, N0, H = CONFIG["anchor"]["easting"], CONFIG["anchor"]["northing"], self.H
        if np.isnan(dem).any():  # fill any gaps with the nearest valid value
            from scipy import ndimage
            idx = ndimage.distance_transform_edt(np.isnan(dem), return_distances=False, return_indices=True)
            dem = dem[tuple(idx)]
        # Bilinear resample onto the 1 m lattice (box-averages the 0.5 m pixels).
        from scipy.ndimage import map_coordinates
        xs = np.arange(-H, H + 1, dtype=np.float64)
        cols = (E0 + xs - left) / res - 0.5
        rows = (top - (N0 - xs)) / res - 0.5  # row 0 = north edge (north = +H)
        rr, cc = np.meshgrid(rows, cols, indexing="ij")
        grid = map_coordinates(dem, [rr, cc], order=1, mode="nearest")
        # Light smoothing for a cleaner mesh, keeping kerb-scale detail.
        from scipy.ndimage import gaussian_filter
        return gaussian_filter(grid, 0.6).astype(np.float32)

    def _tin(self, max_error):
        tin = Delatin(np.ascontiguousarray(self.grid), max_error=max_error)
        v = tin.vertices.astype(np.float64)  # (x=col, y=(n-1)-row, z)
        local = np.empty_like(v)
        local[:, 0] = v[:, 0] - self.H          # x (east)
        local[:, 1] = v[:, 1] - self.H          # north
        local[:, 2] = v[:, 2]                   # elevation, m AHD
        return local, tin.triangles.astype(np.int64)

    # --- sampling ---------------------------------------------------------
    def height(self, x, north):
        """Bilinear height (m AHD) at local coordinates (arrays allowed)."""
        x = np.asarray(x, dtype=np.float64)
        north = np.asarray(north, dtype=np.float64)
        c = np.clip(x + self.H, 0, self.n - 1.000001)
        r = np.clip(self.H - north, 0, self.n - 1.000001)
        c0 = np.floor(c).astype(int)
        r0 = np.floor(r).astype(int)
        fc, fr = c - c0, r - r0
        g = self.grid
        return ((g[r0, c0] * (1 - fc) + g[r0, c0 + 1] * fc) * (1 - fr)
                + (g[r0 + 1, c0] * (1 - fc) + g[r0 + 1, c0 + 1] * fc) * fr)

    def heightmap_u16(self):
        """Grid quantised to centimetres above zmin, for the viewer's height queries."""
        q = np.round((self.grid - self.zmin) * 100.0)
        return np.clip(q, 0, 65535).astype("<u2")
