// First-person walking on desktop: WASD + mouse look (pointer lock or drag), terrain following,
// building collisions, optional fly mode.
import * as THREE from 'three';

export class WalkControls {
  constructor(camera, dom, terrain, buildings) {
    this.camera = camera;
    this.dom = dom;
    this.terrain = terrain;
    this.buildings = buildings;
    this.enabled = false;
    this.eye = 1.65;          // eye height (m)
    this.speed = 2.0;         // brisk walk (m/s)
    this.runSpeed = 6.0;      // Shift
    this.fly = false;
    this.pos = new THREE.Vector3();
    this.yaw = 0;             // 0 = facing north (-z)
    this.pitch = 0;
    this.keys = new Set();
    this.dragging = false;
    this.onChange = null;

    window.addEventListener('keydown', (e) => {
      if (!this.enabled || e.target.closest?.('input, textarea, select')) return;
      this.keys.add(e.code);
      if (e.code === 'KeyF') { this.fly = !this.fly; this.onChange?.(); }
      if (e.code.startsWith('Arrow') || e.code === 'Space') e.preventDefault();
    });
    window.addEventListener('keyup', (e) => this.keys.delete(e.code));
    window.addEventListener('blur', () => this.keys.clear());
    document.addEventListener('pointerlockchange', () => this.onChange?.());
    dom.addEventListener('mousemove', (e) => {
      if (!this.enabled) return;
      if (this.locked || this.dragging) {
        const k = this.locked ? 0.0022 : 0.004;
        this.yaw -= e.movementX * k;
        this.pitch = THREE.MathUtils.clamp(this.pitch - e.movementY * k, -1.45, 1.45);
      }
    });
    dom.addEventListener('pointerdown', (e) => { if (this.enabled && e.button === 2) this.dragging = true; });
    window.addEventListener('pointerup', () => { this.dragging = false; });
  }

  get locked() { return document.pointerLockElement === this.dom; }

  lock() { if (this.enabled && !this.locked) this.dom.requestPointerLock?.(); }

  enable(x, z, yaw = this.yaw) {
    this.pos.set(x, this.terrain.heightAt(x, z), z);
    this.yaw = yaw;
    this.pitch = -0.05;
    this.enabled = true;
    this.apply();
  }

  disable() {
    this.enabled = false;
    this.keys.clear();
    if (this.locked) document.exitPointerLock();
  }

  tryMove(dx, dz) {
    const p = this.pos, t = this.terrain;
    const blocked = (x, z) => !t.contains(x, z, 1) || (!this.fly && this.buildings.hit(x, z) >= 0);
    if (!blocked(p.x + dx, p.z + dz)) { p.x += dx; p.z += dz; return; }
    if (!blocked(p.x + dx, p.z)) { p.x += dx; return; }       // slide along walls
    if (!blocked(p.x, p.z + dz)) { p.z += dz; }
  }

  update(dt) {
    if (!this.enabled) return;
    const k = this.keys;
    const f = (k.has('KeyW') || k.has('ArrowUp') ? 1 : 0) - (k.has('KeyS') || k.has('ArrowDown') ? 1 : 0);
    const s = (k.has('KeyD') || k.has('ArrowRight') ? 1 : 0) - (k.has('KeyA') || k.has('ArrowLeft') ? 1 : 0);
    const run = k.has('ShiftLeft') || k.has('ShiftRight');
    const speed = (this.fly ? 4 : 1) * (run ? this.runSpeed : this.speed);
    if (f || s) {
      const fx = -Math.sin(this.yaw), fz = -Math.cos(this.yaw);
      const rx = Math.cos(this.yaw), rz = -Math.sin(this.yaw);
      let dx = fx * f + rx * s, dz = fz * f + rz * s;
      const len = Math.hypot(dx, dz);
      dx *= (speed * dt) / len; dz *= (speed * dt) / len;
      this.tryMove(dx, dz);
    }
    const ground = this.terrain.heightAt(this.pos.x, this.pos.z);
    if (this.fly) {
      const v = (k.has('KeyE') || k.has('Space') ? 1 : 0) - (k.has('KeyQ') || k.has('KeyC') ? 1 : 0);
      this.pos.y = Math.max(this.pos.y + v * speed * dt, ground);
    } else {
      // follow the ground smoothly (kerbs, slopes)
      this.pos.y += (ground - this.pos.y) * Math.min(1, dt * 12);
    }
    this.apply();
  }

  apply() {
    this.camera.position.set(this.pos.x, this.pos.y + this.eye, this.pos.z);
    this.camera.rotation.set(this.pitch, this.yaw, 0, 'YXZ');
  }
}
