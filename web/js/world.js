// World helpers: terrain height queries, building collision index, geo conversion.
// Frame (three.js): x = east (m), y = up (m above base_z AHD), z = south (m) — so north is -z.
import * as THREE from 'three';

export class TerrainHeights {
  constructor(meta, buffer) {
    this.H = meta.half_size_m;
    this.n = meta.terrain.size;
    this.scale = meta.terrain.scale;
    this.offset = meta.terrain.zmin_ahd - meta.base_z_ahd;
    this.base = meta.base_z_ahd;
    this.data = new Uint16Array(buffer);
  }

  /** Terrain height (three.js y) at three.js (x, z). Bilinear on the 1 m LiDAR grid. */
  heightAt(x, z) {
    const n = this.n, H = this.H, d = this.data;
    const c = Math.min(Math.max(x + H, 0), n - 1.0001);
    const r = Math.min(Math.max(z + H, 0), n - 1.0001);   // row 0 = north edge (z = -H)
    const c0 = c | 0, r0 = r | 0, fc = c - c0, fr = r - r0, i = r0 * n + c0;
    const h = (d[i] * (1 - fc) + d[i + 1] * fc) * (1 - fr) + (d[i + n] * (1 - fc) + d[i + n + 1] * fc) * fr;
    return h * this.scale + this.offset;
  }

  contains(x, z, margin = 0) {
    return Math.abs(x) <= this.H - margin && Math.abs(z) <= this.H - margin;
  }

  /** March a ray against the height field. Returns a Vector3 hit or null. */
  raycast(origin, dir, maxDist = 2500) {
    const p = new THREE.Vector3();
    let above = origin.y - this.heightAt(origin.x, origin.z);
    if (above < 0) return null;
    let t = 0, prevT = 0;
    while (t < maxDist) {
      const step = Math.min(Math.max(above * 0.5, 0.25), 15);
      prevT = t;
      t += step;
      p.copy(origin).addScaledVector(dir, t);
      if (!this.contains(p.x, p.z) && p.y > 400) return null;
      above = p.y - this.heightAt(p.x, p.z);
      if (above <= 0) {
        let lo = prevT, hi = t;
        for (let i = 0; i < 24; i++) {
          const mid = (lo + hi) / 2;
          p.copy(origin).addScaledVector(dir, mid);
          if (p.y - this.heightAt(p.x, p.z) > 0) lo = mid; else hi = mid;
        }
        p.copy(origin).addScaledVector(dir, hi);
        return this.contains(p.x, p.z) ? p : null;
      }
    }
    return null;
  }
}

function pointInPoly(x, z, fp) {
  let inside = false;
  for (let i = 0, j = fp.length - 1; i < fp.length; j = i++) {
    const xi = fp[i][0], zi = fp[i][1], xj = fp[j][0], zj = fp[j][1];
    if ((zi > z) !== (zj > z) && x < ((xj - xi) * (z - zi)) / (zj - zi) + xi) inside = !inside;
  }
  return inside;
}

function distToPoly(x, z, fp) {
  let best = Infinity;
  for (let i = 0, j = fp.length - 1; i < fp.length; j = i++) {
    const ax = fp[j][0], az = fp[j][1], bx = fp[i][0], bz = fp[i][1];
    const dx = bx - ax, dz = bz - az, L2 = dx * dx + dz * dz || 1e-9;
    let t = ((x - ax) * dx + (z - az) * dz) / L2;
    t = Math.max(0, Math.min(1, t));
    const px = ax + t * dx - x, pz = az + t * dz - z;
    best = Math.min(best, px * px + pz * pz);
  }
  return Math.sqrt(best);
}

/** Uniform-grid index of building footprints for walking collisions and teleport checks. */
export class BuildingIndex {
  constructor(buildings, cell = 20) {
    this.cell = cell;
    this.map = new Map();
    this.items = [];
    buildings.forEach((b, i) => {
      if (b.kind === 'canopy') return; // carports etc. can be walked under
      const fp = b.footprint;
      let x0 = Infinity, z0 = Infinity, x1 = -Infinity, z1 = -Infinity;
      for (const [x, z] of fp) { x0 = Math.min(x0, x); x1 = Math.max(x1, x); z0 = Math.min(z0, z); z1 = Math.max(z1, z); }
      this.items[i] = { fp, bb: [x0, z0, x1, z1] };
      for (let cx = Math.floor((x0 - 1) / cell); cx <= Math.floor((x1 + 1) / cell); cx++)
        for (let cz = Math.floor((z0 - 1) / cell); cz <= Math.floor((z1 + 1) / cell); cz++) {
          const k = cx + ',' + cz;
          if (!this.map.has(k)) this.map.set(k, []);
          this.map.get(k).push(i);
        }
    });
  }

  /** Index of the building at (x, z) (within `pad` metres of its outline), or -1. */
  hit(x, z, pad = 0.3) {
    const list = this.map.get(Math.floor(x / this.cell) + ',' + Math.floor(z / this.cell));
    if (!list) return -1;
    for (const i of list) {
      const { fp, bb } = this.items[i];
      if (x < bb[0] - pad || x > bb[2] + pad || z < bb[1] - pad || z > bb[3] + pad) continue;
      if (pointInPoly(x, z, fp) || (pad > 0 && distToPoly(x, z, fp) < pad)) return i;
    }
    return -1;
  }
}

/** Local three.js (x, z) -> lon/lat via the affine fit stored in scene.json. */
export function toLonLat(meta, x, z) {
  const n = -z, a = meta.geo_affine;
  return [a.lon[0] + a.lon[1] * x + a.lon[2] * n, a.lat[0] + a.lat[1] * x + a.lat[2] * n];
}

export function toMGA(meta, x, z) {
  return [meta.anchor.easting + x, meta.anchor.northing - z];
}
