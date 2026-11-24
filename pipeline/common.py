"""Shared helpers for the The Gap VR data pipeline.

All spatial work happens in GDA2020 / MGA zone 56 (EPSG:7856), in metres.
The viewer uses a local frame centred on the anchor point in config.json:
    x = easting  - anchor easting      (metres, east positive)
    y = elevation (AHD) - base_z       (metres, up positive)
    z = -(northing - anchor northing)  (metres, so north is -z, like three.js)
"""
from __future__ import annotations

import json
import os
import pathlib
import tempfile
import time

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
WEB_DATA = ROOT / "web" / "data"
CONFIG = json.loads((ROOT / "config.json").read_text())
# Large intermediate downloads (e.g. the 68 MB Microsoft buildings tile) go here,
# outside Dropbox by default. Override with the THEGAP_CACHE environment variable.
CACHE = pathlib.Path(os.environ.get("THEGAP_CACHE", pathlib.Path(tempfile.gettempdir()) / "the-gap-vr-cache"))

USER_AGENT = "the-gap-vr-pipeline/0.1 (open-data research prototype)"


def area_bounds(margin: float = 0.0):
    """(xmin, ymin, xmax, ymax) of the model square in EPSG:7856."""
    e0 = CONFIG["anchor"]["easting"]
    n0 = CONFIG["anchor"]["northing"]
    h = CONFIG["half_size_m"] + margin
    return (e0 - h, n0 - h, e0 + h, n0 + h)


def area_bounds_wgs84(margin: float = 0.0):
    """(south, west, north, east) in WGS84 degrees, covering the model square."""
    from pyproj import Transformer

    t = Transformer.from_crs("EPSG:7856", "EPSG:4326", always_xy=True)
    xmin, ymin, xmax, ymax = area_bounds(margin)
    lons, lats = [], []
    for x in (xmin, xmax):
        for y in (ymin, ymax):
            lon, lat = t.transform(x, y)
            lons.append(lon)
            lats.append(lat)
    return (min(lats), min(lons), max(lats), max(lons))


def http(method: str, url: str, *, retries: int = 5, timeout: int = 180, **kw):
    """HTTP request with a polite user agent and exponential backoff."""
    headers = kw.pop("headers", {})
    headers.setdefault("User-Agent", USER_AGENT)
    delay = 5.0
    last = None
    for attempt in range(retries):
        try:
            r = requests.request(method, url, headers=headers, timeout=timeout, **kw)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}: {r.text[:200]!r}"
        except requests.RequestException as exc:  # network hiccup
            last = repr(exc)
        if attempt < retries - 1:
            print(f"  retry {attempt + 1}/{retries - 1} in {delay:.0f}s ({last})")
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"{method} {url} failed: {last}")


def write_json(path: pathlib.Path, obj, compact: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        path.write_text(json.dumps(obj, separators=(",", ":")))
    else:
        path.write_text(json.dumps(obj, indent=2))
