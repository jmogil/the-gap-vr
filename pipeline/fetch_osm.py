"""Download all OpenStreetMap data for the model square via the Overpass API.

Output: data/raw/osm/osm.json (Overpass JSON with inline geometry).
OpenStreetMap data (c) OpenStreetMap contributors, ODbL 1.0.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CONFIG, RAW, area_bounds_wgs84, http  # noqa: E402


def main():
    s, w, n, e = area_bounds_wgs84(margin=40)
    bbox = f"{s:.6f},{w:.6f},{n:.6f},{e:.6f}"
    query = f"""
[out:json][timeout:180][bbox:{bbox}];
(way; node(if:count_tags() > 0);)->.a;
.a out tags geom qt;
(relation["type"="multipolygon"]; relation["type"="building"];)->.r;
.r out tags geom({bbox}) qt;
"""
    out_dir = RAW / "osm"
    out_dir.mkdir(parents=True, exist_ok=True)
    last_err = None
    for endpoint in CONFIG["overpass_endpoints"]:
        print(f"Overpass: {endpoint}")
        try:
            r = http("POST", endpoint, data={"data": query}, retries=2, timeout=240,
                     headers={"Accept": "application/json"})
            if not r.text.lstrip().startswith("{"):
                raise RuntimeError(r.text[:200])
            (out_dir / "osm.json").write_bytes(r.content)
            print(f"  saved {len(r.content) / 1e6:.1f} MB -> {out_dir / 'osm.json'}")
            return
        except Exception as exc:  # try the next mirror
            last_err = exc
            print(f"  failed: {exc}")
    raise SystemExit(f"All Overpass endpoints failed: {last_err}")


if __name__ == "__main__":
    main()
