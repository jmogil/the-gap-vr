# The Gap 3D

An interactive 3D model of a 1.2 km × 1.2 km area of The Gap (Brisbane, QLD), built entirely from open data. You can orbit it like a map, walk it at street level, or stand in it in VR (WebXR, e.g. Meta Quest 3).

| Layer | What's in it | Source |
|---|---|---|
| Terrain | LiDAR bare-earth model (1 m grid, adaptive mesh within 8 cm), draped with 2022 aerial imagery | QLD LiDAR DEM (Brisbane 2019) + Brisbane 2022 10 cm orthophoto |
| Roads & paths | 300 OSM ways buffered to carriageway width and draped exactly on the terrain; 13 bridges built as decks; cul-de-sac bulbs; 60 OSM driveways in concrete; 28 km of 15 cm kerbs along every carriageway edge | OpenStreetMap |
| Buildings | 1,575 buildings. Heights and **roof shapes come from the 2019 LiDAR** for 1,564 of them: 365 simple roofs are fitted as clean flat, hip, gable or skillion forms, and 1,199 complex roofs use the measured roof surface. Roof colours come from the aerial photo, walls get windows per storey, homes get a front door facing the street, and addresses are shown where known. | Outlines: OSM (534), Microsoft ML (550), traced from LiDAR (491, mostly sheds, garages and houses under canopy) |
| Trees | 12,599 trees with height and crown width from the LiDAR canopy, crown colour from the aerial photo | Brisbane 2019 LiDAR point cloud (202 M points) |
| Lots & fences | 1,430 cadastral parcels (click the ground for lot/plan, tenure and area) and 49 km of 1.8 m timber fences along boundaries shared by neighbouring lots | QLD Digital Cadastral Database (DCDB) |
| Poles, lights & signs | 401 power poles and 173 wire spans detected in the LiDAR, street lights on 193 street-side poles; traffic signals at 15 locations, 5 give-way/stop signs, 13 bus stops, benches; 64 street-name signs at intersections | LiDAR 2019 + OpenStreetMap |

## View it

```bash
./serve.command          # or: python3 serve.py
```

Then open <http://localhost:8777>. It has to be served over HTTP; opening `index.html` directly won't work. `serve.py` turns off browser caching, so a rebuilt model shows up on a normal reload. Three.js is loaded from a CDN, so you need to be online.

**Controls**
- **Orbit:** drag to rotate, right-drag to pan, scroll to zoom. Click a building, road, tree, pole, sign or the ground (for the land parcel) to see its details. Double-click the ground to walk there.
- **Walk:** W A S D or arrow keys to move, Shift to run, click to capture the mouse, Esc to release. F toggles fly mode (E/Q up and down). Keys 1 and 2 switch between orbit and walk.
- **VR:** left stick walks, right stick snap-turns 30°, trigger aims and releasing teleports. You stay on the terrain and can't walk through buildings.
- The panel has layer toggles: buildings, roads (with kerbs), trees, fences, poles/lights/signs, lot boundaries, aerial photo, photo roofs. It also has a sun-position slider (real solar position for The Gap, AEST) and **Colour buildings by height source**.
- The HUD shows your position in MGA zone 56, latitude/longitude and ground elevation (AHD).

### On a Meta Quest 3
WebXR needs a secure origin, and `localhost` counts. Either:
1. Connect the headset by USB with developer mode on, run `adb reverse tcp:8777 tcp:8777`, then open `http://localhost:8777` in the Quest Browser and press **VR**; or
2. Copy the `web/` folder to any HTTPS static host (GitHub Pages, Netlify, university web space).

*The VR mode has been written but not yet tested on a headset; only the desktop modes have been checked. The scene is now about 1.8 M triangles. If the Quest stutters, turn off shadows, trees or fences first.*

### In Blender / Unity / Unreal
`export/the_gap_3d.glb` holds the terrain, roads, kerbs, buildings and fences, with the aerial texture embedded. It is Y-up, in metres, with the origin at the model centre (MGA56 E 494611, N 6964541). y is height above 52.86 m AHD and north is −z. Measured roofs are faceted, so use flat shading for the roof mesh (Blender: *Shade Flat*; Unity: import with a smoothing angle of 0). Walls carry UV-like facade data (`_FACADE`: distance along wall, height above ground, wall length, eave height) for your own window shaders.

Other files in `export/`:
- `trees.csv`: every tree.
- `streetscape.json`: poles, wires, signals, signs, bus stops, benches and street-name signs, with positions and orientations.
- `buildings.geojson`: building footprints and attributes for QGIS.

## Data sources and licences

