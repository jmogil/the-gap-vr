# The Gap 3D: Yuruga Street + 500 m

An interactive 3D model of Yuruga Street, The Gap (Brisbane, QLD) and everything within 500 m of it, built entirely from open data. You can orbit it like a map, walk it at street level, or stand in it in VR (WebXR, e.g. Meta Quest 3).

| Layer | What's in it | Source |
|---|---|---|
| Terrain | 1.2 km × 1.2 km LiDAR bare-earth model (1 m grid, adaptive mesh within 8 cm), draped with 2022 aerial imagery | QLD LiDAR DEM (Brisbane 2019) + Brisbane 2022 10 cm orthophoto |
| Roads & paths | 300 OSM ways buffered to carriageway width and draped exactly on the terrain; 13 bridges built as decks; cul-de-sac bulbs | OpenStreetMap |
| Buildings | 1,575 buildings. Heights and **roof shapes come from the 2019 LiDAR** for 1,564 of them: 365 simple roofs are fitted as clean flat, hip, gable or skillion forms, and 1,199 complex roofs use the measured roof surface (planes, ridges, steps, verandahs). Roof colours come from the aerial photo, and addresses are shown where known. | Outlines: OSM (534), Microsoft ML (550), traced from LiDAR (491, mostly sheds, garages and houses under canopy) |
| Trees | 12,599 trees with height and crown width from the LiDAR canopy, crown colour from the aerial photo | Brisbane 2019 LiDAR point cloud (202 M points) |

## View it

```bash
./serve.command          # or: cd web && python3 -m http.server 8777
```

Then open <http://localhost:8777>. It has to be served over HTTP; opening `index.html` directly won't work. Three.js is loaded from a CDN, so you need to be online.

**Controls**
- **Orbit:** drag to rotate, right-drag to pan, scroll to zoom. Click a building, road or tree for its details. Double-click the ground to walk there.
- **Walk:** W A S D or arrow keys to move, Shift to run, click to capture the mouse, Esc to release. F toggles fly mode (E/Q up and down). Keys 1 and 2 switch between orbit and walk.
- **VR:** left stick walks, right stick snap-turns 30°, trigger aims and releasing teleports. You stay on the terrain and can't walk through buildings.
- The panel has layer toggles (including trees) and a sun-position slider (real solar position for The Gap, AEST), plus **Colour buildings by height source** to show which heights are measured.
- The HUD shows your position in MGA zone 56, latitude/longitude and ground elevation (AHD).

### On a Meta Quest 3
WebXR needs a secure origin, and `localhost` counts. Either:
1. Connect the headset by USB with developer mode on, run `adb reverse tcp:8777 tcp:8777`, then open `http://localhost:8777` in the Quest Browser and press **VR**; or
2. Copy the `web/` folder to any HTTPS static host (GitHub Pages, Netlify, university web space).

*The VR mode has been written but not yet tested on a headset; only the desktop modes have been checked. With trees the scene is about 1.5 M triangles. If the Quest stutters, turn off shadows or trees first.*

### In Blender / Unity / Unreal
`export/the_gap_yuruga_st.glb` is the terrain, roads and buildings with the aerial texture embedded. It is Y-up, in metres, with the origin at the midpoint of Yuruga Street (MGA56 E 494611, N 6964541). y is height above 52.86 m AHD and north is −z. `export/trees.csv` lists every tree (position, ground height, height, crown radius, lat/lon) for placing your own tree models. `export/buildings.geojson` holds the building footprints and attributes (including roof form and pitch) for QGIS. Measured roofs are faceted, so use flat shading for the roof mesh (Blender: *Shade Flat*; Unity: import with a smoothing angle of 0).

## Data sources and licences

