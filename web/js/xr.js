// WebXR (e.g. Meta Quest 3) locomotion: left stick smooth walk, right stick snap turn,
// trigger to aim + release to teleport. The user's rig (a Group holding the camera) is kept
// standing on the LiDAR terrain and cannot walk through buildings.
import * as THREE from 'three';
import { XRControllerModelFactory } from 'three/addons/webxr/XRControllerModelFactory.js';

const UP = new THREE.Vector3(0, 1, 0);

export class XRLocomotion {
  constructor({ renderer, scene, rig, camera, terrain, buildings }) {
    Object.assign(this, { renderer, rig, camera, terrain, buildings });
    this.speed = 2.0;                                  // m/s
    this.snap = THREE.MathUtils.degToRad(30);          // snap-turn step
    this.turnArmed = true;
    this.aiming = null;
    this.hit = null;
    this.head = new THREE.Vector3();
    this._dir = new THREE.Vector3();
    this._o = new THREE.Vector3();
    this._q = new THREE.Quaternion();

    const factory = new XRControllerModelFactory();
    this.controllers = [0, 1].map((i) => {
      const c = renderer.xr.getController(i);
      c.addEventListener('connected', (e) => { c.userData.source = e.data; });
      c.addEventListener('disconnected', () => { c.userData.source = null; });
      c.addEventListener('selectstart', () => { this.aiming = c; });
      c.addEventListener('selectend', () => {
        if (this.aiming === c) { this.teleport(); this.aiming = null; }
      });
      const ray = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3(0, 0, -1)]),
        new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.45 }));
      ray.scale.z = 4;
      c.add(ray);
      c.userData.ray = ray;
      rig.add(c);
      const grip = renderer.xr.getControllerGrip(i);
      grip.add(factory.createControllerModel(grip));
      rig.add(grip);
      return c;
    });

    this.marker = new THREE.Mesh(
      new THREE.RingGeometry(0.22, 0.34, 40).rotateX(-Math.PI / 2),
      new THREE.MeshBasicMaterial({ color: 0x3fa7d6, transparent: true, opacity: 0.95, depthTest: false }));
    this.marker.renderOrder = 999;
    this.marker.visible = false;
    scene.add(this.marker);
  }

  /** Stand the user at (x, z) facing `yaw` (0 = north). */
  start(x, z, yaw) {
    this.rig.position.set(x, this.terrain.heightAt(x, z), z);
    this.rig.rotation.set(0, yaw, 0);
  }

  getHead() { return this.camera.getWorldPosition(this.head); }

  blocked(x, z) { return !this.terrain.contains(x, z, 1) || this.buildings.hit(x, z, 0.25) >= 0; }

  move(dx, dz, head) {
    const r = this.rig.position;
    if (!this.blocked(head.x + dx, head.z + dz)) { r.x += dx; r.z += dz; }
    else if (!this.blocked(head.x + dx, head.z)) { r.x += dx; }
    else if (!this.blocked(head.x, head.z + dz)) { r.z += dz; }
  }

  turn(angle, head) {
    const r = this.rig;
    const off = new THREE.Vector3().subVectors(r.position, head).setY(0).applyAxisAngle(UP, angle);
    r.position.set(head.x + off.x, r.position.y, head.z + off.z);
    r.rotation.y += angle;
  }

  teleport() {
    this.marker.visible = false;
    if (!this.hit) return;
    const head = this.getHead();
    this.rig.position.x += this.hit.x - head.x;
    this.rig.position.z += this.hit.z - head.z;
    this.rig.position.y = this.terrain.heightAt(this.hit.x, this.hit.z);
    this.hit = null;
  }

  update(dt) {
    const head = this.getHead();
    for (const c of this.controllers) {
      const src = c.userData.source;
      if (!src || !src.gamepad) continue;
      const a = src.gamepad.axes;
      const x = (a.length >= 4 ? a[2] : a[0]) || 0;
      const y = (a.length >= 4 ? a[3] : a[1]) || 0;
      if (src.handedness === 'left') {
        if (Math.hypot(x, y) > 0.15) {
          const f = this.camera.getWorldDirection(this._dir).setY(0).normalize();
          const rx = -f.z, rz = f.x;                    // right-hand vector
          const k = this.speed * dt * Math.min(1, Math.hypot(x, y));
          const n = Math.hypot(x, y) || 1;
          this.move((f.x * -y + rx * x) / n * k, (f.z * -y + rz * x) / n * k, head);
        }
      } else if (src.handedness === 'right') {
        if (Math.abs(x) > 0.7 && this.turnArmed) { this.turn(-Math.sign(x) * this.snap, head); this.turnArmed = false; }
        if (Math.abs(x) < 0.3) this.turnArmed = true;
      }
    }

    // Teleport aiming
    if (this.aiming) {
      const c = this.aiming;
      c.getWorldPosition(this._o);
      c.getWorldQuaternion(this._q);
      const dir = this._dir.set(0, 0, -1).applyQuaternion(this._q);
      const p = this.terrain.raycast(this._o, dir, 400);
      const ok = p && this.buildings.hit(p.x, p.z, 0.25) < 0 && p.distanceTo(this._o) < 120;
      this.hit = ok ? p : null;
      this.marker.visible = !!p;
      if (p) {
        this.marker.position.set(p.x, p.y + 0.03, p.z);
        this.marker.material.color.set(ok ? 0x3fa7d6 : 0xd64f6a);
        c.userData.ray.scale.z = p.distanceTo(this._o);
      }
    } else {
      for (const c of this.controllers) c.userData.ray.scale.z = 4;
    }

    // Keep the user's feet on the ground under their head.
    const g = this.terrain.heightAt(head.x, head.z);
    this.rig.position.y += (g - this.rig.position.y) * Math.min(1, dt * 10);
  }
}
