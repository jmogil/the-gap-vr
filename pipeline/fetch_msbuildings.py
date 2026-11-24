"""Download Microsoft Global ML Building Footprints for the model square.

OSM only maps part of the houses around Yuruga Street, so these machine-learned
footprints (traced from aerial imagery) fill the gaps. The full z9 quadkey tile is
~70 MB and is cached outside the project; only the clipped subset is kept.

Source: https://github.com/microsoft/GlobalMLBuildingFootprints  (ODbL 1.0)
Output: data/raw/msbuildings/msbuildings.geojson
"""
import csv
import gzip
import io
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CACHE, RAW, area_bounds_wgs84, http  # noqa: E402

INDEX_URL = "https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv"
FIRST_COORD = re.compile(r'"coordinates":\s*\[\[\[\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)')


def quadkey(lat, lon, z=9):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    q = ""
    for i in range(z, 0, -1):
        m = 1 << (i - 1)
        q += str((1 if x & m else 0) + (2 if y & m else 0))
    return q


def main():
    s, w, n, e = area_bounds_wgs84(margin=60)
    keys = sorted({quadkey(la, lo) for la in (s, n) for lo in (w, e)})
    CACHE.mkdir(parents=True, exist_ok=True)
    idx_path = CACHE / "ms-dataset-links.csv"
    if not idx_path.exists():
        idx_path.write_bytes(http("GET", INDEX_URL).content)
    rows = [r for r in csv.DictReader(idx_path.open()) if r["Location"] == "Australia" and r["QuadKey"] in keys]
    if not rows:
        raise SystemExit(f"No Microsoft building tiles for quadkeys {keys}")
    feats = []
    for r in rows:
        gz_path = CACHE / f"ms-{r['QuadKey']}-{r['UploadDate']}.csv.gz"
        if not gz_path.exists():
            print(f"Downloading {r['Url']} ({r['Size']}) ...")
            gz_path.write_bytes(http("GET", r["Url"], timeout=600).content)
        print(f"Filtering {gz_path.name} ...")
        with gzip.open(gz_path, "rt") as fh:
            for line in fh:
                m = FIRST_COORD.search(line)
                if not m:
                    continue
                lon, lat = float(m.group(1)), float(m.group(2))
                if not (s <= lat <= n and w <= lon <= e):
                    continue
                f = json.loads(line)
                f.setdefault("properties", {})["ms_release"] = r["Url"].split("/")[5]
                feats.append(f)
    out_dir = RAW / "msbuildings"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = {"type": "FeatureCollection",
           "attribution": "Microsoft Global ML Building Footprints, ODbL 1.0",
           "features": feats}
    (out_dir / "msbuildings.geojson").write_text(json.dumps(out))
    heights = [f["properties"].get("height", -1) for f in feats]
    known = [h for h in heights if h is not None and h > 0]
    print(f"Kept {len(feats)} footprints; {len(known)} with a height estimate")


if __name__ == "__main__":
    main()
