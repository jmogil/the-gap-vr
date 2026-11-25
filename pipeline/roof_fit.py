"""Choose each building's roof form from its LiDAR roof heights.

For every candidate form (flat, hip, gables at different edges, skillion) the roof
height at a point is  z = eave + slope * min(D(p), cap)  where D is the form's distance
field (roofs.RoofFrame.distance_field) and `cap` optionally flattens the top.  Eave and
slope are fitted by trimmed least squares; the form with the lowest trimmed RMS wins,
with a small penalty per extra parameter so a simpler roof is kept unless the data
clearly prefers a more complex one.
"""
from __future__ import annotations

import math

import numpy as np

MIN_SLOPE = math.tan(math.radians(4))     # flatter than this counts as a flat roof
MAX_SLOPE = math.tan(math.radians(50))
PENALTY = 0.04                            # metres of RMS per extra parameter
TRIM = 0.1                                # ignore the worst 10% (chimneys, aerials, panels, noise)


def _trimmed_rms(r):
    a = np.sort(np.abs(r))
    a = a[: max(int(len(a) * (1 - TRIM)), 1)]
    return float(np.sqrt(np.mean(a ** 2)))


def _fit_line(X, z):
    def line(x, y):  # closed-form least squares for y = e + s x (avoids BLAS noise)
        xm, ym = x.mean(), y.mean()
        vx = ((x - xm) ** 2).sum()
        s = ((x - xm) * (y - ym)).sum() / vx if vx > 1e-12 else 0.0
        return ym - s * xm, s
    e, s = line(X, z)
    r = z - (e + s * X)
    if len(z) > 20:
        keep = np.abs(r) <= np.quantile(np.abs(r), 1 - TRIM)
        e, s = line(X[keep], z[keep])
        r = z - (e + s * X)
    return float(e), float(s), _trimmed_rms(r)


def fit_roof(frame, xy, z, area):
    """Best roof form for LiDAR samples (xy local metres, z m AHD).

    Returns dict(type, gables, eave, slope, cap, rms, flat_top)."""
    zf = float(np.median(z))
    best = dict(type="flat", gables=(), eave=zf, slope=0.0, cap=math.inf,
                rms=_trimmed_rms(z - zf), flat_top=False)
    best_score = best["rms"] + PENALTY * 1
    for kind, gables in frame.candidates(area):
        D = frame.distance_field(xy, gables)
        if not np.all(np.isfinite(D)):
            continue
        dmax = float(D.max())
        if dmax < 0.8:
            continue
        for q in (None, 0.5, 0.7, 0.85):
            cap = math.inf if q is None else q * dmax
            eave, slope, rms = _fit_line(np.minimum(D, cap), z)
            if not (MIN_SLOPE <= slope <= MAX_SLOPE):
                continue
            score = rms + PENALTY * (2 if q is None else 3)
            if score < best_score:
                best_score = score
                best = dict(type=kind, gables=gables, eave=eave, slope=slope, cap=cap, rms=rms,
                            flat_top=q is not None)
    return best