| Data | Licence | Credit |
|---|---|---|
| LiDAR point cloud: Queensland LiDAR Data, Brisbane 2019 Project (classified, AHD), via ELVIS | CC BY 4.0 | © State of Queensland (Department of Resources); distributed by Geoscience Australia |
| LiDAR DEM: Brisbane 2019 project, via the QLD `Elevation/QldDem` image service | CC BY-SA (stated in the service's licence info) | © State of Queensland (Department of Natural Resources and Mines, Manufacturing and Regional and Rural Development) |
| Aerial imagery: `Brisbane_LGA_2022_10cm_SISP`, via `Basemaps/LatestStateProgram_AllUsers` | CC BY-SA (stated in the service's licence info) | © State of Queensland |
| Cadastral parcels: Digital Cadastral Database via `PlanningCadastre/LandParcelPropertyFramework` | CC BY 4.0 | © State of Queensland (Department of Natural Resources and Mines, Manufacturing and Regional and Rural Development) |
| Roads, building outlines, address points, signals, signs, bus stops, benches | ODbL 1.0 | © OpenStreetMap contributors |
| Gap-fill footprints: Microsoft Global ML Building Footprints (2026-02 release) | ODbL 1.0 | © Microsoft |

If you publish images or derived data, keep these credits. The viewer footer and the *About the data* dialog show them. The two image services are **ShareAlike**: if you distribute the model or textures, you must share those derived works under the same terms. The OSM and Microsoft parts are ODbL, which has the equivalent share-alike requirement for derived databases. Using the model in your own lab doesn't trigger either. (The imagery service only uses Planet satellite imagery, which is all rights reserved, where no aerial photography exists; that doesn't apply here.)

## Accuracy: what's surveyed, inferred and assumed

**Surveyed / measured**
- **Terrain:** LiDAR bare earth. It agrees with the raw 2019 ground points to 0.12 m on average.
- **Building heights:** measured for 99% of buildings from LiDAR roof returns (a typical roof is 97% covered). The 11 without returns use defaults. The data is from 2019, so anything built or demolished since then will differ.
- **Roofs:**
  - A clean fitted form is used where one explains the returns to within 12 cm RMS (365 buildings).
  - Otherwise the roof is the measured surface itself (1,199 buildings): roof cells are grouped into planes and fitted, then meshed to 6 cm and clipped to the outline. Chimneys, skylights and solar panels under about 3 m² are folded into the surrounding plane.
- **Trees:** treetops and crown sizes come from the LiDAR canopy height model. Crowns are drawn as generic shapes, and species aren't identified.
- **Poles and wires:** pole positions and heights are detected where the LiDAR's unclassified returns (power lines and pole tops) cluster. Wires are drawn only between poles the LiDAR shows are connected, so some real spans are missing. A few detections may be other tall structures.

**Mapped (positions from public databases)**
- **Building outlines:** OSM where mapped, otherwise Microsoft ML, plus outlines traced from LiDAR roof points (blockier; may include a few false positives such as large shade structures).
- **Lots:** DCDB boundaries. Their stated positional accuracy here is mostly ±1.5 m (shown in each lot's info).
- **Signals, signs, bus stops, benches, driveways:** OSM, so only what has been mapped (for example 60 driveways, while most houses have one).
- **Alignment:** OSM here was traced from GDA94-era imagery and sat about 1.5 m south-west of everything else. The pipeline shifts it by the GDA94→GDA2020 difference (0.60 m E, 1.39 m N; see `config.json`), which made its outlines match the LiDAR roofs. The LiDAR (supplied in GDA94) is transformed to GDA2020, the frame used by the DEM, imagery and cadastre; the cadastre needed no shift.

**Inferred or assumed (plausible, not observed)**
- **Fences:** a 1.8 m timber paling fence along every boundary shared by two lots (or a lot and a reserve), cut where a building straddles the line. Street frontages are left open. Real fence types, heights and gaps will differ.
- **Kerbs:** a 15 cm concrete kerb along every carriageway edge, from the OSM road outlines, with gaps at OSM driveways.
- **Street lights:** positions are not published openly here. A light is placed on street-side poles roughly 30 m apart.
- **Street-name signs:** placed at each corner where two named OSM streets meet, in a generic style.
- **Facades:** windows are drawn procedurally per storey on homes and civic buildings (not their real positions), with a front door on the wall facing the nearest street. Wall colours are neutral, not observed.
- **Road widths:** typical Brisbane values unless OSM tags `width` or `lanes`.

## Rebuild or change the area

```bash
python3 -m venv ~/.venvs/the-gap-vr          # keep the venv out of Dropbox
~/.venvs/the-gap-vr/bin/pip install -r requirements.txt
PY=~/.venvs/the-gap-vr/bin/python
$PY pipeline/fetch_osm.py            # OpenStreetMap via Overpass (falls back across mirrors)
$PY pipeline/fetch_msbuildings.py    # Microsoft footprints (70 MB tile, cached in $TMPDIR)
$PY pipeline/fetch_rasters.py        # LiDAR DEM (0.5 m) + 8192 px aerial mosaic
$PY pipeline/fetch_cadastre.py       # QLD DCDB parcels for the square
$PY pipeline/prepare_lidar.py ~/Downloads/DATA_19775.zip   # or the LAZ cache folder; see below
$PY pipeline/build_scene.py          # writes web/data/ and export/ in about a minute
```

**LiDAR point clouds** come from the free [ELVIS portal](https://elevation.fsdf.org.au/): Order Data → Manual, enter the area's bounding box, tick the `Brisbane_2019_Prj` point-cloud tiles, and ELVIS emails a download link (job 19775 was this area). `prepare_lidar.py` takes the zip or the folder of LAZ tiles. It keeps LAZ copies in `~/Library/Caches/the-gap-vr/lidar/` (1.2 GB; delete them whenever you like) and writes compact grids and the power-line points to `data/derived/` (20 MB). Rebuilds only need `data/derived/`. Without it the build falls back to estimated heights, with no trees and no poles.

The area is set in `config.json` by `anchor` (an MGA56 point, the model centre) and `half_size_m`. Change those and re-run everything to model somewhere else. Re-check `osm_offset_m` for a new area.

**Layout:**
- `pipeline/`: the Python scripts.
- `data/raw/`: the downloads (about 37 MB).
- `data/derived/`: the LiDAR products.
- `web/`: the static viewer (`index.html`, `js/`, `css/`, plus the generated `data/`, about 55 MB).
- `export/`: the files for other tools.

## Next steps
1. A Unity/Unreal import template, and compressed assets (Draco/KTX2) for standalone Quest performance.
2. Night mode with the street lights switched on.
3. Driveways traced from the aerial photo where OSM has none, and real fence lines from the LiDAR.
