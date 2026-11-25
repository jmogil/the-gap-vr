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
import streetscape  # noqa: E402
import waterways  # noqa: E402
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
    {"layer": "Roads, creeks, OSM buildings, addresses, street furniture", "name": "OpenStreetMap", "licence": "ODbL 1.0",
     "credit": "© OpenStreetMap contributors"},
    {"layer": "Gap-fill buildings + height estimates", "name": "Microsoft Global ML Building Footprints",
     "licence": "ODbL 1.0", "credit": "© Microsoft"},
]
CADASTRE_SOURCE = {"layer": "Lots and fences", "name": "Queensland Digital Cadastral Database (DCDB) parcels",
                   "licence": "CC BY 4.0", "credit": "© State of Queensland (Department of Natural Resources and Mines, "
                   "Manufacturing and Regional and Rural Development)"}
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


def mesh_dict(name, arr, base_z, H, material, uv=True, extra=None):
    if isinstance(uv, str):          # "own": the mesh carries its own UVs (e.g. fence palings in metres)
        uvs = arr["uv"]
    else:
        uvs = planar_uv(arr["pos"], H) if uv else None
    plain = material.get("plain", False)      # single-colour meshes (kerbs, fences): no colours / ids
    return {"name": name, "pos": to_three(arr["pos"], base_z), "nrm": nrm_three(arr["nrm"]),
            "col": None if plain else arr["col"], "uv": uvs, "fid": None if plain else arr["fid"],
            "idx": arr["idx"], "material": material, "extra_attrs": extra}


def _yaw(bearing):
    """Compass bearing (rad, clockwise from north) -> three.js rotation.y."""
    return round(-float(bearing), 4)


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


