"""Download Queensland cadastral parcels (DCDB) for the model square.

Source: Queensland Government, PlanningCadastre/LandParcelPropertyFramework MapServer,
layer 4 "Cadastral parcels" (Digital Cadastral Database, updated nightly), CC BY 4.0.
Output: data/raw/cadastre/parcels.geojson with coordinates in EPSG:7856 (GDA2020 / MGA56).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import RAW, area_bounds, http  # noqa: E402

SERVICE = ("https://spatial-gis.information.qld.gov.au/arcgis/rest/services/"
           "PlanningCadastre/LandParcelPropertyFramework/MapServer/4/query")
FIELDS = "lot,plan,lotplan,tenure,lot_area,excl_area,parcel_typ,cover_typ,locality,feat_name,alias_name,acc_code"


def main():
    xmin, ymin, xmax, ymax = area_bounds(margin=30)
    feats, offset = [], 0
    while True:
        r = http("GET", SERVICE, params={
            "geometry": f"{xmin},{ymin},{xmax},{ymax}", "geometryType": "esriGeometryEnvelope",
            "inSR": 7856, "spatialRel": "esriSpatialRelIntersects", "outFields": FIELDS,
            "returnGeometry": "true", "outSR": 7856, "resultOffset": offset, "resultRecordCount": 2000,
            "f": "geojson"})
        page = r.json()
        if "error" in page:
            raise SystemExit(f"cadastre query failed: {page['error']}")
        feats += page.get("features", [])
        if not page.get("exceededTransferLimit") and len(page.get("features", [])) < 2000:
            break
        offset += 2000
    out_dir = RAW / "cadastre"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = {"type": "FeatureCollection", "crs_note": "coordinates are EPSG:7856 (GDA2020 / MGA zone 56)",
           "attribution": "© State of Queensland (Department of Resources), DCDB, CC BY 4.0", "features": feats}
    (out_dir / "parcels.geojson").write_text(json.dumps(out))
    print(f"Saved {len(feats)} parcels -> {out_dir / 'parcels.geojson'}")


if __name__ == "__main__":
    main()
