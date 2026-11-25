// Streetscape layers: LiDAR poles + street lights + wires, OSM signals/signs/bus stops/benches,
// street-name signs, fence palings, lot boundaries and the procedural facade (windows) shader.
// Yaw convention (from the pipeline): rotation.y = yaw points an object's -Z axis at its target.
import * as THREE from 'three';

const UP = new THREE.Vector3(0, 1, 0);

function canvasTex(w, h, draw) {
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  draw(c.getContext('2d'), w, h);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 4;
  return t;
}

function instanced(geo, mat, n, name) {
  const m = new THREE.InstancedMesh(geo, mat, Math.max(n, 1));
  m.count = n;
  m.name = name;
  m.castShadow = true;
  m.receiveShadow = true;
  return m;
}

const _m = new THREE.Matrix4(), _q = new THREE.Quaternion(), _s = new THREE.Vector3(), _p = new THREE.Vector3();
function place(mesh, i, x, y, z, yaw = 0, sx = 1, sy = 1, sz = 1) {
  _q.setFromAxisAngle(UP, yaw);
  mesh.setMatrixAt(i, _m.compose(_p.set(x, y, z), _q, _s.set(sx, sy, sz)));
}

// ------------------------------------------------------------------ textures
export function palingTexture() {
  // One metre of timber paling fence (10 palings with thin gaps); u in metres, v = 0..1 of 1.8 m.
  const t = canvasTex(256, 256, (g, w, h) => {
    g.clearRect(0, 0, w, h);
    for (let i = 0; i < 10; i++) {
      const x0 = (i / 10) * w + 1.5, pw = w / 10 - 3;
      const shade = 150 + ((i * 37) % 23) - 11;
      g.fillStyle = `rgb(${shade}, ${shade - 26}, ${shade - 52})`;
      g.fillRect(x0, 4 + ((i * 13) % 5), pw, h);
      g.fillStyle = 'rgba(60,40,25,0.18)';
      for (let k = 0; k < 6; k++) g.fillRect(x0 + ((i * 7 + k * 11) % pw), 0, 1, h);
    }
    g.fillStyle = 'rgb(118, 96, 72)';                        // rails
    g.fillRect(0, h * 0.18, w, 7);
    g.fillRect(0, h * 0.8, w, 7);
  });
  t.wrapS = THREE.RepeatWrapping;
  t.wrapT = THREE.ClampToEdgeWrapping;
  return t;
}

function signTex(kind) {
  return canvasTex(256, 256, (g, w, h) => {
    g.clearRect(0, 0, w, h);
    if (kind === 'stop') {
      g.fillStyle = '#c8102e';
      g.beginPath();
      for (let i = 0; i < 8; i++) {
        const a = Math.PI / 8 + (i * Math.PI) / 4;
        g.lineTo(w / 2 + Math.cos(a) * 122, h / 2 + Math.sin(a) * 122);
      }
      g.closePath(); g.fill();
      g.lineWidth = 8; g.strokeStyle = '#fff'; g.stroke();
      g.fillStyle = '#fff'; g.font = 'bold 74px Arial, sans-serif'; g.textAlign = 'center'; g.textBaseline = 'middle';
      g.fillText('STOP', w / 2, h / 2 + 4);
    } else if (kind === 'give_way') {
      g.fillStyle = '#c8102e';
      g.beginPath(); g.moveTo(8, 18); g.lineTo(w - 8, 18); g.lineTo(w / 2, h - 10); g.closePath(); g.fill();
      g.fillStyle = '#fff';
      g.beginPath(); g.moveTo(44, 42); g.lineTo(w - 44, 42); g.lineTo(w / 2, h - 66); g.closePath(); g.fill();
      g.fillStyle = '#111'; g.font = 'bold 30px Arial, sans-serif'; g.textAlign = 'center';
      g.fillText('GIVE', w / 2, 92); g.fillText('WAY', w / 2, 126);
    } else if (kind === 'bus') {
      g.fillStyle = '#ffd200'; g.fillRect(14, 6, w - 28, h - 12);
      g.fillStyle = '#0a5c36'; g.fillRect(14, 6, w - 28, 70);
      g.fillStyle = '#fff'; g.font = 'bold 44px Arial, sans-serif'; g.textAlign = 'center'; g.textBaseline = 'middle';
      g.fillText('BUS', w / 2, 43);
      g.fillStyle = '#111'; g.font = 'bold 40px Arial, sans-serif'; g.fillText('STOP', w / 2, 150);
    } else if (kind === 'signal') {
      g.fillStyle = '#151515'; g.fillRect(0, 0, w, h);
      [['#ff3b30', 0.2], ['#ffb000', 0.5], ['#2fd25a', 0.8]].forEach(([c, y], i) => {
        g.fillStyle = i === 2 ? c : '#2a2a2a';
        g.beginPath(); g.arc(w / 2, h * y, w * 0.3, 0, Math.PI * 2); g.fill();
      });
    }
  });
}

