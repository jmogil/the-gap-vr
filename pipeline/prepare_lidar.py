"""Unpack the ELVIS LiDAR order, keep compressed LAZ copies, and rasterise the points.

Usage:
    python pipeline/prepare_lidar.py /path/to/DATA_<job>.zip      # ELVIS package
    python pipeline/prepare_lidar.py ~/Library/Caches/the-gap-vr/lidar   # or a folder of LAZ/LAS tiles

The ELVIS package (Brisbane 2019 project, QLD Government, classified LAS in AHD) is
a zip of per-tile zips. Each tile is streamed once:
  * a LAZ copy is written to the LiDAR cache (outside Dropbox, ~5x smaller than LAS)
  * points inside the model square are binned into 0.5 m grids:
        bld_max  highest building return (class 6)        -> roof heights
        veg_max  highest vegetation return (classes 3-5)  -> tree canopy
        gnd_mean mean ground return (class 2)             -> alignment check vs the DTM
        n_bld, n_veg, n_gnd point counts
  * unclassified returns (class 1: power lines, poles, street lights) are kept as points
Output: data/derived/lidar_grids_0.5m.npz, lidar_unclassified.npz, lidar_meta.json (in the project).
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import laspy  # noqa: E402
import numpy as np  # noqa: E402
from pyproj import CRS, Transformer  # noqa: E402

from common import CONFIG, ROOT  # noqa: E402

RES = 0.5
LIDAR_CACHE = pathlib.Path(os.environ.get("THEGAP_LIDAR", pathlib.Path.home() / "Library/Caches/the-gap-vr/lidar"))
DERIVED = ROOT / "data" / "derived"


def unpack(package: pathlib.Path, work: pathlib.Path):
    """Yield paths of .las/.laz files from the (possibly nested) ELVIS zip."""
    with zipfile.ZipFile(package) as outer:
        for info in outer.infolist():
            name = pathlib.PurePosixPath(info.filename).name
            if info.is_dir() or not name:
                continue
            low = name.lower()
            if low.endswith(".zip"):
                inner_path = work / name
                with outer.open(info) as src, open(inner_path, "wb") as dst:
                    shutil.copyfileobj(src, dst, 16 << 20)
                with zipfile.ZipFile(inner_path) as inner:
                    for ii in inner.infolist():
                        nn = pathlib.PurePosixPath(ii.filename).name
                        if nn.lower().endswith((".las", ".laz")):
                            out = work / nn
                            with inner.open(ii) as s, open(out, "wb") as d:
                                shutil.copyfileobj(s, d, 16 << 20)
                            yield out
                inner_path.unlink()
            elif low.endswith((".las", ".laz")):
                out = work / name
                with outer.open(info) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst, 16 << 20)
                yield out


def sources(path: pathlib.Path, work: pathlib.Path):
    """(file, is_temporary) for every point-cloud tile in a folder or ELVIS zip."""
    if path.is_dir():
        for f in sorted(list(path.glob("*.laz")) + list(path.glob("*.las"))):
            yield f, False
    else:
        for f in unpack(path, work):
            yield f, True


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    package = pathlib.Path(sys.argv[1]).expanduser().resolve()
    H = CONFIG["half_size_m"]
    E0, N0 = CONFIG["anchor"]["easting"], CONFIG["anchor"]["northing"]
    n = int(round(2 * H / RES))
    shape = (n, n)  # row 0 = north edge; cell centres at -H + (c + 0.5) * RES
    bld_max = np.full(shape, -np.inf, np.float32)
    veg_max = np.full(shape, -np.inf, np.float32)
    gnd_sum = np.zeros(shape, np.float64)
    n_bld = np.zeros(shape, np.uint16)
    n_veg = np.zeros(shape, np.uint16)
    n_gnd = np.zeros(shape, np.uint16)
    class_hist = np.zeros(256, np.int64)
    unclassified = []
    tiles, crs_seen = [], set()
    LIDAR_CACHE.mkdir(parents=True, exist_ok=True)
    DERIVED.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with tempfile.TemporaryDirectory(dir=LIDAR_CACHE) as tmp:
        for las_path, temporary in sources(package, pathlib.Path(tmp)):
            with laspy.open(las_path) as reader:
                hdr = reader.header
                try:
                    crs = hdr.parse_crs()
                except Exception:  # missing or unparsable CRS VLR
                    crs = None
                epsg = crs.to_epsg() if crs is not None else None
                crs_seen.add(f"EPSG:{epsg}" if epsg else (crs.name if crs is not None else "unknown"))
                # Model frame is GDA2020 / MGA56. Older QLD LiDAR is GDA94 / MGA56 (~1.5 m apart here).
                if epsg in (None, 7856):
                    dx = dy = 0.0
                else:
                    t = Transformer.from_crs(CRS.from_epsg(epsg), "EPSG:7856", always_xy=True)
                    cx, cy = (hdr.mins[0] + hdr.maxs[0]) / 2, (hdr.mins[1] + hdr.maxs[1]) / 2
                    ex, ey = t.transform(cx, cy)
                    dx, dy = ex - cx, ey - cy  # constant shift is exact to mm over a 1 km tile
                laz_path = LIDAR_CACHE / (las_path.stem + ".laz")
                writer = None if (not temporary or laz_path.exists()) else \
                    laspy.open(laz_path, mode="w", header=hdr, do_compress=True)
                npts = 0
                for pts in reader.chunk_iterator(5_000_000):
                    npts += len(pts)
                    if writer is not None:
                        writer.write_points(pts)
                    cls = np.asarray(pts.classification, dtype=np.uint8)
                    class_hist += np.bincount(cls, minlength=256)
                    x = np.asarray(pts.x) + dx - E0
                    y = np.asarray(pts.y) + dy - N0
                    z = np.asarray(pts.z, dtype=np.float32)
                    c = np.floor((x + H) / RES).astype(np.int64)
                    r = np.floor((H - y) / RES).astype(np.int64)
                    ok = (c >= 0) & (c < n) & (r >= 0) & (r < n)
                    flat = r * n + c
                    for mask, grid, cnt in (
                        (ok & (cls == 6), bld_max, n_bld),
                        (ok & ((cls == 3) | (cls == 4) | (cls == 5)), veg_max, n_veg),
                    ):
                        f = flat[mask]
                        np.maximum.at(grid.ravel(), f, z[mask])
                        np.add.at(cnt.ravel(), f, 1)
                    u = ok & (cls == 1)
                    unclassified.append(np.column_stack([x[u], y[u], z[u]]).astype(np.float32))
                    g = ok & (cls == 2)
                    np.add.at(gnd_sum.ravel(), flat[g], z[g])
                    np.add.at(n_gnd.ravel(), flat[g], 1)
                if writer is not None:
                    writer.close()
                tiles.append({"file": las_path.name, "points": int(npts), "crs": sorted(crs_seen)[-1],
                              "shift_to_gda2020_m": [round(dx, 3), round(dy, 3)],
                              "point_format": int(hdr.point_format.id), "las_version": str(hdr.version)})
                print(f"  {las_path.name}: {npts / 1e6:.1f} M points, CRS {tiles[-1]['crs']}, "
                      f"shift ({dx:.2f}, {dy:.2f}) m  [{time.time() - t0:.0f}s]")
            if temporary:
                las_path.unlink()

    gnd_mean = np.where(n_gnd > 0, gnd_sum / np.maximum(n_gnd, 1), np.nan).astype(np.float32)
    bld_max[~np.isfinite(bld_max)] = np.nan
    veg_max[~np.isfinite(veg_max)] = np.nan

    def q(a):  # centimetres AHD as uint16 (0 = no data) -> compact, lossless enough
        return np.where(np.isfinite(a), np.clip(np.round(a * 100), 1, 65535), 0).astype(np.uint16)

    pts1 = np.concatenate(unclassified) if unclassified else np.zeros((0, 3), np.float32)
    np.savez_compressed(DERIVED / "lidar_unclassified.npz", xyz=pts1)   # local x, north, z AHD
    np.savez_compressed(DERIVED / "lidar_grids_0.5m.npz", bld_max_cm=q(bld_max), veg_max_cm=q(veg_max),
                        gnd_mean_cm=q(gnd_mean), n_bld=n_bld, n_veg=n_veg, n_gnd=n_gnd)
    names = {0: "never classified", 1: "unclassified", 2: "ground", 3: "low vegetation", 4: "medium vegetation",
             5: "high vegetation", 6: "building", 7: "low noise", 9: "water", 10: "rail", 11: "road surface",
             13: "wire guard", 14: "wire conductor", 15: "transmission tower", 17: "bridge deck", 18: "high noise"}
    meta = {
        "source": "Queensland LiDAR Data - Brisbane 2019 Project (QLD Government), via ELVIS (Geoscience Australia)",
        "elvis_package": package.name, "tiles": tiles, "grid_resolution_m": RES,
        "grid_note": "uint16 centimetres AHD, 0 = no data; row 0 = north edge; same square as config.json",
        "class_counts": {f"{k} {names.get(k, '')}".strip(): int(v) for k, v in enumerate(class_hist) if v},
        "laz_cache": str(LIDAR_CACHE).replace(str(pathlib.Path.home()), "~", 1),  # no usernames in the repo
    }
    (DERIVED / "lidar_meta.json").write_text(json.dumps(meta, indent=2))
    total = sum(t["points"] for t in tiles)
    print(f"Done: {len(tiles)} tiles, {total / 1e6:.0f} M points in {time.time() - t0:.0f}s")
    print("Classes:", meta["class_counts"])
    print(f"Grids -> {DERIVED / 'lidar_grids_0.5m.npz'}; LAZ copies -> {LIDAR_CACHE}")


if __name__ == "__main__":
    main()
