"""Build the viewer data (web/data/) and exchange files (export/) from data/raw/.

Run after the fetch_* scripts:
    python pipeline/build_scene.py
"""
import datetime
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np  # noqa: E402
import shapely  # noqa: E402
from PIL import Image  # noqa: E402
from pyproj import Transformer  # noqa: E402
from shapely.geometry import LineString  # noqa: E402

import buildings  # noqa: E402
import roads  # noqa: E402
from common import CONFIG, RAW, ROOT, WEB_DATA, write_json  # noqa: E402
from glb import write_glb  # noqa: E402
from lidar_features import LidarGrids  # noqa: E402
from terrain import Terrain  # noqa: E402

Image.MAX_IMAGE_PIXELS = None

SOURCES = [
    {"layer": "Terrain", "name": "Queensland LiDAR DEM (Brisbane 2019 project, 0.5-1 m DTM) via QldDem image service",
     "licence": "CC BY-SA (per the service licence)", "credit": "© State of Queensland (Department of Natural Resources and Mines, "
     "Manufacturing and Regional and Rural Development)"},
    {"layer": "Aerial imagery", "name": "Brisbane_LGA_2022_10cm_SISP orthophoto via LatestStateProgram_AllUsers",
     "licence": "CC BY-SA (per the service licence)", "credit": "© State of Queensland (Department of Natural Resources and Mines, "
     "Manufacturing and Regional and Rural Development)"},
    {"layer": "Roads, OSM buildings, addresses", "name": "OpenStreetMap", "licence": "ODbL 1.0",
     "credit": "© OpenStreetMap contributors"},
    {"layer": "Gap-fill buildings + height estimates", "name": "Microsoft Global ML Building Footprints",
     "licence": "ODbL 1.0", "credit": "© Microsoft"},
]
LIDAR_SOURCE = {"layer": "Building heights, extra buildings, trees",
                "name": "Queensland LiDAR Data - Brisbane 2019 Project (classified point cloud, via ELVIS)",
                "licence": "CC BY 4.0", "credit": "© State of Queensland (Department of Resources); "
                "distributed by Geoscience Australia (ELVIS)"}


def to_three(pos_local, base_z):
    """(x, north, z_ahd) -> three.js (x, y_up, z_south)."""
    p = np.asarray(pos_local, dtype=np.float64)
    return np.column_stack([p[:, 0], p[:, 2] - base_z, -p[:, 1]]).astype(np.float32)


def nrm_three(n):
    n = np.asarray(n, dtype=np.float64)
    return np.column_stack([n[:, 0], n[:, 2], -n[:, 1]]).astype(np.float32)


def planar_uv(pos_local, H):
    p = np.asarray(pos_local)
    return np.column_stack([(p[:, 0] + H) / (2 * H), (H - p[:, 1]) / (2 * H)]).astype(np.float32)


def mesh_dict(name, arr, base_z, H, material):
    return {"name": name, "pos": to_three(arr["pos"], base_z), "nrm": nrm_three(arr["nrm"]),
            "col": arr["col"], "uv": planar_uv(arr["pos"], H), "fid": arr["fid"],
            "idx": arr["idx"], "material": material}


def terrain_mesh(terrain, base_z, H):
    V, T = terrain.vertices, terrain.triangles
    fn = np.cross(V[T[:, 1]] - V[T[:, 0]], V[T[:, 2]] - V[T[:, 0]])  # area-weighted
    vn = np.zeros_like(V)
    for k in range(3):
        np.add.at(vn, T[:, k], fn)
    vn /= np.linalg.norm(vn, axis=1, keepdims=True)
    return {"name": "terrain", "pos": to_three(V, base_z), "nrm": nrm_three(vn), "col": None,
            "uv": planar_uv(V, H), "fid": None, "idx": T,
            "material": {"name": "terrain", "roughness": 1.0, "textured": True}}


def focus_outline(terrain, H, local, osm):
    way_id = CONFIG["anchor"]["osm_way"]
    for e in osm["elements"]:
        if e["type"] == "way" and e["id"] == way_id:
            line = LineString(local.ring([(p["lon"], p["lat"]) for p in e["geometry"]]))
            break
    else:
        raise SystemExit("anchor street not found in OSM data")
    ring = line.buffer(CONFIG["focus_buffer_m"], quad_segs=32).exterior
    d = np.arange(0, ring.length, 4.0)
    pts = np.array([ring.interpolate(v).coords[0] for v in d])
    pts[:, 0] = np.clip(pts[:, 0], -H, H)
    pts[:, 1] = np.clip(pts[:, 1], -H, H)
    z = terrain.height(pts[:, 0], pts[:, 1])
    street = np.asarray(line.coords)
    return pts, z, street


