"""Hipped roofs from building footprints via the straight skeleton.

The straight-skeleton roof of a simple polygon is the lower envelope of one
"slab" per edge (Eppstein & Erickson, 1999): the plane rising inward from the
edge, bounded sideways by rays from the edge's endpoints - perpendicular to the
edge at convex corners, along the angle bisector at reflex corners.  Each roof
face is therefore computed with a handful of polygon clips, then validated
against the mitred inward offset of the footprint (which is the same wavefront).
"""
from __future__ import annotations

import numpy as np
import shapely
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient


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


def skeleton_faces(poly: Polygon):
    """Return (faces, max_height) where faces = [(polygon, a, n)] and the height of a
    point p in a face above the eave is dot(p - a, n).  Coordinates are taken relative
    to the polygon centroid internally and returned in the original frame."""
    poly = orient(Polygon(poly.exterior), 1.0)
    c0 = np.array(poly.centroid.coords[0])
    P = shapely.affinity.translate(poly, -c0[0], -c0[1])
    pts = np.asarray(P.exterior.coords)[:-1]
    m = len(pts)
    R = float(np.max(np.hypot(pts[:, 0], pts[:, 1]))) * 1.5 + 1.0
    d = np.roll(pts, -1, axis=0) - pts
    lens = np.hypot(d[:, 0], d[:, 1])
    u = d / lens[:, None]
    nrm = np.stack([-u[:, 1], u[:, 0]], axis=1)           # inward (left) normals for CCW
    u_prev = np.roll(u, 1, axis=0)
    cross = u_prev[:, 0] * u[:, 1] - u_prev[:, 1] * u[:, 0]
    convex = cross > 1e-9                                  # at vertex i (start of edge i)
    bis = nrm + np.roll(nrm, 1, axis=0)
    bis /= np.maximum(np.hypot(bis[:, 0], bis[:, 1]), 1e-12)[:, None]

    # Unclipped slab regions; the footprint is applied once at the end, which avoids
    # sliver artefacts along shared boundaries.  All overlay ops snap to a 1 mm grid.
    G = 1e-3
    slabs = []
    for i in range(m):
        j = (i + 1) % m
        ra = nrm[i] if convex[i] else bis[i]
        rb = nrm[i] if convex[j] else bis[j]
        L = 4 * R
        q = Polygon([pts[i], pts[j], pts[j] + L * rb, pts[i] + L * ra])
        if not q.is_valid:
            q = q.buffer(0)
        slabs.append(q)
    off = np.einsum("ij,ij->i", pts, nrm)                  # h_i(p) = n_i.p - off_i

    faces = []
    for i in range(m):
        cutters = []
        for j in range(m):
            if j == i:
                continue
            # region where slab j is strictly lower than slab i (ties -> lower index)
            w = nrm[j] - nrm[i]
            c = off[j] - off[i] + (1e-7 if j < i else -1e-7)
            hp = _halfplane(w, c, R)
            if hp is None:
                continue
            cut = slabs[j] if hp == "all" else _areal(shapely.intersection(slabs[j], hp, grid_size=G))
            if not cut.is_empty:
                cutters.append(cut)
        face = _areal(shapely.intersection(slabs[i], P, grid_size=G))
        if cutters and not face.is_empty:
            union = _areal(shapely.union_all(cutters, grid_size=G))
            face = _areal(shapely.difference(face, union, grid_size=G))
        for f in _polys(face, 0.02):
            if f.area / max(f.length, 1e-9) < 0.01:      # drop needle-thin slivers
                continue
            faces.append((shapely.affinity.translate(f, c0[0], c0[1]), pts[i] + c0, nrm[i]))

    # Validation: faces must tile the footprint and reach the true collapse height.
    area = sum(f.area for f, _, _ in faces)
    hmax = 0.0
    for f, a, n in faces:
        fc = np.asarray(f.exterior.coords) - a
        hmax = max(hmax, float((fc @ n).max()))
    ok = abs(area - poly.area) < 0.01 * poly.area + 0.05
    if ok:
        inner = poly.buffer(-hmax * 0.98, join_style="mitre", mitre_limit=50)
        beyond = poly.buffer(-hmax * 1.02 - 0.02, join_style="mitre", mitre_limit=50)
        ok = (not inner.is_empty) and beyond.is_empty
    return (faces if ok else None), hmax


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
