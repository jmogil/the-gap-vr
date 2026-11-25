"""Roof geometry from building footprints: hips, gables, skillions and flat roofs.

The straight-skeleton (hipped) roof of a simple polygon is the lower envelope of one
"slab" per edge (Eppstein & Erickson, 1999): the plane rising inward from the edge,
bounded sideways by rays from the edge's endpoints - perpendicular to the edge at
convex corners, along the angle bisector at reflex corners.

Making an edge a *gable* removes its slab from the envelope: the neighbouring roof
faces then run straight through to that wall, which is built up to the roof line.
Removing every slab but one gives a skillion (mono-pitch) roof.  The same slabs give
a fast `distance_field` (roof height per unit slope at any point) that roof_fit.py uses
to score each roof form against LiDAR heights.
"""
from __future__ import annotations

import math

import numpy as np
import shapely
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient

GRID = 1e-3  # overlay snapping grid (m)


def _halfplane(w, c, R):
    """Polygon of {p : w.p < c} within radius R of the origin (None = empty, 'all' = everything)."""
    nw = np.hypot(*w)
    if nw < 1e-12:
        return "all" if c > 0 else None
    w = w / nw
    c = c / nw
    if c > R:
        return "all"
    if c < -R:
        return None
    p0 = w * c
    t = np.array([-w[1], w[0]])
    L = 4 * R
    return Polygon([p0 + L * t, p0 - L * t, p0 - L * t - L * w, p0 + L * t - L * w])


def _areal(geom):
    """Keep only the polygonal parts of an overlay result (drops stray lines/points)."""
    parts = _polys(geom, 0.0)
    if not parts:
        return Polygon()
    return parts[0] if len(parts) == 1 else shapely.geometry.MultiPolygon(parts)


def _polys(geom, min_area=1e-4):
    out = []
    for g in getattr(geom, "geoms", [geom]):
        if isinstance(g, Polygon) and not g.is_empty and g.area > min_area:
            out.append(g)
        elif hasattr(g, "geoms"):
            out.extend(_polys(g, min_area))
    return out


