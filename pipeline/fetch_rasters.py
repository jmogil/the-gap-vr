"""Download the LiDAR terrain model (DTM) and aerial orthophoto for the model square.

Sources (Queensland Government spatial services, ArcGIS ImageServer exportImage):
  * Elevation/QldDem  - latest public LiDAR DTM (Brisbane 2019 project here), 0.5-1 m
  * Basemaps/LatestStateProgram_AllUsers - latest public aerial imagery
    (Brisbane_LGA_2022_10cm_SISP here)
Both are requested directly in EPSG:7856 so pixels line up with the model grid.

Outputs:
  data/raw/dem/dem_<res>m.tif          float32 GeoTIFF, AHD metres
  data/raw/imagery/ortho_<px>.jpg      RGB JPEG covering the model square exactly
  data/raw/imagery/ortho_<px>.json     extent + source metadata
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CONFIG, RAW, area_bounds, http  # noqa: E402

MAX_W, MAX_H = 4096, 2048  # server limits (maxImageWidth / maxImageHeight)


def export_image(service, bbox, size, fmt, extra=None):
    params = {
        "bbox": ",".join(f"{v:.3f}" for v in bbox),
        "bboxSR": 7856,
        "imageSR": 7856,
        "size": f"{size[0]},{size[1]}",
        "format": fmt,
        "f": "image",
    }
    params.update(extra or {})
    r = http("GET", service + "/exportImage", params=params, timeout=300)
    ctype = r.headers.get("Content-Type", "")
    if "json" in ctype or "html" in ctype:
        raise RuntimeError(f"exportImage error: {r.text[:300]}")
    return r.content


def fetch_dem():
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    res = CONFIG["dem"]["resolution_m"]
    xmin, ymin, xmax, ymax = area_bounds(margin=10)
    w = int(round((xmax - xmin) / res))
    h = int(round((ymax - ymin) / res))
    out = np.full((h, w), np.nan, dtype=np.float32)
    # Tile the request to stay inside the server's size limits.
    step_x, step_y = MAX_W // 2, MAX_H // 2  # modest chunks (2048 x 1024 px)
    for r0 in range(0, h, step_y):
        for c0 in range(0, w, step_x):
            tw, th = min(step_x, w - c0), min(step_y, h - r0)
            bx = (xmin + c0 * res, ymax - (r0 + th) * res, xmin + (c0 + tw) * res, ymax - r0 * res)
            print(f"  DEM tile rows {r0}:{r0 + th} cols {c0}:{c0 + tw}")
            data = export_image(CONFIG["dem"]["service"], bx, (tw, th), "tiff", {
                "pixelType": "F32",
                "noData": -9999,
                "noDataInterpretation": "esriNoDataMatchAny",
                "interpolation": "RSP_BilinearInterpolation",
                "compression": "LZ77",
            })
            with rasterio.open(io.BytesIO(data)) as src:
                arr = src.read(1).astype(np.float32)
                if arr.shape != (th, tw):
                    raise RuntimeError(f"unexpected tile shape {arr.shape} != {(th, tw)}")
                arr[arr < -1000] = np.nan
                out[r0:r0 + th, c0:c0 + tw] = arr
    nan = int(np.isnan(out).sum())
    print(f"  DEM {w}x{h} @ {res} m, z {np.nanmin(out):.1f}..{np.nanmax(out):.1f} m AHD, {nan} nodata px")
    out_dir = RAW / "dem"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"dem_{res:g}m.tif"
    with rasterio.open(path, "w", driver="GTiff", width=w, height=h, count=1, dtype="float32",
                       crs="EPSG:7856", transform=from_origin(xmin, ymax, res, res),
                       nodata=np.nan, compress="deflate", predictor=3) as dst:
        dst.write(out, 1)
    print(f"  saved {path}")


def fetch_imagery():
    from PIL import Image

    px = CONFIG["imagery"]["size_px"]
    xmin, ymin, xmax, ymax = area_bounds()
    res = (xmax - xmin) / px
    mosaic = Image.new("RGB", (px, px))
    for r0 in range(0, px, MAX_H):
        for c0 in range(0, px, MAX_W):
            tw, th = min(MAX_W, px - c0), min(MAX_H, px - r0)
            bx = (xmin + c0 * res, ymax - (r0 + th) * res, xmin + (c0 + tw) * res, ymax - r0 * res)
            print(f"  imagery tile rows {r0}:{r0 + th} cols {c0}:{c0 + tw}")
            data = export_image(CONFIG["imagery"]["service"], bx, (tw, th), "jpg",
                                {"compressionQuality": 90, "interpolation": "RSP_BilinearInterpolation"})
            tile = Image.open(io.BytesIO(data)).convert("RGB")
            if tile.size != (tw, th):
                raise RuntimeError(f"unexpected tile size {tile.size}")
            mosaic.paste(tile, (c0, r0))
    out_dir = RAW / "imagery"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"ortho_{px}.jpg"
    mosaic.save(path, quality=90, optimize=True)
    meta = {"bounds_epsg7856": [xmin, ymin, xmax, ymax], "size_px": px, "resolution_m": res,
            "source": "Queensland Government, Brisbane_LGA_2022_10cm_SISP via LatestStateProgram_AllUsers"}
    (out_dir / f"ortho_{px}.json").write_text(json.dumps(meta, indent=2))
    print(f"  saved {path} ({res:.3f} m/px)")


if __name__ == "__main__":
    which = sys.argv[1:] or ["dem", "imagery"]
    if "dem" in which:
        print("Fetching LiDAR DTM ...")
        fetch_dem()
    if "imagery" in which:
        print("Fetching aerial imagery ...")
        fetch_imagery()
