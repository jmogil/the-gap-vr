// Street-name labels as screen-facing sprites with a constant on-screen size.
import * as THREE from 'three';

const cache = new Map();

function labelTexture(text, highlight) {
  const key = text + (highlight ? '*' : '');
  if (cache.has(key)) return cache.get(key);
  const dpr = 2, fs = 26 * dpr, padX = 14 * dpr, padY = 8 * dpr;
  const c = document.createElement('canvas');
  const ctx = c.getContext('2d');
  ctx.font = `600 ${fs}px system-ui, -apple-system, Segoe UI, sans-serif`;
  const w = Math.ceil(ctx.measureText(text).width) + padX * 2;
  const h = fs + padY * 2;
  c.width = w; c.height = h;
  ctx.font = `600 ${fs}px system-ui, -apple-system, Segoe UI, sans-serif`;
  const r = h / 2;
  ctx.fillStyle = highlight ? 'rgba(242,165,65,0.95)' : 'rgba(18,22,26,0.78)';
  ctx.beginPath();
  ctx.moveTo(r, 0); ctx.lineTo(w - r, 0); ctx.arc(w - r, r, r, -Math.PI / 2, Math.PI / 2);
  ctx.lineTo(r, h); ctx.arc(r, r, r, Math.PI / 2, -Math.PI / 2); ctx.closePath();
  ctx.fill();
  ctx.fillStyle = highlight ? '#1a1205' : '#ffffff';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, padX, h / 2 + dpr);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 4;
  const out = { tex, aspect: w / h };
  cache.set(key, out);
  return out;
}

export function makeLabel(text, highlight = false) {
  const { tex, aspect } = labelTexture(text, highlight);
  const mat = new THREE.SpriteMaterial({ map: tex, sizeAttenuation: false, depthTest: false, depthWrite: false, transparent: true });
  const s = new THREE.Sprite(mat);
  const hgt = highlight ? 0.042 : 0.034;
  s.scale.set(hgt * aspect, hgt, 1);
  s.center.set(0.5, 0);
  s.renderOrder = 10;
  return s;
}