class RoofFrame:
    """Edge/slab geometry of one footprint (CCW, coordinates relative to its centroid)."""

    def __init__(self, poly: Polygon):
        poly = orient(Polygon(poly.exterior), 1.0)
        self.poly = poly
        self.c0 = np.array(poly.centroid.coords[0])
        self.P = shapely.affinity.translate(poly, -self.c0[0], -self.c0[1])
        pts = np.asarray(self.P.exterior.coords)[:-1]
        self.pts, self.m = pts, len(pts)
        self.R = float(np.max(np.hypot(pts[:, 0], pts[:, 1]))) * 1.5 + 1.0
        d = np.roll(pts, -1, axis=0) - pts
        self.lens = np.hypot(d[:, 0], d[:, 1])
        self.u = d / self.lens[:, None]
        self.nrm = np.stack([-self.u[:, 1], self.u[:, 0]], axis=1)   # inward normals
        u_prev = np.roll(self.u, 1, axis=0)
        self.convex = (u_prev[:, 0] * self.u[:, 1] - u_prev[:, 1] * self.u[:, 0]) > 1e-9  # at vertex i
        bis = self.nrm + np.roll(self.nrm, 1, axis=0)
        bis /= np.maximum(np.hypot(bis[:, 0], bis[:, 1]), 1e-12)[:, None]
        L = 4 * self.R
        self.quads, self.slabs = [], []
        for i in range(self.m):
            j = (i + 1) % self.m
            ra = self.nrm[i] if self.convex[i] else bis[i]
            rb = self.nrm[i] if self.convex[j] else bis[j]
            q = np.array([pts[i], pts[j], pts[j] + L * rb, pts[i] + L * ra])   # CCW
            s = Polygon(q)
            self.quads.append(q)
            self.slabs.append(s if s.is_valid else s.buffer(0))
        self.off = np.einsum("ij,ij->i", pts, self.nrm)               # dist_i(p) = n_i.p - off_i

    # -- fast evaluation ------------------------------------------------------
    def distance_field(self, xy, gables=()):
        """Roof height per unit slope at points xy (original coordinates); inf where no slab applies."""
        p = np.asarray(xy, dtype=np.float64).reshape(-1, 2) - self.c0
        D = np.full(len(p), np.inf)
        for i in range(self.m):
            if i in gables:
                continue
            q = self.quads[i]
            inside = np.ones(len(p), bool)
            for k in range(4):
                a, b = q[k], q[(k + 1) % 4]
                inside &= (b[0] - a[0]) * (p[:, 1] - a[1]) - (b[1] - a[1]) * (p[:, 0] - a[0]) >= -1e-6
            dist = p[:, 0] * self.nrm[i, 0] + p[:, 1] * self.nrm[i, 1] - self.off[i]
            D = np.where(inside & (dist < D), dist, D)
        return D

    def candidates(self, area):
        """Roof forms worth testing: hip, gables at wing ends / across / along the main axis,
        and skillions on small simple buildings. Returns [(kind, gable_edge_indices)]."""
        m, L = self.m, self.lens
        cc = [i for i in range(m) if self.convex[i] and self.convex[(i + 1) % m] and L[i] >= 1.5]
        rc = np.asarray(self.poly.minimum_rotated_rectangle.exterior.coords)[:4]
        e1, e2 = rc[1] - rc[0], rc[2] - rc[1]
        ax = e1 if np.hypot(*e1) >= np.hypot(*e2) else e2
        along = np.abs(self.u @ (ax / max(np.hypot(*ax), 1e-9)))       # 1 = parallel to the long axis
        ends = [i for i in cc if L[i] <= 1.05 * min(L[i - 1], L[(i + 1) % m])]
        across = [i for i in cc if along[i] < 0.42]
        lengthwise = [i for i in cc if along[i] > 0.9]
        out, seen = [("hip", ())], {()}
        for G in (ends, across, lengthwise):
            G = tuple(sorted(G))
            if G and G not in seen and len(G) < m - 1:
                seen.add(G)
                out.append(("gable", G))
        if m <= 6 and area < 120:
            for i in range(m):
                G = tuple(j for j in range(m) if j != i)
                if L[i] >= 0.6 * L.max() and G not in seen:
                    seen.add(G)
                    out.append(("skillion", G))
        return out

    # -- exact roof faces -------------------------------------------------------
    def faces(self, gables=()):
        """Roof faces [(polygon, a, n)] for the given gable edges; the height of a point p
        on a face is slope * dot(p - a, n). Returns (faces or None if invalid, max height)."""
        active = [i for i in range(self.m) if i not in gables]
        faces = []
        for i in active:
            cutters = []
            for j in active:
                if j == i:
                    continue
                # region where slab j is strictly lower than slab i (ties -> lower index)
                w = self.nrm[j] - self.nrm[i]
                c = self.off[j] - self.off[i] + (1e-7 if j < i else -1e-7)
                hp = _halfplane(w, c, self.R)
                if hp is None:
                    continue
                cut = self.slabs[j] if hp == "all" else _areal(shapely.intersection(self.slabs[j], hp, grid_size=GRID))
                if not cut.is_empty:
                    cutters.append(cut)
            face = _areal(shapely.intersection(self.slabs[i], self.P, grid_size=GRID))
            if cutters and not face.is_empty:
                face = _areal(shapely.difference(face, _areal(shapely.union_all(cutters, grid_size=GRID)),
                                                 grid_size=GRID))
            for f in _polys(face, 0.02):
                if f.area / max(f.length, 1e-9) < 0.01:      # drop needle-thin slivers
                    continue
                faces.append((shapely.affinity.translate(f, self.c0[0], self.c0[1]),
                              self.pts[i] + self.c0, self.nrm[i]))
        hmax = 0.0
        for f, a, n in faces:
            hmax = max(hmax, float(((np.asarray(f.exterior.coords) - a) @ n).max()))
        ok = abs(sum(f.area for f, _, _ in faces) - self.poly.area) < 0.01 * self.poly.area + 0.05
        if ok and not gables:  # plain hips must also reach the true collapse height
            inner = self.poly.buffer(-hmax * 0.98, join_style="mitre", mitre_limit=50)
            beyond = self.poly.buffer(-hmax * 1.02 - 0.02, join_style="mitre", mitre_limit=50)
            ok = (not inner.is_empty) and beyond.is_empty
        return (faces if ok else None), hmax

    def edge_for(self, x0, x1):
        """Index of the footprint edge parallel to and nearest the wall segment x0 -> x1."""
        d = np.asarray(x1, float) - np.asarray(x0, float)
        ln = math.hypot(*d)
        if ln < 1e-9:
            return None
        mid = (np.asarray(x0, float) + np.asarray(x1, float)) / 2 - self.c0
        best, best_d = None, math.inf
        for i in range(self.m):
            if abs(self.u[i] @ (d / ln)) < 0.98:
                continue
            dist = abs(mid @ self.nrm[i] - self.off[i])
            if dist < best_d:
                best, best_d = i, dist
        return best if best_d < 1.5 else None


def skeleton_faces(poly: Polygon):
    """Hipped-roof faces of a footprint (see RoofFrame.faces)."""
    return RoofFrame(poly).faces(())


def collapse_height(poly: Polygon, tol=0.02):
    """Largest inward offset distance before the footprint vanishes (bisection)."""
    lo, hi = 0.0, max(poly.bounds[2] - poly.bounds[0], poly.bounds[3] - poly.bounds[1])
    while hi - lo > tol:
        mid = (lo + hi) / 2
        if poly.buffer(-mid, join_style="mitre", mitre_limit=50).is_empty:
            hi = mid
        else:
            lo = mid
    return lo
