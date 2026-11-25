// The Gap 3D — open-data model of a 1.2 km × 1.2 km area of The Gap, Brisbane.
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { Sky } from 'three/addons/objects/Sky.js';
import { computeBoundsTree, disposeBoundsTree, acceleratedRaycast } from 'three-mesh-bvh';
import { TerrainHeights, BuildingIndex, toLonLat, toMGA } from './world.js';
import { WalkControls } from './walk.js';
import { XRLocomotion } from './xr.js';
import { sunPosition, brisbaneDate, brisbaneToday } from './sun.js';
import { Minimap } from './minimap.js';
import { makeLabel } from './labels.js';
import { buildStreetscape, buildLotLines, LotIndex, palingTexture, applyFacade } from './streetscape.js';
import { makeTrees, makeWaterMaterial, skyEnvironment } from './nature.js';

THREE.BufferGeometry.prototype.computeBoundsTree = computeBoundsTree;
THREE.BufferGeometry.prototype.disposeBoundsTree = disposeBoundsTree;
THREE.Mesh.prototype.raycast = acceleratedRaycast;

const $ = (id) => document.getElementById(id);
const isMobile = /Android|iPhone|iPad|Quest|OculusBrowser|Pico/i.test(navigator.userAgent);
const SRC_COLOURS = { 'LiDAR 2019 (measured)': '#4caf7d', 'OSM height tag': '#3fa7d6', 'OSM building:levels': '#3fa7d6',
  'Microsoft ML estimate': '#f2a541', 'default (no data)': '#d64f6a' };

function setProgress(f, text) {
  $('loading-bar').style.width = `${Math.round(f * 100)}%`;
  if (text) $('loading-text').textContent = text;
}
async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}
async function fetchBuffer(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.arrayBuffer();
}
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1).replace(/_/g, ' ') : '');

main().catch((err) => {
  console.error(err);
  $('loading').classList.add('error');
  $('loading-text').textContent = `Could not load the model: ${err?.message || err?.target?.src || err}` +
    (location.protocol === 'file:' ? ' — open it through a local web server (see README).' : '');
});

