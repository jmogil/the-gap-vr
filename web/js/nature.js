// Trees (LiDAR positions, sizes and photo colours) built from leaf-cluster cards, and creek water.
// All textures are generated procedurally, so nothing extra is downloaded.
import * as THREE from 'three';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';

function rng(seed) {                      // mulberry32: deterministic per tree
  return () => {
    seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const clamp = (v, a, b) => Math.min(Math.max(v, a), b);

function canvasTexture(size, draw, srgb = true) {
  const c = document.createElement('canvas');
  c.width = c.height = size;
  draw(c.getContext('2d'), size);
  const t = new THREE.CanvasTexture(c);
  if (srgb) t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 8;
  return t;
}

// A clump of foliage: ~1,500 small leaves, denser and lighter towards the top-centre,
// transparent between leaves so light and shadow come through.
function leafTexture() {
  return canvasTexture(512, (g, S) => {
    const r = rng(11);
    g.clearRect(0, 0, S, S);
    g.strokeStyle = 'rgba(90,70,50,0.55)';
    g.lineWidth = 2.2;
    for (let k = 0; k < 10; k++) {          // twigs under the leaves
      const a = r() * Math.PI * 2;
      g.beginPath(); g.moveTo(S / 2, S * 0.6);
      g.lineTo(S / 2 + Math.cos(a) * S * 0.38, S * 0.55 + Math.sin(a) * S * 0.34); g.stroke();
    }
    for (let i = 0; i < 1500; i++) {
      const a = r() * Math.PI * 2;
      const d = Math.pow(r(), 0.7) * S * 0.47;
      const x = S / 2 + Math.cos(a) * d, y = S / 2 + Math.sin(a) * d * 0.9;
      const len = S * (0.02 + r() * 0.03), wid = len * (0.3 + r() * 0.22);
      const lift = 1 - y / S;                    // brighter towards the top
      const l = 140 + 85 * lift + (r() - 0.5) * 80;
      g.fillStyle = `rgb(${clamp(l * 0.84, 0, 255) | 0},${clamp(l, 0, 255) | 0},${clamp(l * 0.7, 0, 255) | 0})`;
      g.beginPath(); g.ellipse(x, y, len / 2, wid / 2, r() * Math.PI, 0, Math.PI * 2); g.fill();
    }
  });
}

function barkTexture() {
  const t = canvasTexture(128, (g, S) => {
    const r = rng(5);
    g.fillStyle = '#6f6253'; g.fillRect(0, 0, S, S);
    for (let i = 0; i < 260; i++) {
      const x = r() * S, w = 1 + r() * 3, sh = 70 + r() * 70;
      g.fillStyle = `rgba(${sh},${sh * 0.88},${sh * 0.75},0.55)`;
      g.fillRect(x, 0, w, S);
    }
  });
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  return t;
}

export function makeTrees(rows, lowSpec) {
  // Each crown = several clusters; each cluster = crossed leaf cards (2 upright + 1 flat).
  const cards = lowSpec
    ? [new THREE.PlaneGeometry(1, 1), new THREE.PlaneGeometry(1, 1).rotateY(Math.PI / 2)]
    : [new THREE.PlaneGeometry(1, 1), new THREE.PlaneGeometry(1, 1).rotateY(Math.PI / 2),
      new THREE.PlaneGeometry(1, 1).rotateX(-Math.PI / 2)];
  const cluster = mergeGeometries(cards);
  const plan = [];
  let total = 0;
  rows.forEach(([x, y, z, h, rad], i) => {
    const base = h * (h > 8 ? 0.4 : 0.28);
    const ry = Math.max((h - base) / 2, 0.6), rx = Math.max(rad, 0.8);
    const n = clamp(Math.round(3 + rx * rx * 0.8), 4, lowSpec ? 10 : 24);
    plan.push({ x, y, z, h, rx, ry, base, n, i });
    total += n;
  });
  const leaves = leafTexture();
  const leafMat = new THREE.MeshStandardMaterial({ map: leaves, alphaTest: 0.5, side: THREE.DoubleSide, roughness: 0.85, metalness: 0 });
  leafMat.onBeforeCompile = (sh) => {
    // Shade the crown like a rounded mass: normals point out from the crown centre.
    sh.vertexShader = sh.vertexShader
      .replace('#include <common>', '#include <common>\nattribute vec3 aCenter;')
      .replace('#include <defaultnormal_vertex>', `#include <defaultnormal_vertex>
        vec3 crownOut = normalize( (instanceMatrix * vec4( position, 1.0 )).xyz - aCenter );
        transformedNormal = normalize( normalMatrix * normalize( crownOut + vec3( 0.0, 0.3, 0.0 ) ) );`);
    sh.fragmentShader = sh.fragmentShader.replace('#include <normal_fragment_begin>',
      THREE.ShaderChunk.normal_fragment_begin.replace('normal *= faceDirection;', ''));
  };
  leafMat.customProgramCacheKey = () => 'leaf-cards-v1';
  const foliage = new THREE.InstancedMesh(cluster, leafMat, total);
  const center = new Float32Array(total * 3);
  const treeIndex = new Int32Array(total);
  const trunkGeo = new THREE.CylinderGeometry(0.75, 1, 1, 7, 1, true).translate(0, 0.5, 0);
  const bark = new THREE.MeshStandardMaterial({ map: barkTexture(), roughness: 1 });
  const trunks = new THREE.InstancedMesh(trunkGeo, bark, rows.length);
  const big = plan.filter((p) => p.h >= 7);
  const branches = new THREE.InstancedMesh(new THREE.CylinderGeometry(0.6, 1, 1, 5, 1, true).translate(0, 0.5, 0), bark, big.length * 2);
  const m = new THREE.Matrix4(), q = new THREE.Quaternion(), q2 = new THREE.Quaternion(), e = new THREE.Euler();
  const pos = new THREE.Vector3(), scl = new THREE.Vector3(), col = new THREE.Color(), base = new THREE.Color(), hsl = {};
  const fallback = new THREE.Color(0x5d7a45);
  let k = 0, b = 0;
  for (const p of plan) {
    const row = rows[p.i], r = rng(p.i * 7919 + 17), hex = row[6], overRoof = row[5];
    if (hex) {
      base.set('#' + hex).getHSL(hsl);
      base.setHSL(hsl.h, Math.min(hsl.s * 1.1, 0.55), clamp(hsl.l * 1.25, 0.2, 0.5));
    } else base.copy(fallback);
    const cy = p.y + p.base + p.ry;
    const size = clamp(0.95 * Math.min(p.rx, p.ry * 1.2) + 0.4, 1.2, 5.0);
    for (let c = 0; c < p.n; c++, k++) {
      const u = 2 * r() - 1, phi = 2 * Math.PI * r(), s = Math.sqrt(1 - u * u);
      const d = 0.15 + 0.55 * Math.sqrt(r());
      pos.set(p.x + s * Math.cos(phi) * p.rx * d, cy + u * p.ry * d, p.z + s * Math.sin(phi) * p.rx * d);
      q.setFromEuler(e.set((r() - 0.5) * 0.6, r() * Math.PI * 2, (r() - 0.5) * 0.6));
      const sc = size * (0.75 + 0.5 * r());
      foliage.setMatrixAt(k, m.compose(pos, q, scl.set(sc, sc, sc)));
      foliage.setColorAt(k, col.copy(base).multiplyScalar(0.86 + 0.28 * r()));
      center[k * 3] = p.x; center[k * 3 + 1] = cy; center[k * 3 + 2] = p.z;
      treeIndex[k] = p.i;
    }
    const tr = overRoof ? 0 : 0.1 + 0.012 * p.h;          // crowns over roofs: hide the trunk
    q.identity();
    trunks.setMatrixAt(p.i, m.compose(pos.set(p.x, p.y - 0.3, p.z), q, scl.set(tr, overRoof ? 0 : p.base + p.ry * 0.5 + 0.3, tr)));
    if (p.h >= 7) {
      for (let j = 0; j < 2; j++, b++) {                  // two main limbs into the crown
        q.setFromAxisAngle(new THREE.Vector3(0, 1, 0), r() * Math.PI * 2 + j * Math.PI);
        q2.setFromAxisAngle(new THREE.Vector3(1, 0, 0), 0.55 + r() * 0.35);
        branches.setMatrixAt(b, m.compose(pos.set(p.x, p.y + p.base * 0.85, p.z), q.multiply(q2),
          scl.set(tr * 0.55, overRoof ? 0 : p.ry * 0.95, tr * 0.55)));
      }
    }
  }
  cluster.setAttribute('aCenter', new THREE.InstancedBufferAttribute(center, 3));
  for (const im of [foliage, trunks, branches]) {
    im.instanceMatrix.needsUpdate = true;
    im.computeBoundingSphere();
    im.castShadow = im.receiveShadow = true;
  }
  foliage.instanceColor.needsUpdate = true;
  foliage.userData.treeIndex = treeIndex;
  const group = new THREE.Group();
  group.name = 'trees';
  group.add(foliage, trunks, branches);
  return { group, crowns: foliage, trunks, treeIndex };
}

// ------------------------------------------------------------------ water
function waterNormalTexture() {
  // Tileable ripples: ~28 waves in scattered directions with integer wave numbers (so the
  // tile wraps), weaker at higher frequency. No few waves dominate, so no visible grid.
  const S = 256, r = rng(3), waves = [];
  while (waves.length < 28) {
    const kx = Math.round((r() - 0.5) * 26), ky = Math.round((r() - 0.5) * 26), kk = Math.hypot(kx, ky);
    if (kk >= 2.5) waves.push([kx, ky, Math.pow(kk, -1.25), r() * Math.PI * 2]);
  }
  const gx = new Float32Array(S * S), gy = new Float32Array(S * S);
  let ss = 0;
  for (let y = 0; y < S; y++) for (let x = 0; x < S; x++) {
    let dx = 0, dy = 0;
    for (const [kx, ky, a, ph] of waves) {
      const c = a * Math.cos(2 * Math.PI * (kx * x + ky * y) / S + ph);
      dx += c * kx; dy += c * ky;
    }
    gx[y * S + x] = dx; gy[y * S + x] = dy; ss += dx * dx + dy * dy;
  }
  const f = 0.45 / Math.sqrt(ss / (S * S));          // rms slope 0.45 before normalScale
  const t = canvasTexture(S, (g) => {
    const img = g.createImageData(S, S);
    for (let i = 0; i < S * S; i++) {
      const nx = -gx[i] * f, ny = -gy[i] * f, l = Math.hypot(nx, ny, 1);
      img.data[i * 4] = (nx / l * 0.5 + 0.5) * 255; img.data[i * 4 + 1] = (ny / l * 0.5 + 0.5) * 255;
      img.data[i * 4 + 2] = (1 / l * 0.5 + 0.5) * 255; img.data[i * 4 + 3] = 255;
    }
    g.putImageData(img, 0, 0);
  }, false);
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  t.repeat.set(1 / 6, 1 / 6);                 // UVs are metres: one ripple tile every 6 m
  return t;
}

export function makeWaterMaterial() {
  // Creeks here run under trees, so they mostly mirror dark canopy, not open sky: the
  // sky reflection is kept faint and the body colour is a dark tea-green.
  return new THREE.MeshStandardMaterial({
    color: 0x2a3a30, roughness: 0.22, metalness: 0, transparent: true, opacity: 0.93,
    normalMap: waterNormalTexture(), normalScale: new THREE.Vector2(0.3, 0.3), envMapIntensity: 0.06,
    polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -2,
  });
}

/** Sky reflections: re-renders the sky into an environment map whenever the sun moves. */
export function skyEnvironment(renderer, skyMaterial) {
  const pmrem = new THREE.PMREMGenerator(renderer);
  const envScene = new THREE.Scene();
  const box = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), skyMaterial);
  box.scale.setScalar(80);
  envScene.add(box);
  let rt = null;
  return () => {
    const old = rt;
    rt = pmrem.fromScene(envScene, 0, 0.1, 200);
    if (old) old.dispose();
    return rt.texture;
  };
}
