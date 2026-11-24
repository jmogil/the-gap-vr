# The Gap 3D: Yuruga Street + 500 m

An interactive 3D model of Yuruga Street, The Gap (Brisbane, QLD) and everything within 500 m of it, built entirely from open data. You can orbit it like a map, walk it at street level, or stand in it in VR (WebXR, e.g. Meta Quest 3).

Phase 1 covers **terrain, roads and buildings**:

| Layer | What's in it | Source |
|---|---|---|
| Terrain | 1.2 km × 1.2 km LiDAR bare-earth model (1 m grid, adaptive mesh within 8 cm), draped with 2022 aerial imagery | QLD LiDAR DEM (Brisbane 2019) + Brisbane 2022 10 cm orthophoto |
| Roads & paths | 301 OSM ways buffered to carriageway width and draped exactly on the terrain; 13 bridges built as decks; cul-de-sac bulbs | OpenStreetMap |
| Buildings | 1,084 buildings with walls and modelled hipped roofs, roof colours sampled from the aerial photo, addresses where known | OSM outlines (582) + Microsoft ML footprints for unmapped houses |

## View it

```bash
./serve.command          # or: cd web && python3 -m http.server 8777
```

Then open <http://localhost:8777>. It has to be served over HTTP; opening `index.html` directly won't work. Three.js is loaded from a CDN, so you need to be online.

**Controls**
- **Orbit:** drag to rotate, right-drag to pan, scroll to zoom. Click a building or road for its details. Double-click the ground to walk there.
- **Walk:** W A S D or arrow keys to move, Shift to run, click to capture the mouse, Esc to release. F toggles fly mode (E/Q up and down). Keys 1 and 2 switch between orbit and walk.
- **VR:** left stick walks, right stick snap-turns 30°, trigger aims and releasing teleports. You stay on the terrain and can't walk through buildings.
- The panel has layer toggles and a sun-position slider (real solar position for The Gap, AEST), plus **Colour buildings by height source** to show which heights are estimates.
- The HUD shows your position in MGA zone 56, latitude/longitude and ground elevation (AHD).

### On a Meta Quest 3
WebXR needs a secure origin, and `localhost` counts. Either:
1. Connect the headset by USB with developer mode on, run `adb reverse tcp:8777 tcp:8777`, then open `http://localhost:8777` in the Quest Browser and press **VR**; or
2. Copy the `web/` folder to any HTTPS static host (GitHub Pages, Netlify, university web space).

*The VR mode has been written but not yet tested on a headset; only the desktop modes have been checked.*

### In Blender / Unity / Unreal
`export/the_gap_yuruga_st.glb` is the same scene with the aerial texture embedded. It is Y-up, in metres, with the origin at the midpoint of Yuruga Street (MGA56 E 494611, N 6964541). y is height above 52.86 m AHD and north is −z. `export/buildings.geojson` holds the building footprints and attributes for QGIS.

## Data sources and licences

| Data | Licence | Credit |
|---|---|---|
| LiDAR DEM: Brisbane 2019 project, via the QLD `Elevation/QldDem` image service | CC BY-SA (stated in the service's licence info) | © State of Queensland (Department of Natural Resources and Mines, Manufacturing and Regional and Rural Development) |
| Aerial imagery: `Brisbane_LGA_2022_10cm_SISP`, via `Basemaps/LatestStateProgram_AllUsers` | CC BY-SA (stated in the service's licence info) | © State of Queensland |
| Roads, building outlines, address points | ODbL 1.0 | © OpenStreetMap contributors |
| Gap-fill footprints + height estimates: Microsoft Global ML Building Footprints (2026-02 release) | ODbL 1.0 | © Microsoft |

If you publish images or derived data, keep these credits. The viewer footer and the *About the data* dialog show them. Both Queensland licences are **ShareAlike**: if you distribute the model or textures, you must share those derived works under the same terms. The OSM and Microsoft parts are ODbL, which has the equivalent share-alike requirement for derived databases. Using the model in your own lab doesn't trigger either. (The imagery service only uses Planet satellite imagery, which is all rights reserved, where no aerial photography exists; that doesn't apply here.)

## Accuracy: what's measured and what's modelled

- **Terrain:** surveyed LiDAR (bare earth, roughly ±0.1–0.15 m vertical), lightly smoothed.
- **Building outlines:** OSM where mapped (hand-traced, usually the roof outline). Otherwise ML-traced from imagery. Houses hidden under tree canopy can be missing.
- **Building heights are estimates.** 952 come from Microsoft's ML model (they cluster around 5 m, so highset and two-storey houses are under-estimated), 16 from OSM `building:levels`, and 116 are defaults. The *Colour buildings by height source* toggle shows which is which.
- **Roofs** are generated hipped roofs (exact straight-skeleton geometry) at 22.5° pitch, not measured shapes. Colours come from the 2022 photo.
- **Road widths** are typical Brisbane values unless OSM tags `width` or `lanes`.
- **Not yet modelled:** trees and vegetation, fences, kerbs, driveways, street furniture, facades.

## Rebuild or change the area

```bash
python3 -m venv ~/.venvs/the-gap-vr          # keep the venv out of Dropbox
~/.venvs/the-gap-vr/bin/pip install -r requirements.txt
PY=~/.venvs/the-gap-vr/bin/python
$PY pipeline/fetch_osm.py            # OpenStreetMap via Overpass (falls back across mirrors)
$PY pipeline/fetch_msbuildings.py    # Microsoft footprints (70 MB tile, cached in $TMPDIR)
$PY pipeline/fetch_rasters.py        # LiDAR DEM (0.5 m) + 8192 px aerial mosaic
$PY pipeline/build_scene.py          # writes web/data/ and export/ in about 10 s
```

The area is set in `config.json` by `anchor` (an MGA56 point) and `half_size_m`. Change those and re-run everything to model somewhere else.

**Layout:** `pipeline/` holds the Python scripts. `data/raw/` holds the downloads (about 36 MB). `web/` is the static viewer: `index.html`, `js/`, `css/`, plus the generated `data/` (about 43 MB). `export/` holds the files for other tools.

## Next steps
1. **LiDAR point clouds** (Brisbane 2019, free through GA's ELVIS portal) would give measured heights for every building, roof shapes, and 3D trees from the vegetation classes. This is the biggest realism gain, because The Gap is heavily treed.
2. Property boundaries from the QLD cadastre (DCDB), for fences and lots.
3. Kerbs and driveways, street lights and signs from OSM, and simple facade textures.
4. A Unity/Unreal import template, and compressed assets (Draco/KTX2) for standalone Quest performance.