const bladeCache = new Map();
function bladeTex(name) {
  if (bladeCache.has(name)) return bladeCache.get(name);
  const t = canvasTex(512, 84, (g, w, h) => {
    g.fillStyle = '#0d3b66'; g.fillRect(0, 0, w, h);
    g.strokeStyle = '#fff'; g.lineWidth = 4; g.strokeRect(5, 5, w - 10, h - 10);
    g.fillStyle = '#fff'; g.textAlign = 'center'; g.textBaseline = 'middle';
    let fs = 46;
    g.font = `600 ${fs}px Arial, sans-serif`;
    while (g.measureText(name.toUpperCase()).width > w - 40 && fs > 20) { fs -= 2; g.font = `600 ${fs}px Arial, sans-serif`; }
    g.fillText(name.toUpperCase(), w / 2, h / 2 + 2);
  });
  bladeCache.set(name, t);
  return t;
}

// ------------------------------------------------------------------ facade shader
export function applyFacade(material) {
  material.onBeforeCompile = (shader) => {
    shader.vertexShader = shader.vertexShader
      .replace('#include <common>', '#include <common>\nattribute vec4 _facade;\nvarying vec4 vFacade;')
      .replace('#include <begin_vertex>', '#include <begin_vertex>\nvFacade = _facade;');
    shader.fragmentShader = shader.fragmentShader
      .replace('#include <common>', '#include <common>\nvarying vec4 vFacade;')
      .replace('#include <color_fragment>', `#include <color_fragment>
        float glassMix = 0.0;
        {
          float u = vFacade.x, v = vFacade.y, L = vFacade.z, eave = vFacade.w;
          if (eave > 2.0 && L > 1.6) {
            float bays = max(1.0, floor(L / 3.2));
            float bw = L / bays;
            float lu = mod(u, bw) - 0.5 * bw;
            float lv = mod(v, 2.9);
            float hw = clamp(bw * 0.21, 0.35, 0.85);
            if (abs(lu) < hw && lv > 0.95 && lv < 2.15 && v > 0.6 && v < eave - 0.3 && u > 0.45 && u < L - 0.45) {
              float fx = abs(lu) / hw, fy = abs(lv - 1.55) / 0.6;
              if (fx > 0.88 || fy > 0.86 || abs(lu) < 0.025) {
                diffuseColor.rgb = vec3(0.8);
              } else {
                diffuseColor.rgb = mix(vec3(0.03, 0.045, 0.06), vec3(0.14, 0.18, 0.23), clamp((lv - 0.95) / 1.2, 0.0, 1.0));
                glassMix = 1.0;
              }
            }
          }
          if (eave > 0.0) diffuseColor.rgb *= 1.0 - 0.05 * step(0.9, fract(v / 0.2));   // weatherboards
        }`)
      .replace('#include <roughnessmap_fragment>', '#include <roughnessmap_fragment>\nroughnessFactor = mix(roughnessFactor, 0.12, glassMix);');
  };
  material.customProgramCacheKey = () => 'facade-v1';
}