def start_view(ways):
    """Walk-mode start: the point on a named street nearest the model centre, facing along it."""
    best = None
    for w in ways:
        if w["group"] not in ("major", "minor") or not w["tags"].get("name"):
            continue
        d = w["line"].distance(shapely.Point(0, 0))
        if best is None or d < best[0]:
            best = (d, w["line"])
    if best is None:
        return {"x": 0.0, "z": 0.0, "yaw": 0.0}
    line = best[1]
    s0 = line.project(shapely.Point(0, 0))
    p = np.array(line.interpolate(s0).coords[0])
    q = np.array(line.interpolate(min(s0 + 3, line.length)).coords[0])
    if np.allclose(p, q):
        q = p + 1e-3
        p = np.array(line.interpolate(max(s0 - 3, 0)).coords[0])
    dx, dn = q - p
    return {"x": round(float(p[0]), 2), "z": round(float(-p[1]), 2),
            "yaw": round(float(np.arctan2(-dx, dn)), 4)}   # three.js yaw: 0 = north (-z)


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
    road_arr, rfeats, ways, road_extras = roads.build(terrain, H)
    labels = roads.street_labels(ways, terrain, H)
    trees = []
    if lidar is not None:
        print("Trees ...")
        trees = lidar.trees([shapely.Polygon(b["footprint"]) for b in bfeats], buildings.OrthoSampler(H))
        print(f"  {len(trees)} trees from the canopy height model "
              f"(median height {np.median([t['h'] for t in trees]):.1f} m)")

    start = start_view(ways)

    print("Waterways ...")
    water_arr, wfeats = waterways.build(terrain, H)

    print("Streetscape ...")
    bpolys = [shapely.Polygon(b["footprint"]) for b in bfeats]
    lots, lot_lines, fence_arr = streetscape.lots_and_fences(terrain, H, bpolys)
    kerb_arr = streetscape.kerbs(terrain, H, road_extras["carriageway"], road_extras["driveways"])
    poles, wires = streetscape.poles_and_wires(
        terrain, H, [w["line"] for w in ways if w["group"] in ("major", "minor")], bpolys)
    signals, signs, stops, benches, name_signs = streetscape.furniture(terrain, H, ways)

    def gy(x, n):
        return round(float(terrain.height(x, n)) - base_z, 2)

    street = {
        "note": "Poles are detected in the 2019 LiDAR; street lights are assumed on poles near streets. "
                "Signals, signs, bus stops and benches are OpenStreetMap nodes; street-name signs are placed "
                "at intersections of named OSM streets.",
        "poles": [[round(p["x"], 2), gy(p["x"], p["n"]), round(-p["n"], 2), round(p["h"], 1), int(p["light"]),
                   _yaw(p["arm"]), _yaw(p["cross"])] for p in poles],
        "wires": [[round(a, 2), round(za - base_z, 2), round(-b, 2), round(c, 2), round(zb - base_z, 2), round(-d, 2)]
                  for a, b, za, c, d, zb in wires],
        "signals": [[round(v["x"], 2), gy(v["x"], v["n"]), round(-v["n"], 2), _yaw(v["yaw"]), v["osm"]] for v in signals],
        "signs": [[v["type"], round(v["x"], 2), gy(v["x"], v["n"]), round(-v["n"], 2), _yaw(v["yaw"]), v["osm"]]
                  for v in signs],
        "bus_stops": [[round(v["x"], 2), gy(v["x"], v["n"]), round(-v["n"], 2), _yaw(v["yaw"]), v["osm"], v["name"]]
                      for v in stops],
        "benches": [[round(v["x"], 2), gy(v["x"], v["n"]), round(-v["n"], 2), _yaw(v["yaw"]), v["osm"]] for v in benches],
        "name_signs": [{"x": round(v["x"], 2), "y": gy(v["x"], v["n"]), "z": round(-v["n"], 2),
                        "blades": [[nm, _yaw(b)] for nm, b in v["blades"]]} for v in name_signs],
    }

    meshes = [
        terrain_mesh(terrain, base_z, H),
        mesh_dict("roads", road_arr, base_z, H, {"name": "roads", "roughness": 0.95}),
        mesh_dict("building_walls", walls, base_z, H, {"name": "walls", "roughness": 0.85}, uv=False,
                  extra={"_FACADE": walls["fac"]}),
        mesh_dict("building_roofs", roofs_, base_z, H, {"name": "roofs", "roughness": 0.7, "double_sided": True}),
    ]
    if water_arr is not None:
        meshes.append(mesh_dict("water", water_arr, base_z, H, {"name": "water", "roughness": 0.1}, uv="own"))
    if kerb_arr is not None:
        meshes.append(mesh_dict("kerbs", kerb_arr, base_z, H, {"name": "kerbs", "roughness": 0.9, "plain": True}, uv=False))
    if fence_arr is not None:
        meshes.append(mesh_dict("fences", fence_arr, base_z, H, {"name": "fences", "roughness": 0.95, "plain": True,
                                                                 "double_sided": True}, uv="own"))

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
        "half_size_m": H, "base_z_ahd": base_z, "start": start,
        "terrain": {"file": "terrain_1m.u16", "size": terrain.n, "spacing_m": 1.0,
                    "zmin_ahd": round(terrain.zmin, 3), "scale": 0.01,
                    "note": "uint16 little-endian, row 0 = north edge, value*scale + zmin = m AHD"},
        "imagery": {"files": {"1024": "ortho_1024.jpg", "4096": "ortho_4096.jpg", "8192": "ortho_8192.jpg"},
                    "bounds_local": [-H, -H, H, H]},
        "counts": {"buildings": len(bfeats), "roads": len(rfeats), "labels": len(labels),
                   "terrain_triangles": int(len(terrain.triangles)), "trees": len(trees),
                   "lidar_heights": sum(1 for b in bfeats if b["height_source"].startswith("LiDAR")),
                   "lidar_outlines": sum(1 for b in bfeats if b["source"].startswith("LiDAR")),
                   "lots": len(lots), "poles": len(poles), "waterways": len(wfeats), "wires": len(wires), "signals": len(signals),
                   "signs": len(signs), "bus_stops": len(stops), "name_signs": len(name_signs)},
        "alignment": {"osm_offset_m": CONFIG.get("osm_offset_m", {}).get("east_north", [0, 0]),
                      "note": CONFIG.get("osm_offset_m", {}).get("note", "")},
        "sources": SOURCES + [CADASTRE_SOURCE] + ([LIDAR_SOURCE] if lidar is not None else []),
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
    write_json(WEB_DATA / "features.json", {"buildings": bfeats, "roads": rfeats, "labels": lab3,
                                            "waterways": wfeats})
    write_json(WEB_DATA / "lots.json", {"source": CADASTRE_SOURCE["name"], "lots": lots, "boundaries": lot_lines})
    write_json(WEB_DATA / "streetscape.json", street)
    write_json(ROOT / "export" / "streetscape.json", street, compact=False)
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
    write_glb(ROOT / "export" / "the_gap_3d.glb", exp_meshes, extras=meta, image_jpeg=buf.getvalue())
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
