// North-up overview map (aerial thumbnail) with the study area, Yuruga St and the viewer position.
export class Minimap {
  constructor(canvas, meta, imageUrl, onPick) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.H = meta.half_size_m;
    this.outline = meta.focus_outline.map((p) => [p[0], p[2]]);
    this.street = meta.focus_street;
    this.img = new Image();
    this.img.onload = () => { this.ready = true; };
    this.img.src = imageUrl;
    canvas.addEventListener('click', (e) => {
      const r = canvas.getBoundingClientRect();
      const u = (e.clientX - r.left) / r.width, v = (e.clientY - r.top) / r.height;
      onPick?.(u * 2 * this.H - this.H, v * 2 * this.H - this.H);
    });
  }

  px(x, z) {
    const W = this.canvas.width;
    return [((x + this.H) / (2 * this.H)) * W, ((z + this.H) / (2 * this.H)) * W];
  }

  draw({ x, z, yaw, fov, mode }) {
    if (!this.ready) return;
    const c = this.ctx, W = this.canvas.width;
    c.drawImage(this.img, 0, 0, W, W);
    c.fillStyle = 'rgba(10,12,14,0.18)';
    c.fillRect(0, 0, W, W);
    // study area
    c.beginPath();
    this.outline.forEach(([ox, oz], i) => { const [u, v] = this.px(ox, oz); i ? c.lineTo(u, v) : c.moveTo(u, v); });
    c.closePath();
    c.setLineDash([8, 6]);
    c.lineWidth = 2.5;
    c.strokeStyle = 'rgba(242,165,65,0.95)';
    c.stroke();
    c.setLineDash([]);
    // Yuruga Street
    c.beginPath();
    this.street.forEach(([sx, sz], i) => { const [u, v] = this.px(sx, sz); i ? c.lineTo(u, v) : c.moveTo(u, v); });
    c.lineWidth = 6;
    c.strokeStyle = '#f2a541';
    c.stroke();
    // viewer
    const [u, v] = this.px(x, z);
    const dir = -yaw - Math.PI / 2;                   // yaw 0 = north = up on the map
    const half = Math.max(0.35, (fov || 1) / 2);
    const g = c.createRadialGradient(u, v, 0, u, v, 60);
    g.addColorStop(0, 'rgba(63,167,214,0.55)');
    g.addColorStop(1, 'rgba(63,167,214,0)');
    c.fillStyle = g;
    c.beginPath();
    c.moveTo(u, v);
    c.arc(u, v, 60, dir - half, dir + half);
    c.closePath();
    c.fill();
    c.beginPath();
    c.arc(u, v, mode === 'orbit' ? 7 : 8, 0, Math.PI * 2);
    c.fillStyle = mode === 'orbit' ? '#ffffff' : '#3fa7d6';
    c.fill();
    c.lineWidth = 2.5;
    c.strokeStyle = '#0e1114';
    c.stroke();
    // north arrow
    c.fillStyle = 'rgba(255,255,255,0.9)';
    c.font = '600 22px system-ui, sans-serif';
    c.textAlign = 'center';
    c.fillText('N', W - 22, 30);
    c.beginPath();
    c.moveTo(W - 22, 36); c.lineTo(W - 29, 50); c.lineTo(W - 15, 50); c.closePath();
    c.fill();
  }
}