def main():
    t0 = time.time()
    H = CONFIG["half_size_m"]
    WEB_DATA.mkdir(parents=True, exist_ok=True)
    (ROOT / "export").mkdir(exist_ok=True)

    print("Terrain ...")
    terrain = Terrain(max_error=0.08)
    base_z = round(float(terrain.height(0.0, 0.0)), 2)
    print(f"  TIN {len(terrain.vertices)} vertices / {len(terrain.triangles)} triangles; "
          f"z {terrain.zmin:.1f}..{terrain.zmax:.1f} m AHD; base_z {base_z}")

    lidar = None
    if LidarGrids.available():
        lidar = LidarGrids(terrain)
        print(f"  LiDAR grids: {lidar.n} x {lidar.n} cells at 0.5 m")
    else:
        print("  (no LiDAR grids - run prepare_lidar.py for measured heights and trees)")

    print("Buildings ...")
    walls, roofs_, bfeats = buildings.build(terrain, H, lidar)
    print("Roads ...")
    road_arr, rfeats, ways = roads.build(terrain, H)
    labels = roads.street_labels(ways, terrain, H)
    trees = []
    if lidar is not None:
        print("Trees ...")
        trees = lidar.trees([shapely.Polygon(b["footprint"]) for b in bfeats], buildings.OrthoSampler(H))
        print(f"  {len(trees)} trees from the canopy height model "
              f"(median height {np.median([t['h'] for t in trees]):.1f} m)")

    local = buildings.Local.osm()
    osm = json.loads((RAW / "osm" / "osm.json").read_text())
    ring, ring_z, street = focus_outline(terrain, H, local, osm)

    meshes = [
        terrain_mesh(terrain, base_z, H),
        mesh_dict("roads", road_arr, base_z, H, {"name": "roads", "roughness": 0.95}),
        mesh_dict("building_walls", walls, base_z, H, {"name": "walls", "roughness": 0.85}),
        mesh_dict("building_roofs", roofs_, base_z, H, {"name": "roofs", "roughness": 0.7, "double_sided": True}),
    ]

    to_ll = Transformer.from_crs("EPSG:7856", "EPSG:4326", always_xy=True)
    E0, N0 = CONFIG["anchor"]["easting"], CONFIG["anchor"]["northing"]
    lon0, lat0 = to_ll.transform(E0, N0)
    # Affine fit local (x, north) -> lon/lat over the square (sub-millimetre error at this size).
    gx, gn = np.meshgrid(np.linspace(-H, H, 5), np.linspace(-H, H, 5))
    glon, glat = to_ll.transform(E0 + gx.ravel(), N0 + gn.ravel())
    Amat = np.column_stack([np.ones(gx.size), gx.ravel(), gn.ravel()])
    geo_affine = {"lon": np.linalg.lstsq(Amat, glon, rcond=None)[0].tolist(),
                  "lat": np.linalg.lstsq(Amat, glat, rcond=None)[0].tolist()}
    meta = {
        "title": CONFIG["name"],
        "built": datetime.datetime.now().isoformat(timespec="seconds"),
        "crs": CONFIG["crs"], "crs_name": CONFIG["crs_name"],
        "anchor": {"easting": E0, "northing": N0, "lon": round(lon0, 7), "lat": round(lat0, 7),
                   "description": CONFIG["anchor"]["description"]},
        "frame": "three.js: x = easting - anchor, y = AHD - base_z, z = -(northing - anchor)",
        "geo_affine": geo_affine,
        "half_size_m": H, "base_z_ahd": base_z, "focus_buffer_m": CONFIG["focus_buffer_m"],
        "terrain": {"file": "terrain_1m.u16", "size": terrain.n, "spacing_m": 1.0,
                    "zmin_ahd": round(terrain.zmin, 3), "scale": 0.01,
                    "note": "uint16 little-endian, row 0 = north edge, value*scale + zmin = m AHD"},
        "imagery": {"files": {"1024": "ortho_1024.jpg", "4096": "ortho_4096.jpg", "8192": "ortho_8192.jpg"},
                    "bounds_local": [-H, -H, H, H]},
        "focus_outline": [[round(float(x), 2), round(float(z - base_z + 0.6), 2), round(float(-n), 2)]
                          for (x, n), z in zip(ring, ring_z)],
        "focus_street": [[round(float(x), 2), round(float(-n), 2)] for x, n in street],
        "counts": {"buildings": len(bfeats), "roads": len(rfeats), "labels": len(labels),
                   "terrain_triangles": int(len(terrain.triangles)), "trees": len(trees),
                   "lidar_heights": sum(1 for b in bfeats if b["height_source"].startswith("LiDAR")),
                   "lidar_outlines": sum(1 for b in bfeats if b["source"].startswith("LiDAR"))},
        "alignment": {"osm_offset_m": CONFIG.get("osm_offset_m", {}).get("east_north", [0, 0]),
                      "note": CONFIG.get("osm_offset_m", {}).get("note", "")},
        "sources": SOURCES + ([LIDAR_SOURCE] if lidar is not None else []),
    }

    print("Writing web/data ...")
    write_glb(WEB_DATA / "scene.glb", meshes, extras=meta)
    (WEB_DATA / "terrain_1m.u16").write_bytes(terrain.heightmap_u16().tobytes())
    write_json(WEB_DATA / "scene.json", meta, compact=False)
    for b in bfeats:  # local -> three.js for footprints used by collision + picking
        b["footprint"] = [[x, -n] for x, n in b["footprint"]]
        b["centroid"] = [b["centroid"][0], -b["centroid"][1]]
    lab3 = [{"text": l["text"], "x": l["x"], "y": round(l["z"] - base_z, 2), "z": -l["n"],
             "angle": l["angle"]} for l in labels]
    write_json(WEB_DATA / "features.json", {"buildings": bfeats, "roads": rfeats, "labels": lab3})
    rows = [[round(t["x"], 2), round(t["ground"] - base_z, 2), round(-t["n"], 2), round(t["h"], 1),
             round(t["r"], 1), int(t["on_roof"]),
             "%02x%02x%02x" % tuple(int(v * 255) for v in t["colour"]) if t["colour"] is not None else ""]
            for t in trees]
    write_json(WEB_DATA / "trees.json", {
        "fields": ["x", "y_ground", "z", "height_m", "crown_radius_m", "over_roof", "colour"],
        "source": "Treetops and crowns from the Brisbane 2019 LiDAR canopy height model (classes 3-5)",
        "trees": rows})

    px = CONFIG["imagery"]["size_px"]
    ortho = Image.open(RAW / "imagery" / f"ortho_{px}.jpg").convert("RGB")
    ortho.save(WEB_DATA / "ortho_8192.jpg", quality=82, optimize=True, progressive=True)
    o4 = ortho.resize((4096, 4096), Image.LANCZOS)
    o4.save(WEB_DATA / "ortho_4096.jpg", quality=85, optimize=True, progressive=True)
    ortho.resize((1024, 1024), Image.LANCZOS).save(WEB_DATA / "ortho_1024.jpg", quality=85, optimize=True)

    print("Writing export/ ...")
    buf = io.BytesIO()
    o4.save(buf, format="JPEG", quality=85)
    exp_meshes = [dict(m) for m in meshes]
    for m in exp_meshes:
        if m["name"] == "building_roofs":
            m["material"] = dict(m["material"], textured=False)
    write_glb(ROOT / "export" / "the_gap_yuruga_st.glb", exp_meshes, extras=meta, image_jpeg=buf.getvalue())
    to_ll_local = Transformer.from_crs("EPSG:7856", "EPSG:4326", always_xy=True)
    gj = {"type": "FeatureCollection", "features": []}
    for b in bfeats:
        ring_ll = [list(np.round(to_ll_local.transform(E0 + x, N0 - z), 7)) for x, z in b["footprint"]]
        ring_ll.append(ring_ll[0])
        props = {k: v for k, v in b.items() if k not in ("footprint", "centroid")}
        props["addresses"] = "; ".join(props["addresses"])
        gj["features"].append({"type": "Feature", "properties": props,
                               "geometry": {"type": "Polygon", "coordinates": [ring_ll]}})
    write_json(ROOT / "export" / "buildings.geojson", gj)
    if trees:
        lines = ["x_east_m,y_north_m,ground_ahd_m,height_m,crown_radius_m,lon,lat"]
        for t in trees:
            lon, lat = to_ll_local.transform(E0 + t["x"], N0 + t["n"])
            lines.append(f"{t['x']:.2f},{t['n']:.2f},{t['ground']:.2f},{t['h']:.1f},{t['r']:.1f},{lon:.7f},{lat:.7f}")
        (ROOT / "export" / "trees.csv").write_text("\n".join(lines) + "\n")

    sizes = {p.name: f"{p.stat().st_size / 1e6:.1f} MB" for p in sorted(WEB_DATA.iterdir())}
    print(f"  {sizes}")
    print(f"Done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