// ------------------------------------------------------------------ street furniture
export function buildStreetscape(d) {
  const group = new THREE.Group();
  group.name = 'streetscape';
  const pick = [];
  const wood = new THREE.MeshStandardMaterial({ color: 0x6f6252, roughness: 1 });
  const metal = new THREE.MeshStandardMaterial({ color: 0x8a8f93, roughness: 0.6, metalness: 0.3 });
  const dark = new THREE.MeshStandardMaterial({ color: 0x2b2d2f, roughness: 0.7 });
  const lamp = new THREE.MeshStandardMaterial({ color: 0xd8d6cf, roughness: 0.5, emissive: 0x000000 });

  // Power poles, crossarms and street lights (LiDAR poles; lights assumed)
  const P = d.poles || [];
  const poles = instanced(new THREE.CylinderGeometry(0.12, 0.16, 1, 8).translate(0, 0.5, 0), wood, P.length, 'poles');
  const arms = instanced(new THREE.BoxGeometry(0.1, 0.1, 2.2), wood, P.length, 'crossarms');
  const lit = P.filter((p) => p[4]);
  const larms = instanced(new THREE.BoxGeometry(0.07, 0.07, 1.8).translate(0, 0, -0.9), metal, lit.length, 'light arms');
  const heads = instanced(new THREE.BoxGeometry(0.3, 0.13, 0.62).translate(0, -0.05, -1.95), lamp, lit.length, 'lamps');
  P.forEach(([x, y, z, h, , , cross], i) => {
    place(poles, i, x, y - 0.3, z, 0, 1, h + 0.3, 1);
    place(arms, i, x, y + h - 0.35, z, cross);
  });
  lit.forEach(([x, y, z, h, , arm], i) => {
    const ah = Math.min(h - 1.2, 7.2);
    place(larms, i, x, y + ah, z, arm);
    place(heads, i, x, y + ah, z, arm);
  });
  poles.userData = { kind: 'pole', records: P };
  group.add(poles, arms, larms, heads);
  pick.push(poles);

  // Wires between poles (two conductors per span, with sag)
  const wv = [];
  for (const [x1, y1, z1, x2, y2, z2] of d.wires || []) {
    const L = Math.hypot(x2 - x1, z2 - z1);
    const nx = -(z2 - z1) / L, nz = (x2 - x1) / L, sag = 0.012 * L;
    for (const off of [-0.55, 0.55]) {
      let prev = null;
      for (let k = 0; k <= 10; k++) {
        const t = k / 10;
        const p = [x1 + (x2 - x1) * t + nx * off, y1 + (y2 - y1) * t - sag * 4 * t * (1 - t), z1 + (z2 - z1) * t + nz * off];
        if (prev) wv.push(...prev, ...p);
        prev = p;
      }
    }
  }
  if (wv.length) {
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(wv, 3));
    const wires = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color: 0x222222, transparent: true, opacity: 0.85 }));
    wires.name = 'wires';
    group.add(wires);
  }

  // Traffic signals (OSM)
  const S = d.signals || [];
  const sPoles = instanced(new THREE.CylinderGeometry(0.07, 0.08, 1, 8).translate(0, 0.5, 0), metal, S.length, 'signal poles');
  const faceMat = new THREE.MeshStandardMaterial({ map: signTex('signal'), roughness: 0.5, emissive: 0xffffff, emissiveMap: signTex('signal'), emissiveIntensity: 0.35 });
  const sHeads = instanced(new THREE.BoxGeometry(0.34, 1.0, 0.24).translate(0, 0, -0.2),
    [dark, dark, dark, dark, dark, faceMat], S.length, 'signal heads');
  S.forEach(([x, y, z, yaw], i) => { place(sPoles, i, x, y - 0.2, z, 0, 1, 3.8, 1); place(sHeads, i, x, y + 3.0, z, yaw); });
  sPoles.userData = { kind: 'signal', records: S };
  group.add(sPoles, sHeads);
  pick.push(sPoles);

  // Give-way / stop signs and bus stops (OSM)
  const signPost = new THREE.CylinderGeometry(0.035, 0.035, 1, 6).translate(0, 0.5, 0);
  const plate = new THREE.PlaneGeometry(0.75, 0.75).rotateY(Math.PI).translate(0, 0, -0.04);   // faces -Z
  const back = new THREE.PlaneGeometry(0.72, 0.72).translate(0, 0, -0.035);
  const backMat = new THREE.MeshStandardMaterial({ color: 0x9a9ea2, roughness: 0.6 });
  for (const kind of ['give_way', 'stop']) {
    const rec = (d.signs || []).filter((s) => s[0] === kind);
    if (!rec.length) continue;
    const posts = instanced(signPost, metal, rec.length, `${kind} posts`);
    const faces = instanced(plate, new THREE.MeshStandardMaterial({ map: signTex(kind), transparent: true, alphaTest: 0.5, roughness: 0.5 }), rec.length, kind);
    const backs = instanced(back, backMat, rec.length, `${kind} backs`);
    rec.forEach(([, x, y, z, yaw], i) => {
      place(posts, i, x, y - 0.2, z, 0, 1, 2.45, 1);
      place(faces, i, x, y + 2.05, z, yaw);
      place(backs, i, x, y + 2.05, z, yaw);
    });
    posts.userData = { kind, records: rec };
    group.add(posts, faces, backs);
    pick.push(posts);
  }
  const B = d.bus_stops || [];
  if (B.length) {
    const posts = instanced(signPost, metal, B.length, 'bus stop posts');
    const faces = instanced(new THREE.PlaneGeometry(0.45, 0.6), new THREE.MeshStandardMaterial({ map: signTex('bus'), roughness: 0.5, side: THREE.DoubleSide }), B.length, 'bus stop signs');
    B.forEach(([x, y, z, yaw], i) => { place(posts, i, x, y - 0.2, z, 0, 1, 3.0, 1); place(faces, i, x, y + 2.45, z, yaw); });
    posts.userData = { kind: 'bus_stop', records: B };
    group.add(posts, faces);
    pick.push(posts);
  }

  // Benches (OSM)
  const N = d.benches || [];
  if (N.length) {
    const benchGeo = new THREE.BoxGeometry(1.6, 0.06, 0.42).translate(0, 0.45, 0);
    const backGeo = new THREE.BoxGeometry(1.6, 0.4, 0.05).translate(0, 0.72, 0.2);
    const timber = new THREE.MeshStandardMaterial({ color: 0x8a6a48, roughness: 0.9 });
    const seats = instanced(benchGeo, timber, N.length, 'benches');
    const backs = instanced(backGeo, timber, N.length, 'bench backs');
    N.forEach(([x, y, z, yaw], i) => { place(seats, i, x, y, z, yaw); place(backs, i, x, y, z, yaw); });
    seats.userData = { kind: 'bench', records: N };
    group.add(seats, backs);
    pick.push(seats);
  }

  // Street-name signs at intersections (names from OSM)
  const nameGroup = new THREE.Group();
  const nPosts = instanced(signPost, metal, (d.name_signs || []).length, 'name sign posts');
  (d.name_signs || []).forEach((s, i) => {
    place(nPosts, i, s.x, s.y - 0.2, s.z, 0, 1, 3.1, 1);
    s.blades.forEach(([name, yaw], k) => {
      const tex = bladeTex(name);
      const mat = new THREE.MeshStandardMaterial({ map: tex, roughness: 0.5 });
      const edge = new THREE.MeshStandardMaterial({ color: 0x0d3b66, roughness: 0.5 });
      const blade = new THREE.Mesh(new THREE.BoxGeometry(1.15, 0.19, 0.025), [edge, edge, edge, edge, mat, mat]);
      blade.position.set(s.x, s.y + 2.62 + k * 0.24, s.z);
      blade.rotation.y = yaw + Math.PI / 2;           // blade runs along its street
      blade.castShadow = true;
      nameGroup.add(blade);
    });
  });
  nPosts.userData = { kind: 'name_sign', records: d.name_signs || [] };
  group.add(nPosts, nameGroup);
  pick.push(nPosts);

  for (const m of group.children) if (m.isInstancedMesh) { m.instanceMatrix.needsUpdate = true; m.computeBoundingSphere(); }
  return { group, pick, lamp };
}

