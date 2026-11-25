"""Creeks: OpenStreetMap centrelines shaped by the LiDAR channel.

For every OSM waterway (streams/drains above ground; piped or culverted sections are
skipped) the centreline is
  1. snapped to the channel's lowest line (thalweg) in the LiDAR terrain,
  2. given a water level a set depth above that bed (shallower for intermittent creeks),
  3. given edges where each bank, in terrain cross-sections, rises above the water,
and turned into a water-surface ribbon that slopes down onto the bank at its edges. Intermittent creeks get a narrower trickle.
"""
from __future__ import annotations

import json

import numpy as np
import shapely
from scipy.ndimage import median_filter, uniform_filter1d
from shapely.geometry import LineString

from buildings import Local
from common import RAW

# kind: (min width, max width, water depth over the bed), metres
# (LiDAR gets no returns off water, so the DEM is interpolated across a creek at about its
#  surface: the water only needs to sit a little above it.)
KINDS = {"river": (4.0, 12.0, 0.25), "stream": (1.2, 5.0, 0.15), "canal": (2.0, 8.0, 0.2),
         "drain": (0.6, 2.0, 0.12), "ditch": (0.5, 1.5, 0.12)}


def _densify(line, step=1.0):
    n = max(2, int(np.ceil(line.length / step)) + 1)
    return np.array([line.interpolate(d).coords[0] for d in np.linspace(0, line.length, n)])


def build(terrain, H):
    local = Local.osm()
    osm = json.loads((RAW / "osm" / "osm.json").read_text())
    square = shapely.box(-H + 1, -H + 1, H - 1, H - 1)
    pos, nrm, uv, idx, feats = [], [], [], [], []
    nv = 0
    for e in osm["elements"]:
        t = e.get("tags", {})
        kind = t.get("waterway")
        if e["type"] != "way" or kind not in KINDS or len(e.get("geometry", [])) < 2:
            continue
        if t.get("tunnel") in ("yes", "culvert") or str(t.get("layer", "0")).startswith("-"):
            continue                                            # piped / underground
        line = LineString(local.ring([(p["lon"], p["lat"]) for p in e["geometry"]])).intersection(square)
        for part in getattr(line, "geoms", [line]):
            if part.geom_type != "LineString" or part.length < 10:
                continue
            p = _densify(part, 1.0)
            d = np.gradient(p, axis=0)
            d /= np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1e-9)
            nrm2 = np.column_stack([-d[:, 1], d[:, 0]])
            # 1. thalweg: lowest terrain within 6 m either side, smoothed along the creek
            offs = np.arange(-6.0, 6.01, 0.5)
            cs = terrain.height((p[:, None, 0] + nrm2[:, None, 0] * offs).ravel(),
                                (p[:, None, 1] + nrm2[:, None, 1] * offs).ravel()).reshape(len(p), len(offs))
            shift = uniform_filter1d(offs[np.argmin(cs, axis=1)], 9, mode="nearest")
            p = p + nrm2 * shift[:, None]
            bed = median_filter(terrain.height(p[:, 0], p[:, 1]), 7, mode="nearest")   # drop 1-cell pits
            lo, hi, depth = KINDS[kind]
            intermittent = t.get("intermittent") == "yes" or t.get("seasonal") == "yes" or t.get("name") == "Fish Creek"
            if intermittent:
                lo, hi, depth = lo * 0.6, hi * 0.4, depth * 0.75
            # 2. water level: a set depth over the bed, smoothed along the creek but never
            #    buried. (Filling pools from downstream was tried: road embankments over
            #    unmapped culverts dam the channel in the DEM and float the water metres up.)
            level = np.maximum(uniform_filter1d(bed + depth, 9, mode="nearest"), bed + depth * 0.6)
            # 3. each edge sits where that bank rises above the water, tucked 0.3 m under it
            offs2 = np.arange(0.25, hi / 2 + 0.01, 0.25)
            half = []
            for side in (1, -1):
                prof = terrain.height((p[:, None, 0] + side * nrm2[:, None, 0] * offs2).ravel(),
                                      (p[:, None, 1] + side * nrm2[:, None, 1] * offs2).ravel()).reshape(len(p), len(offs2))
                dry = prof > level[:, None]
                reach = np.where(dry.any(axis=1), offs2[np.argmax(dry, axis=1)] + 0.3, hi / 2)
                half.append(uniform_filter1d(np.clip(reach, lo / 2, hi / 2), 5, mode="nearest"))
            width = half[0] + half[1]
            # 4. five rows across (edges, 60 % points, centre line): flat water inside the
            #    60 % rows; an edge that would hang above lower ground is dropped onto it, so
            #    the surface slopes down to meet the bank instead of ending in a ledge
            k = len(p)
            along = np.r_[0, np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))]
            rows, uvs = [], []
            for f in (1.0, 0.6, 0.0, -0.6, -1.0):
                off = f * (half[0] if f > 0 else half[1])
                xy = p + nrm2 * off[:, None]
                z = np.minimum(level, terrain.height(xy[:, 0], xy[:, 1]) - 0.03) if abs(f) == 1 else level
                rows.append(np.column_stack([xy, z]))
                uvs.append(np.column_stack([along, -off]))
            v, uvs = np.concatenate(rows), np.concatenate(uvs)
            i = np.arange(k - 1)
            tri = np.concatenate([np.column_stack(c) for j in range(4)
                                  for c in (((j * k + i), (j + 1) * k + i, (j + 1) * k + i + 1),
                                            ((j * k + i), (j + 1) * k + i + 1, j * k + i + 1))])
            a, b, c = v[tri[:, 0]], v[tri[:, 1]], v[tri[:, 2]]
            up = (b - a)[:, 0] * (c - a)[:, 1] - (b - a)[:, 1] * (c - a)[:, 0]
            tri[up < 0] = tri[up < 0][:, [0, 2, 1]]
            pos.append(v)
            nrm.append(np.repeat([[0, 0, 1.0]], len(v), 0))
            uv.append(uvs)
            idx.append(tri + nv)
            nv += len(v)
            feats.append({"id": len(feats), "osm_id": f"way/{e['id']}", "name": t.get("name"), "kind": kind,
                          "intermittent": intermittent, "length_m": round(float(along[-1]), 1),
                          "width_m": [round(float(width.min()), 1), round(float(width.max()), 1)]})
    if not pos:
        return None, []
    fid = np.concatenate([np.full(len(pp), f["id"], np.float32) for pp, f in zip(pos, feats)])
    arr = dict(pos=np.concatenate(pos), nrm=np.concatenate(nrm), uv=np.concatenate(uv),
               idx=np.concatenate(idx), col=np.zeros((nv, 3)), fid=fid)
    total = sum(f["length_m"] for f in feats)
    print(f"  waterways: {len(feats)} creek sections, {total / 1000:.2f} km of water surface")
    return arr, feats