| Data | Licence | Credit |
|---|---|---|
| LiDAR point cloud: Queensland LiDAR Data, Brisbane 2019 Project (classified, AHD), via ELVIS | CC BY 4.0 | © State of Queensland (Department of Resources); distributed by Geoscience Australia |
| LiDAR DEM: Brisbane 2019 project, via the QLD `Elevation/QldDem` image service | CC BY-SA (stated in the service's licence info) | © State of Queensland (Department of Natural Resources and Mines, Manufacturing and Regional and Rural Development) |
| Aerial imagery: `Brisbane_LGA_2022_10cm_SISP`, via `Basemaps/LatestStateProgram_AllUsers` | CC BY-SA (stated in the service's licence info) | © State of Queensland |
| Roads, building outlines, address points | ODbL 1.0 | © OpenStreetMap contributors |
| Gap-fill footprints: Microsoft Global ML Building Footprints (2026-02 release) | ODbL 1.0 | © Microsoft |

If you publish images or derived data, keep these credits. The viewer footer and the *About the data* dialog show them. The two image services are **ShareAlike**: if you distribute the model or textures, you must share those derived works under the same terms. The OSM and Microsoft parts are ODbL, which has the equivalent share-alike requirement for derived databases. Using the model in your own lab doesn't trigger either. (The imagery service only uses Planet satellite imagery, which is all rights reserved, where no aerial photography exists; that doesn't apply here.)

## Accuracy: what's measured and what's modelled

- **Terrain:** surveyed LiDAR (bare earth). It agrees with the raw 2019 ground points to 0.12 m on average.
- **Building heights:** measured for 99% of buildings. The ridge is the 97th percentile of the LiDAR roof returns and the eave is taken near the outline; a typical roof is 97% covered by returns. The 11 without returns use defaults. The data is from 2019, so anything built or demolished since then will differ.
- **Roofs** come from the LiDAR roof returns in one of two ways:
  - If a single flat, hip, gable or skillion form explains the returns to within 12 cm RMS, that clean fitted form is used (365 buildings). The fitter tests hips, gables at different edges and skillions, and picks the best fit with a small penalty for extra complexity.
  - Otherwise the roof is the measured surface itself (1,199 buildings). Roof cells are grouped into planes, each plane is fitted, and chimneys and vents are folded into the surrounding plane. The result is meshed to within 6 cm and clipped to the outline, so ridges, hips, valleys, steps between levels and verandah roofs are where the LiDAR puts them.
  - Walls rise to meet the roof everywhere, including gable ends.
  - Fine details under about 3 m² (chimneys, skylights, solar panels) are not modelled. Roof colours come from the 2022 photo.
- **Building outlines:** OSM where mapped, otherwise Microsoft ML, plus outlines traced from LiDAR roof points for buildings neither source had. LiDAR-traced outlines are blockier and may include a few false positives, such as large shade structures.
- **Alignment:** OSM in this area was traced from GDA94-era imagery and sat about 1.5 m south-west of everything else. The pipeline shifts OSM by the GDA94→GDA2020 difference (0.60 m E, 1.39 m N; see `config.json`), which made its outlines match the LiDAR roofs. The LiDAR itself (supplied in GDA94) is transformed to GDA2020, the frame used by the DEM and imagery.
- **Trees:** treetops are local maxima of the LiDAR canopy height model, and crowns are the canopy cells closest to each top. Positions and heights are good. Crowns are drawn as generic rounded shapes, and species aren't identified.
- **Road widths** are typical Brisbane values unless OSM tags `width` or `lanes`.
- **Not yet modelled:** fences, kerbs, driveways, street furniture, facades.

## Rebuild or change the area

```bash
python3 -m venv ~/.venvs/the-gap-vr          # keep the venv out of Dropbox
~/.venvs/the-gap-vr/bin/pip install -r requirements.txt
PY=~/.venvs/the-gap-vr/bin/python
$PY pipeline/fetch_osm.py            # OpenStreetMap via Overpass (falls back across mirrors)
$PY pipeline/fetch_msbuildings.py    # Microsoft footprints (70 MB tile, cached in $TMPDIR)
$PY pipeline/fetch_rasters.py        # LiDAR DEM (0.5 m) + 8192 px aerial mosaic
$PY pipeline/prepare_lidar.py ~/Downloads/DATA_19775.zip   # optional, see below
$PY pipeline/build_scene.py          # writes web/data/ and export/ in about 50 s
```

**LiDAR point clouds** come from the free [ELVIS portal](https://elevation.fsdf.org.au/): Order Data → Manual, enter the area's bounding box, tick the `Brisbane_2019_Prj` point-cloud tiles, and ELVIS emails a download link (job 19775 was this area). `prepare_lidar.py` reads the zip in about 15 s, keeps LAZ copies in `~/Library/Caches/the-gap-vr/lidar/` (1.2 GB; delete them whenever you like), and writes compact 0.5 m grids to `data/derived/` (19 MB). Rebuilds only need those grids, so you never have to download the point cloud again unless the area changes. Without the grids the build falls back to estimated heights and no trees.

The area is set in `config.json` by `anchor` (an MGA56 point) and `half_size_m`. Change those and re-run everything to model somewhere else. Re-check `osm_offset_m` for a new area.

**Layout:** `pipeline/` holds the Python scripts. `data/raw/` holds the downloads (about 36 MB), and `data/derived/` holds the LiDAR grids. `web/` is the static viewer: `index.html`, `js/`, `css/`, plus the generated `data/` (about 47 MB). `export/` holds the files for other tools.

## Next steps
1. Property boundaries from the QLD cadastre (DCDB), for fences and lots.
2. Kerbs and driveways, street lights and signs from OSM, and simple facade textures (windows, doors).
3. A Unity/Unreal import template, and compressed assets (Draco/KTX2) for standalone Quest performance.