async function main() {
  setProgress(0.03, 'Loading terrain and feature data…');
  const [meta, features, hmBuf] = await Promise.all([
    fetchJSON('data/scene.json'), fetchJSON('data/features.json'), fetchBuffer('data/terrain_1m.u16')]);
  const terrain = new TerrainHeights(meta, hmBuf);
  const bindex = new BuildingIndex(features.buildings);
  const H = meta.half_size_m;
  const lat0 = meta.anchor.lat, lon0 = meta.anchor.lon;

  // ---------------------------------------------------------------- renderer, scene, camera
  const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.toneMapping = THREE.NeutralToneMapping;
  renderer.toneMappingExposure = 1.0;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.xr.enabled = true;
  renderer.domElement.classList.add('webgl');
  renderer.domElement.tabIndex = 0;
  document.body.prepend(renderer.domElement);
  const maxAniso = renderer.capabilities.getMaxAnisotropy();

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(50, window.innerWidth / window.innerHeight, 0.5, 6000);
  const rig = new THREE.Group();            // the user's "body" in VR; identity otherwise
  rig.add(camera);
  scene.add(rig);

  // ---------------------------------------------------------------- sky, sun, fog
  const sky = new Sky();
  sky.scale.setScalar(4500);
  Object.assign(sky.material.uniforms.turbidity, { value: 4.5 });
  Object.assign(sky.material.uniforms.rayleigh, { value: 1.1 });
  Object.assign(sky.material.uniforms.mieCoefficient, { value: 0.004 });
  Object.assign(sky.material.uniforms.mieDirectionalG, { value: 0.8 });
  scene.add(sky);
  const hemi = new THREE.HemisphereLight(0xdde9f6, 0x66704f, 1.15);
  scene.add(hemi);
  const sun = new THREE.DirectionalLight(0xfff3df, 3.0);
  sun.castShadow = true;
  sun.shadow.mapSize.set(isMobile ? 2048 : 4096, isMobile ? 2048 : 4096);
  sun.shadow.bias = -0.0003;
  sun.shadow.normalBias = 0.05;
  sun.shadow.camera.near = 10;
  sun.shadow.camera.far = 2600;
  scene.add(sun, sun.target);
  const sunDir = new THREE.Vector3(0, 1, 0);
  const fogColour = new THREE.Color(0xc9d6e2);
  scene.fog = new THREE.Fog(fogColour, 1800, 5200);

  // ---------------------------------------------------------------- load model + imagery
  setProgress(0.1, 'Loading 3D model (terrain, roads, buildings)…');
  const gltf = await new Promise((resolve, reject) => new GLTFLoader().load('data/scene.glb', resolve,
    (e) => { if (e.total) setProgress(0.1 + 0.6 * (e.loaded / e.total)); }, reject));
  setProgress(0.72, 'Loading 2022 aerial imagery…');
  const loadOrtho = async (url) => {
    const t = await new THREE.TextureLoader().loadAsync(url);
    t.flipY = false;                               // glTF-style UVs (v down)
    t.colorSpace = THREE.SRGBColorSpace;
    t.anisotropy = maxAniso;
    t.wrapS = t.wrapT = THREE.ClampToEdgeWrapping;
    return t;
  };
  let ortho = await loadOrtho('data/' + meta.imagery.files['4096']);

  const meshes = {};
  gltf.scene.traverse((o) => { if (o.isMesh) meshes[o.name] = o; });
  const terrainMesh = meshes.terrain, roads = meshes.roads, walls = meshes.building_walls, roofs = meshes.building_roofs;
  scene.add(gltf.scene);

  const mats = {
    terrainPhoto: new THREE.MeshStandardMaterial({ map: ortho, roughness: 1, metalness: 0 }),
    terrainPlain: new THREE.MeshStandardMaterial({ color: 0xa9b894, roughness: 1, metalness: 0 }),
    roads: new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.95, metalness: 0,
      polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -4 }),
    walls: new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.85, metalness: 0 }),
    roofs: new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.72, metalness: 0, side: THREE.DoubleSide, flatShading: true }),
    roofsPhoto: new THREE.MeshStandardMaterial({ map: ortho, roughness: 0.8, metalness: 0, side: THREE.DoubleSide, flatShading: true }),
    highlight: new THREE.MeshBasicMaterial({ color: 0x3fa7d6, transparent: true, opacity: 0.5, side: THREE.DoubleSide,
      polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -8, depthWrite: false }),
  };
  terrainMesh.material = mats.terrainPhoto;
  roads.material = mats.roads;
  walls.material = mats.walls;
  applyFacade(mats.walls);                          // windows per storey + weatherboards
  const kerbs = meshes.kerbs, fences = meshes.fences, water = meshes.water;
  if (water) {
    water.material = makeWaterMaterial();
    water.receiveShadow = true;
    water.renderOrder = 2;
  }
  const skyEnv = skyEnvironment(renderer, sky.material);   // creek reflections follow the sky
  let envTimer = 0;
  const refreshEnv = () => {
    clearTimeout(envTimer);
    envTimer = setTimeout(() => { if (water) { water.material.envMap = skyEnv(); water.material.needsUpdate = true; } }, 150);
  };
  if (kerbs) {
    kerbs.material = new THREE.MeshStandardMaterial({ color: 0xbdb9b1, roughness: 0.9 });
    kerbs.receiveShadow = kerbs.castShadow = true;
  }
  if (fences) {
    fences.material = new THREE.MeshStandardMaterial({ map: palingTexture(), alphaTest: 0.5, side: THREE.DoubleSide, roughness: 1 });
    fences.receiveShadow = fences.castShadow = true;
  }
  roofs.material = mats.roofs;
  terrainMesh.receiveShadow = true;
  roads.receiveShadow = true;
  walls.castShadow = walls.receiveShadow = true;
  roofs.castShadow = roofs.receiveShadow = true;
  roads.renderOrder = 1;

  // Feature index ranges (contiguous per feature) — taken before the BVH reorders indices.
  const ranges = new Map();
  for (const m of [walls, roofs, roads]) {
    const idx = m.geometry.index.array.slice();
    const fid = m.geometry.attributes._feature_id.array;
    const r = new Map();
    for (let t = 0; t < idx.length; t += 3) {
      const f = fid[idx[t]];
      const e = r.get(f);
      if (e) e[1] = t + 3; else r.set(f, [t, t + 3]);
    }
    ranges.set(m, { idx, r });
  }
  setProgress(0.82, 'Indexing geometry for picking…');
  await new Promise((r) => setTimeout(r, 0));
  for (const m of [terrainMesh, roads, walls, roofs]) m.geometry.computeBoundsTree();

  // Diorama skirt around the edge of the terrain.
  scene.add(makeSkirt(terrain, H));

  // ---------------------------------------------------------------- street labels
  const labels = new THREE.Group();
  for (const l of features.labels) {
    const s = makeLabel(l.text);
    s.position.set(l.x, l.y + 6, l.z);
    s.userData.priority = 1;
    labels.add(s);
  }
  scene.add(labels);

  // Screen-space decluttering: keep the closest labels, hide ones that would overlap.
  const lv = new THREE.Vector3();
  function declutterLabels() {
    if (!labels.visible) return;
    const w = window.innerWidth, h = window.innerHeight, k = camera.projectionMatrix.elements[5] / 2 * h;
    const items = labels.children.map((s) => ({ s, d: s.position.distanceToSquared(camera.position) }));
    items.sort((a, b) => a.s.userData.priority - b.s.userData.priority || a.d - b.d);
    const boxes = [];
    for (const { s, d } of items) {
      lv.copy(s.position).project(camera);
      const maxD = app.mode === 'orbit' ? 900 : 260;          // street level: nearby streets only
      if (lv.z > 1 || Math.abs(lv.x) > 1.05 || Math.abs(lv.y) > 1.05 || d > maxD * maxD) { s.visible = false; continue; }
      const sx = ((lv.x + 1) / 2) * w, sy = ((1 - lv.y) / 2) * h;
      const bw = s.scale.x * k, bh = s.scale.y * k, pad = 6;
      const box = [sx - bw / 2 - pad, sy - bh - pad, sx + bw / 2 + pad, sy + pad];
      const clash = boxes.some((b) => box[0] < b[2] && box[2] > b[0] && box[1] < b[3] && box[3] > b[1]);
      s.visible = !clash;
      if (!clash) boxes.push(box);
    }
  }

  // ---------------------------------------------------------------- trees (2019 LiDAR canopy)
  const treeData = await fetchJSON('data/trees.json').catch(() => null);
  const [lotData, streetData] = await Promise.all([
    fetchJSON('data/lots.json').catch(() => null), fetchJSON('data/streetscape.json').catch(() => null)]);
  const street = streetData ? buildStreetscape(streetData) : null;
  if (street) scene.add(street.group);
  const lotLines = lotData ? buildLotLines(lotData, terrain) : null;
  if (lotLines) scene.add(lotLines);
  const lotIndex = lotData ? new LotIndex(lotData.lots) : null;
  const trees = treeData && treeData.trees.length ? makeTrees(treeData.trees, isMobile) : null;
  if (trees) scene.add(trees.group);

  // ---------------------------------------------------------------- controls
  const orbit = new OrbitControls(camera, renderer.domElement);
  orbit.enableDamping = true;
  orbit.dampingFactor = 0.08;
  orbit.screenSpacePanning = false;
  orbit.maxPolarAngle = THREE.MathUtils.degToRad(84);
  orbit.minDistance = 15;
  orbit.maxDistance = 2600;
  const h0 = terrain.heightAt(0, 0);
  orbit.target.set(0, h0, 0);
  camera.position.set(330, h0 + 560, -620);        // from the north-east, sun behind the viewer
  orbit.update();

  const walk = new WalkControls(camera, renderer.domElement, terrain, bindex);
  const xr = new XRLocomotion({ renderer, scene, rig, camera, terrain, buildings: bindex });

  const start = meta.start || { x: 0, z: 0, yaw: 0 };   // a street near the model centre

  const app = { mode: 'orbit', selected: null, highlight: null, shadowHalf: 0 };
  const minimap = new Minimap($('minimap'), meta, 'data/' + meta.imagery.files['1024'], (x, z) => jumpTo(x, z));

  // ---------------------------------------------------------------- sun
  const today = brisbaneToday();
  $('sun-date').value = today.iso;
  $('sun-time').value = 600; // 10:00
  function updateSun() {
    const minutes = Number($('sun-time').value);
    const date = brisbaneDate($('sun-date').value || today.iso, minutes);
    const { azimuth, elevation } = sunPosition(date, lat0, lon0);
    sunDir.set(Math.sin(azimuth) * Math.cos(elevation), Math.sin(elevation), -Math.cos(azimuth) * Math.cos(elevation));
    sky.material.uniforms.sunPosition.value.copy(sunDir);
    const day = THREE.MathUtils.smoothstep(elevation, -0.05, 0.25);
    sun.intensity = 4.2 * THREE.MathUtils.smoothstep(elevation, -0.01, 0.12);
    sun.color.setHSL(0.1, 0.6 * (1 - day) + 0.25, 0.92 - 0.1 * (1 - day));
    hemi.intensity = 0.2 + 0.6 * day;
    fogColour.setRGB(0.24 + 0.55 * day, 0.27 + 0.57 * day, 0.33 + 0.56 * day);
    scene.fog.color.copy(fogColour);
    renderer.toneMappingExposure = 0.8 + 0.25 * day;
    const hh = Math.floor(minutes / 60), mm = minutes % 60;
    $('sun-label').textContent = `${String(hh).padStart(2, '0')}:${String(mm).padStart(2, '0')} AEST · ` +
      `alt ${Math.round(THREE.MathUtils.radToDeg(elevation))}°`;
  }
  $('sun-time').addEventListener('input', updateSun);
  $('sun-time').addEventListener('input', refreshEnv);
  $('sun-date').addEventListener('change', refreshEnv);
  $('sun-date').addEventListener('change', updateSun);
  $('sun-now').addEventListener('click', () => {
    const t = brisbaneToday();
    $('sun-date').value = t.iso;
    $('sun-time').value = Math.min(1200, Math.max(300, t.minutes));
    updateSun();
    refreshEnv();
  });
  updateSun();
  refreshEnv();

  // ---------------------------------------------------------------- modes
  function freeSpot(x, z) {
    if (bindex.hit(x, z) < 0) return [x, z];
    for (let r = 1; r < 40; r += 1) for (let a = 0; a < 16; a++) {
      const px = x + r * Math.cos((a / 16) * Math.PI * 2), pz = z + r * Math.sin((a / 16) * Math.PI * 2);
      if (terrain.contains(px, pz, 2) && bindex.hit(px, pz) < 0) return [px, pz];
    }
    return [x, z];
  }

  function setMode(mode, opts = {}) {
    if (renderer.xr.isPresenting && mode !== 'vr') return;
    const prev = app.mode;
    if (prev === 'walk' && mode !== 'walk') walk.disable();
    app.mode = mode;
    if (mode === 'orbit') {
      orbit.enabled = true;
      camera.fov = 50;
      if (prev === 'walk') {
        const p = walk.pos;
        orbit.target.set(p.x, p.y, p.z);
        const back = new THREE.Vector3(Math.sin(walk.yaw), 0, Math.cos(walk.yaw)).multiplyScalar(140);
        camera.position.set(p.x + back.x, p.y + 110, p.z + back.z);
      }
      scene.fog.near = 1800; scene.fog.far = 5200;
    } else if (mode === 'walk') {
      orbit.enabled = false;
      camera.fov = 70;
      camera.near = 0.1;
      let x = opts.x ?? orbit.target.x, z = opts.z ?? orbit.target.z;
      let yaw = opts.yaw;
      if (opts.x === undefined && prev === 'orbit' && Math.hypot(orbit.target.x, orbit.target.z) < 1) {
        x = start.x; z = start.z; yaw = start.yaw;   // first walk: a street near the centre
      }
      if (yaw === undefined) {
        const d = new THREE.Vector3().subVectors(orbit.target, camera.position);
        yaw = Math.atan2(-d.x, -d.z);
      }
      [x, z] = freeSpot(x, z);
      walk.enable(x, z, yaw);
      scene.fog.near = 220; scene.fog.far = 1300;
    }
    camera.updateProjectionMatrix();
    syncModeUI();
  }

  function syncModeUI() {
    const m = app.mode;
    $('mode-orbit').classList.toggle('active', m === 'orbit');
    $('mode-walk').classList.toggle('active', m === 'walk');
    $('mode-vr').classList.toggle('active', m === 'vr');
    $('help-orbit').hidden = m !== 'orbit';
    $('help-walk').hidden = m !== 'walk';
    $('help-vr').hidden = m !== 'vr';
    $('crosshair').hidden = !(m === 'walk' && walk.locked);
    $('walk-overlay').hidden = !(m === 'walk' && !walk.locked);
    $('mode-hint').textContent = m === 'walk'
      ? (walk.fly ? 'Flying — E/Q to rise/sink, F to land.' : 'Walking at street level (eye height 1.65 m).')
      : m === 'vr' ? 'In VR — take off the headset to return.' : 'Overview of the area.';
    labels.visible = $('lyr-labels').checked && m !== 'vr';
  }
  walk.onChange = syncModeUI;

  function jumpTo(x, z) {
    if (app.mode === 'walk') {
      const [fx, fz] = freeSpot(x, z);
      walk.enable(fx, fz, walk.yaw);
    } else if (app.mode === 'orbit') {
      const off = new THREE.Vector3().subVectors(camera.position, orbit.target);
      orbit.target.set(x, terrain.heightAt(x, z), z);
      camera.position.copy(orbit.target).add(off);
    }
  }

  $('mode-orbit').addEventListener('click', () => setMode('orbit'));
  $('mode-walk').addEventListener('click', () => setMode('walk'));
  renderer.domElement.addEventListener('click', () => { if (app.mode === 'walk' && !walk.locked) walk.lock(); });
  renderer.domElement.addEventListener('contextmenu', (e) => e.preventDefault());
  renderer.domElement.addEventListener('dblclick', (e) => {
    if (app.mode !== 'orbit') return;
    const hit = pickAt(e.clientX, e.clientY, true);
    if (!hit) return;
    const d = new THREE.Vector3().subVectors(hit.point, camera.position);
    setMode('walk', { x: hit.point.x, z: hit.point.z, yaw: Math.atan2(-d.x, -d.z) });
  });
  window.addEventListener('keydown', (e) => {
    if (e.target.closest?.('input, textarea, select')) return;
    if (e.code === 'Digit1') setMode('orbit');
    if (e.code === 'Digit2') setMode('walk');
  });

  // ---------------------------------------------------------------- VR
  if (navigator.xr) {
    navigator.xr.isSessionSupported('immersive-vr').then((ok) => {
      if (!ok) return;
      $('mode-vr').disabled = false;
      $('mode-vr').title = 'Enter VR (e.g. Meta Quest)';
    }).catch(() => {});
  }
  let xrSession = null;
  $('mode-vr').addEventListener('click', async () => {
    if (xrSession) { xrSession.end(); return; }
    try {
      const session = await navigator.xr.requestSession('immersive-vr',
        { optionalFeatures: ['local-floor', 'bounded-floor', 'hand-tracking'] });
      renderer.xr.setReferenceSpaceType('local-floor');
      await renderer.xr.setSession(session);
      xrSession = session;
      session.addEventListener('end', () => { xrSession = null; });
    } catch (err) {
      console.error(err);
      $('mode-hint').textContent = `Could not start VR: ${err.message}`;
    }
  });
  renderer.xr.addEventListener('sessionstart', () => {
    let x, z, yaw;
    if (app.mode === 'walk') {
      x = walk.pos.x; z = walk.pos.z; yaw = walk.yaw;
    } else {
      [x, z] = freeSpot(start.x, start.z); yaw = start.yaw;
    }
    walk.disable();
    orbit.enabled = false;
    camera.position.set(0, 0, 0);
    camera.rotation.set(0, 0, 0);
    xr.start(x, z, yaw);
    app.mode = 'vr';
    scene.fog.near = 220; scene.fog.far = 1300;
    syncModeUI();
  });
  renderer.xr.addEventListener('sessionend', () => {
    const head = xr.getHead().clone();
    const dir = camera.getWorldDirection(new THREE.Vector3());
    rig.position.set(0, 0, 0);
    rig.rotation.set(0, 0, 0);
    app.mode = 'vr-exit';
    setMode('walk', { x: head.x, z: head.z, yaw: Math.atan2(-dir.x, -dir.z) });
  });

  // ---------------------------------------------------------------- layers
  const lyr = (id, fn) => { $(id).addEventListener('change', (e) => fn(e.target.checked)); fn($(id).checked); };
  lyr('lyr-buildings', (on) => { walls.visible = roofs.visible = on; clearSelection(); });
  lyr('lyr-roads', (on) => { roads.visible = on; if (kerbs) kerbs.visible = on; });
  lyr('lyr-fences', (on) => { if (fences) fences.visible = on; });
  lyr('lyr-water', (on) => { if (water) water.visible = on; });
  lyr('lyr-furniture', (on) => { if (street) street.group.visible = on; });
  lyr('lyr-lots', (on) => { if (lotLines) lotLines.visible = on; });
  lyr('lyr-labels', () => syncModeUI());
  lyr('lyr-aerial', (on) => { terrainMesh.material = on ? mats.terrainPhoto : mats.terrainPlain; });
  lyr('lyr-photoroofs', (on) => { roofs.material = on ? mats.roofsPhoto : mats.roofs; });
  lyr('lyr-shadows', (on) => { sun.castShadow = on; });
  lyr('lyr-trees', (on) => { if (trees) trees.group.visible = on; });
  if (!trees) $('lyr-trees').closest('label').hidden = true;
  const colourStore = new Map();
  lyr('lyr-heightsrc', (on) => {
    $('legend-src').hidden = !on;
    for (const m of [walls, roofs]) {
      const col = m.geometry.attributes.color;
      if (!colourStore.has(m)) colourStore.set(m, col.array.slice());
      if (!on) { col.array.set(colourStore.get(m)); col.needsUpdate = true; continue; }
      const fid = m.geometry.attributes._feature_id.array;
      const c = new THREE.Color();
      for (let i = 0; i < fid.length; i++) {
        const b = features.buildings[fid[i]];
        c.set(SRC_COLOURS[b.height_source] || '#888888');
        col.array[i * 3] = c.r; col.array[i * 3 + 1] = c.g; col.array[i * 3 + 2] = c.b;
      }
      col.needsUpdate = true;
    }
  });
  $('panel-toggle').addEventListener('click', () => {
    const p = $('panel');
    p.classList.toggle('collapsed');
    $('panel-toggle').textContent = p.classList.contains('collapsed') ? '+' : '–';
  });

  // ---------------------------------------------------------------- picking + info
  const raycaster = new THREE.Raycaster();
  raycaster.firstHitOnly = true;
  const ndc = new THREE.Vector2();
  function pickAt(cx, cy, groundOnly = false, withTrees = false) {
    if (cx === null) ndc.set(0, 0);
    else ndc.set((cx / window.innerWidth) * 2 - 1, -(cy / window.innerHeight) * 2 + 1);
    raycaster.setFromCamera(ndc, camera);
    const targets = groundOnly ? [terrainMesh, roads] : [roofs, walls, roads, terrainMesh];
    if (withTrees && trees && trees.group.visible) targets.push(trees.crowns);
    if (withTrees && street && street.group.visible) targets.push(...street.pick);
    if (withTrees && water && water.visible) targets.unshift(water);
    const hits = raycaster.intersectObjects(targets.filter((m) => m.visible), false);
    return hits[0] || null;
  }

  function clearSelection() {
    if (app.highlight) { scene.remove(app.highlight); app.highlight.traverse((o) => o.geometry?.dispose()); }
    app.highlight = null;
    app.selected = null;
    $('info').hidden = true;
  }

  function highlightFeature(meshList, fid) {
    const g = new THREE.Group();
    for (const m of meshList) {
      const rr = ranges.get(m);
      const span = rr.r.get(fid);
      if (!span) continue;
      const geo = new THREE.BufferGeometry();
      geo.setAttribute('position', m.geometry.attributes.position);
      geo.setIndex(new THREE.BufferAttribute(rr.idx.slice(span[0], span[1]), 1));
      const hm = new THREE.Mesh(geo, mats.highlight);
      hm.renderOrder = 5;
      g.add(hm);
    }
    scene.add(g);
    return g;
  }

  function select(hit) {
    clearSelection();
    if (!hit) return;
    if (hit.object === terrainMesh) {                  // bare ground: which lot is this?
      const lot = lotIndex?.at(hit.point.x, hit.point.z);
      if (lot) showLot(lot);
      return;
    }
    if (trees && hit.object === trees.crowns) { showTree(treeData.trees[trees.treeIndex[hit.instanceId]]); return; }
    if (water && hit.object === water) { showCreek(features.waterways[water.geometry.attributes._feature_id.getX(hit.face.a)]); return; }
    if (hit.object.userData?.kind) { showFurniture(hit.object.userData.kind, hit.object.userData.records[hit.instanceId]); return; }
    const fid = hit.object.geometry.attributes._feature_id.getX(hit.face.a);
    if (hit.object === roads) {
      app.highlight = highlightFeature([roads], fid);
      showRoad(features.roads[fid]);
    } else {
      app.highlight = highlightFeature([walls, roofs], fid);
      showBuilding(features.buildings[fid]);
    }
  }

  function osmLink(id) {
    if (!id) return '';
    const [t, n] = id.split('/');
    return `<a href="https://www.openstreetmap.org/${t}/${n}" target="_blank" rel="noopener">OSM ${t} ${n}</a>`;
  }

  function showBuilding(b) {
    const title = b.addresses?.[0] || b.name || cap(b.type === 'yes' ? 'building' : b.type);
    const extra = b.addresses?.length > 1 ? `<dt>Also</dt><dd>${b.addresses.slice(1).map(esc).join('<br>')}</dd>` : '';
    const measured = b.height_source.startsWith('LiDAR');
    const outline = b.osm_id ? osmLink(b.osm_id)
      : b.source.startsWith('LiDAR') ? 'Traced from 2019 LiDAR roof points' : 'Microsoft ML footprint (2026 release)';
    $('info-body').innerHTML = `
      <div class="kind">Building · ${esc(b.source)}</div>
      <h3>${esc(title)}</h3>
      <dl>
        ${extra}
        <dt>Type</dt><dd>${esc(cap(b.type))}${b.name && b.name !== title ? ` · ${esc(b.name)}` : ''}</dd>
        <dt>Height</dt><dd>${b.height_m.toFixed(1)} m to top · eave ${b.eave_m.toFixed(1)} m</dd>
        <dt>Height from</dt><dd><span class="swatch" style="background:${SRC_COLOURS[b.height_source]}"></span>${esc(b.height_source)}${measured && b.lidar_coverage ? ` · ${Math.round(b.lidar_coverage * 100)}% of roof seen` : ''}</dd>
        ${b.levels ? `<dt>Levels</dt><dd>${b.levels}</dd>` : ''}
        <dt>Roof</dt><dd>${esc(cap(b.roof))}${b.roof_pitch_deg ? `, ${b.roof_pitch_deg.toFixed(0)}° pitch` : ''}
          <br><span style="color:var(--text-dim)">${esc(roofFrom(b))}</span></dd>
        <dt>Roof colour</dt><dd>${b.roof_colour ? `<span class="swatch" style="background:${b.roof_colour}"></span>from 2022 aerial` : 'default'}</dd>
        <dt>Footprint</dt><dd>${b.footprint_m2.toFixed(0)} m²</dd>
        <dt>Ground</dt><dd>${b.ground_ahd.toFixed(1)} m AHD</dd>
        <dt>Outline</dt><dd>${outline}</dd>
      </dl>
      <p class="note">${measured
        ? (b.roof_from === 'LiDAR surface'
          ? 'Roof surface built directly from the 2019 LiDAR returns (simplified to about 15 cm).'
          : 'Roof form and pitch fitted to the 2019 LiDAR returns.')
        : 'Height is an estimate (no LiDAR roof returns here). Roof form is modelled.'}</p>`;
    $('info').hidden = false;
  }

  function roofFrom(b) {
    if (b.roof_from === 'LiDAR surface') return 'measured LiDAR surface';
    if (b.roof_from === 'LiDAR fit') return `fitted to LiDAR (±${(b.roof_fit_rms_m * 100).toFixed(0)} cm)`;
    return b.roof_from || 'modelled';
  }

  function showLot(l) {
    const title = l.type === 'Lot Type Parcel' ? `Lot ${l.lot} on ${l.plan}` : (l.name || l.type || 'Parcel');
    $('info-body').innerHTML = `
      <div class="kind">Land parcel · QLD cadastre</div>
      <h3>${esc(title)}</h3>
      <dl>
        <dt>Lot/plan</dt><dd>${esc(l.lotplan || '—')}</dd>
        <dt>Type</dt><dd>${esc(l.type || '—')}</dd>
        ${l.tenure ? `<dt>Tenure</dt><dd>${esc(l.tenure)}</dd>` : ''}
        <dt>Area</dt><dd>${Number(l.area_m2).toLocaleString()} m²</dd>
        ${l.name ? `<dt>Name</dt><dd>${esc(l.name)}</dd>` : ''}
        <dt>Accuracy</dt><dd>${esc((l.accuracy || '').replace(/^.*-\s*/, '±') || '—')}</dd>
      </dl>
      <p class="note">Boundaries from the Digital Cadastral Database (CC BY 4.0). Fences in the model are inferred from these boundaries, not surveyed.</p>`;
    $('info').hidden = false;
  }

  function showFurniture(kind, r) {
    const info = {
      pole: () => ['Power pole · LiDAR 2019', `Pole, about ${r[3].toFixed(1)} m tall`,
        `<dt>Street light</dt><dd>${r[4] ? 'assumed (not in open data)' : 'none modelled'}</dd>`,
        'Pole position and height detected from power-line returns in the 2019 LiDAR. Street-light positions are not published openly here; lights are placed on street-side poles about 30 m apart.'],
      signal: () => ['Traffic signals · OpenStreetMap', 'Traffic signal', `<dt>OSM node</dt><dd>${osmNode(r[4])}</dd>`,
        'Signal location from OpenStreetMap; poles are placed either side of the road.'],
      give_way: () => ['Sign · OpenStreetMap', 'Give way sign', `<dt>OSM node</dt><dd>${osmNode(r[5])}</dd>`, ''],
      stop: () => ['Sign · OpenStreetMap', 'Stop sign', `<dt>OSM node</dt><dd>${osmNode(r[5])}</dd>`, ''],
      bus_stop: () => ['Bus stop · OpenStreetMap', r[5] || 'Bus stop', `<dt>OSM node</dt><dd>${osmNode(r[4])}</dd>`, ''],
      bench: () => ['Bench · OpenStreetMap', 'Bench', `<dt>OSM node</dt><dd>${osmNode(r[4])}</dd>`, ''],
      name_sign: () => ['Street sign', r.blades.map((b) => b[0]).join(' / '), '',
        'Street-name sign generated at an intersection of named OpenStreetMap streets.'],
    }[kind]?.();
    if (!info) return;
    const [k, title, rows, note] = info;
    $('info-body').innerHTML = `<div class="kind">${esc(k)}</div><h3>${esc(title)}</h3>
      ${rows ? `<dl>${rows}</dl>` : ''}${note ? `<p class="note">${esc(note)}</p>` : ''}`;
    $('info').hidden = false;
  }

  function osmNode(id) {
    return `<a href="https://www.openstreetmap.org/node/${id}" target="_blank" rel="noopener">${id}</a>`;
  }

  function showCreek(w) {
    $('info-body').innerHTML = `
      <div class="kind">Creek · OpenStreetMap + LiDAR</div>
      <h3>${esc(w.name || (w.kind === 'drain' ? 'Unnamed drain' : 'Unnamed creek'))}</h3>
      <dl>
        <dt>Type</dt><dd>${esc(cap(w.kind))}${w.intermittent ? ', intermittent (often dry)' : ''}</dd>
        <dt>Water width</dt><dd>${w.width_m[0]}–${w.width_m[1]} m (modelled)</dd>
        <dt>Length</dt><dd>${w.length_m} m in model</dd>
        <dt>Source</dt><dd>${osmLink(w.osm_id)}</dd>
      </dl>
      <p class="note">Centreline from OpenStreetMap (also in Queensland's watercourse lines), snapped to the channel in the 2019 LiDAR. Width and water level are estimated from the terrain, not surveyed.</p>`;
    $('info').hidden = false;
  }

  function showTree(t) {
    const [x, y, z, h, r] = t;
    const [lon, lat] = toLonLat(meta, x, z);
    $('info-body').innerHTML = `
      <div class="kind">Tree · LiDAR 2019</div>
      <h3>Tree, ${h.toFixed(1)} m tall</h3>
      <dl>
        <dt>Height</dt><dd>${h.toFixed(1)} m above ground</dd>
        <dt>Crown</dt><dd>about ${(2 * r).toFixed(0)} m across</dd>
        <dt>Ground</dt><dd>${(y + meta.base_z_ahd).toFixed(1)} m AHD</dd>
        <dt>Location</dt><dd>${Math.abs(lat).toFixed(5)}°S ${lon.toFixed(5)}°E</dd>
      </dl>
      <p class="note">Treetop and crown size detected in the 2019 LiDAR canopy. Species and exact crown shape are not modelled; colour comes from the 2022 aerial photo.</p>`;
    $('info').hidden = false;
  }

  function showRoad(r) {
    $('info-body').innerHTML = `
      <div class="kind">${r.bridge ? 'Bridge' : r.group === 'path' ? 'Path' : 'Road'} · OpenStreetMap</div>
      <h3>${esc(r.name || cap(r.highway))}</h3>
      <dl>
        <dt>Class</dt><dd>${esc(cap(r.highway))}${r.ref ? ` (${esc(r.ref)})` : ''}</dd>
        <dt>Width</dt><dd>${r.width_m} m <span style="color:var(--text-dim)">(${esc(r.width_source)})</span></dd>
        ${r.surface ? `<dt>Surface</dt><dd>${esc(cap(r.surface))}</dd>` : ''}
        ${r.lanes ? `<dt>Lanes</dt><dd>${esc(r.lanes)}</dd>` : ''}
        ${r.maxspeed ? `<dt>Speed limit</dt><dd>${esc(r.maxspeed)} km/h</dd>` : ''}
        ${r.oneway ? `<dt>One-way</dt><dd>${esc(r.oneway)}</dd>` : ''}
        ${r.sidewalk ? `<dt>Footpath</dt><dd>${esc(r.sidewalk)}</dd>` : ''}
        <dt>Length</dt><dd>${r.length_m} m in model</dd>
        <dt>Source</dt><dd>${osmLink(r.osm_id)}</dd>
      </dl>`;
    $('info').hidden = false;
  }
  $('info-close').addEventListener('click', clearSelection);

  let down = null;
  renderer.domElement.addEventListener('pointerdown', (e) => { down = { x: e.clientX, y: e.clientY, t: performance.now() }; });
  renderer.domElement.addEventListener('pointerup', (e) => {
    if (!down || e.button !== 0) return;
    const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y);
    if (moved < 5 && performance.now() - down.t < 500) {
      if (app.mode === 'orbit') select(pickAt(e.clientX, e.clientY, false, true));
      else if (app.mode === 'walk' && walk.locked) select(pickAt(null, null, false, true));
    }
    down = null;
  });
  let hoverQueued = false;
  renderer.domElement.addEventListener('pointermove', (e) => {
    if (app.mode !== 'orbit' || hoverQueued || e.buttons) return;
    hoverQueued = true;
    requestAnimationFrame(() => {
      hoverQueued = false;
      const h = pickAt(e.clientX, e.clientY);
      renderer.domElement.style.cursor = h && h.object !== terrainMesh ? 'pointer' : 'default';
    });
  });

  // ---------------------------------------------------------------- about dialog
  $('about-body').innerHTML = `
    <p>${esc(meta.title)}: a ${2 * H / 1000} km × ${2 * H / 1000} km open-data model centred on
    ${Math.abs(meta.anchor.lat).toFixed(5)}°S ${meta.anchor.lon.toFixed(5)}°E (${esc(meta.crs_name)}).</p>
    <table><tr><th>Layer</th><th>Source</th><th>Licence</th></tr>
    ${meta.sources.map((s) => `<tr><td>${esc(s.layer)}</td><td>${esc(s.name)}<br><small>${esc(s.credit)}</small></td><td>${esc(s.licence)}</td></tr>`).join('')}
    </table>
    <p><b>${meta.counts.buildings}</b> buildings (${meta.counts.lidar_heights || 0} with LiDAR-measured heights,
    ${meta.counts.lidar_outlines || 0} outlined from LiDAR) · <b>${(meta.counts.trees || 0).toLocaleString()}</b> trees ·
    <b>${(meta.counts.lots || 0).toLocaleString()}</b> land parcels · <b>${meta.counts.poles || 0}</b> poles ·
    <b>${meta.counts.roads}</b> road/path segments · <b>${meta.counts.terrain_triangles.toLocaleString()}</b> terrain
    triangles. Built ${esc(meta.built)}.</p>
    <p>Accuracy notes: terrain, building heights and trees come from the 2019 LiDAR survey (about 34 points/m²).
    Building outlines are OSM where mapped, otherwise Microsoft ML or traced from LiDAR. Roofs come from the LiDAR:
    simple roofs as fitted flat/hip/gable/skillion forms, complex roofs as the measured, plane-fitted roof surface. OSM here is shifted ${esc(
      (meta.alignment?.osm_offset_m || [0, 0]).map((v) => v.toFixed(2)).join(' m E, '))} m N to match the LiDAR.
    Road widths are typical values unless tagged. Colour buildings by height source to see what is measured.</p>`;
  $('about-link').addEventListener('click', (e) => { e.preventDefault(); $('about').showModal(); });

  // ---------------------------------------------------------------- resize, loop
  window.addEventListener('resize', () => {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
  });

  const clock = new THREE.Clock();
  let hudT = 0;
  const v3 = new THREE.Vector3();
  function updateShadowCamera(cx, cy, cz, half) {
    const sc = sun.shadow.camera;
    if (Math.abs(half - app.shadowHalf) > 1) {
      sc.left = -half; sc.right = half; sc.top = half; sc.bottom = -half;
      sc.updateProjectionMatrix();
      app.shadowHalf = half;
    }
    // snap to shadow texels to reduce shimmering while moving
    const texel = (2 * half) / sun.shadow.mapSize.x;
    cx = Math.round(cx / texel) * texel; cz = Math.round(cz / texel) * texel;
    sun.target.position.set(cx, cy, cz);
    sun.position.set(cx + sunDir.x * 1200, cy + sunDir.y * 1200, cz + sunDir.z * 1200);
  }

  renderer.setAnimationLoop(() => {
    const dt = Math.min(clock.getDelta(), 0.1);
    let px, pz, yaw;
    if (renderer.xr.isPresenting) {
      xr.update(dt);
      const head = xr.getHead();
      px = head.x; pz = head.z;
      const d = camera.getWorldDirection(v3);
      yaw = Math.atan2(-d.x, -d.z);
      updateShadowCamera(px, head.y, pz, 150);
    } else if (app.mode === 'walk') {
      walk.update(dt);
      px = walk.pos.x; pz = walk.pos.z; yaw = walk.yaw;
      updateShadowCamera(px, walk.pos.y, pz, 150);
    } else {
      orbit.update();
      const t = orbit.target;
      const cx = THREE.MathUtils.clamp(t.x, -H, H), cz = THREE.MathUtils.clamp(t.z, -H, H);
      if (cx !== t.x || cz !== t.z) { camera.position.x += cx - t.x; camera.position.z += cz - t.z; t.x = cx; t.z = cz; }
      const dy = (terrain.heightAt(t.x, t.z) - t.y) * 0.15;
      t.y += dy; camera.position.y += dy;
      const floor = terrain.heightAt(camera.position.x, camera.position.z) + 3;
      if (camera.position.y < floor) camera.position.y = floor;
      const dist = camera.position.distanceTo(t);
      const near = THREE.MathUtils.clamp(dist * 0.004, 0.3, 4);
      if (Math.abs(near - camera.near) / camera.near > 0.2) { camera.near = near; camera.updateProjectionMatrix(); }
      px = t.x; pz = t.z;
      const d = camera.getWorldDirection(v3);
      yaw = Math.atan2(-d.x, -d.z);
      updateShadowCamera(t.x, t.y, t.z, THREE.MathUtils.clamp(dist * 0.85, 160, 720));
    }
    if (water && water.visible) water.material.normalMap.offset.x -= dt * 0.05;   // ripples drift downstream (~0.3 m/s)
    renderer.render(scene, camera);

    hudT += dt;
    if (hudT > 0.12) {
      hudT = 0;
      const [E, N] = toMGA(meta, px, pz);
      const [lon, lat] = toLonLat(meta, px, pz);
      const ahd = terrain.heightAt(px, pz) + meta.base_z_ahd;
      const hdg = ((-THREE.MathUtils.radToDeg(yaw) % 360) + 360) % 360;
      $('hud').textContent = `${app.mode === 'orbit' ? 'Target' : 'You'} · E ${E.toFixed(1)} N ${N.toFixed(1)} (MGA56) · ` +
        `${Math.abs(lat).toFixed(5)}°S ${lon.toFixed(5)}°E · ground ${ahd.toFixed(1)} m AHD · heading ${hdg.toFixed(0)}°`;
      minimap.draw({ x: px, z: pz, yaw, fov: THREE.MathUtils.degToRad(camera.fov * camera.aspect), mode: app.mode });
      declutterLabels();
    }
  });

  syncModeUI();
  setProgress(1, 'Ready');
  $('loading').classList.add('done');

  // Sharper ground imagery on desktop GPUs.
  if (!isMobile && renderer.capabilities.maxTextureSize >= 8192) {
    loadOrtho('data/' + meta.imagery.files['8192']).then((t) => {
      mats.terrainPhoto.map = t; mats.roofsPhoto.map = t;
      mats.terrainPhoto.needsUpdate = mats.roofsPhoto.needsUpdate = true;
      ortho.dispose();
      ortho = t;
    }).catch((e) => console.warn('8k imagery not loaded', e));
  }

  // Expose for debugging / research scripting in the console.
  window.theGap = { THREE, scene, camera, renderer, meta, features, terrain, bindex, setMode, jumpTo, walk, xr, orbit, trees, street, lotIndex };
}

function makeSkirt(terrain, H) {
  const pos = [], idx = [];
  const bottom = terrain.offset - 15;
  const sides = [
    (t) => [-H + 2 * H * t, -H], (t) => [H, -H + 2 * H * t], (t) => [H - 2 * H * t, H], (t) => [-H, H - 2 * H * t]];
  const steps = 300;
  for (const f of sides) {
    const start = pos.length / 3;
    for (let i = 0; i <= steps; i++) {
      const [x, z] = f(i / steps);
      pos.push(x, terrain.heightAt(x, z), z, x, bottom, z);
    }
    for (let i = 0; i < steps; i++) {
      const a = start + i * 2;
      idx.push(a, a + 1, a + 2, a + 1, a + 3, a + 2);
    }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  g.setIndex(idx);
  g.computeVertexNormals();
  return new THREE.Mesh(g, new THREE.MeshStandardMaterial({ color: 0x6f5f4d, roughness: 1, side: THREE.DoubleSide }));
}