// ------------------------------------------------------------------ lots
export function buildLotLines(lots, terrain) {
  const v = [];
  for (const line of lots.boundaries || []) {
    for (let i = 0; i + 3 < line.length; i += 2) {
      const [x1, z1, x2, z2] = [line[i], line[i + 1], line[i + 2], line[i + 3]];
      v.push(x1, terrain.heightAt(x1, z1) + 0.12, z1, x2, terrain.heightAt(x2, z2) + 0.12, z2);
    }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(v, 3));
  const lines = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.75 }));
  lines.name = 'lot boundaries';
  return lines;
}

export class LotIndex {
  constructor(lots, cell = 25) {
    this.lots = lots;
    this.cell = cell;
    this.map = new Map();
    lots.forEach((l, i) => {
      let x0 = Infinity, z0 = Infinity, x1 = -Infinity, z1 = -Infinity;
      for (const [x, z] of l.ring) { x0 = Math.min(x0, x); x1 = Math.max(x1, x); z0 = Math.min(z0, z); z1 = Math.max(z1, z); }
      l.bb = [x0, z0, x1, z1];
      for (let cx = Math.floor(x0 / cell); cx <= Math.floor(x1 / cell); cx++)
        for (let cz = Math.floor(z0 / cell); cz <= Math.floor(z1 / cell); cz++) {
          const k = cx + ',' + cz;
          if (!this.map.has(k)) this.map.set(k, []);
          this.map.get(k).push(i);
        }
    });
  }

  at(x, z) {
    for (const i of this.map.get(Math.floor(x / this.cell) + ',' + Math.floor(z / this.cell)) || []) {
      const l = this.lots[i];
      if (x < l.bb[0] || x > l.bb[2] || z < l.bb[1] || z > l.bb[3]) continue;
      let inside = false;
      const r = l.ring;
      for (let a = 0, b = r.length - 1; a < r.length; b = a++) {
        if ((r[a][1] > z) !== (r[b][1] > z) && x < ((r[b][0] - r[a][0]) * (z - r[a][1])) / (r[b][1] - r[a][1]) + r[a][0]) inside = !inside;
      }
      if (inside) return l;
    }
    return null;
  }
}
