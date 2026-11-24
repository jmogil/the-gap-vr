// Solar position (standard low-precision astronomical formulas, accurate to ~0.1°).
const rad = Math.PI / 180;
const e = rad * 23.4397; // obliquity of the Earth

function toDays(date) { return date.valueOf() / 86400000 - 0.5 + 2440588 - 2451545; }

/** Sun azimuth (radians clockwise from north) and elevation (radians) for a Date at lat/lon. */
export function sunPosition(date, lat, lon) {
  const d = toDays(date);
  const M = rad * (357.5291 + 0.98560028 * d);
  const C = rad * (1.9148 * Math.sin(M) + 0.02 * Math.sin(2 * M) + 0.0003 * Math.sin(3 * M));
  const L = M + C + rad * 102.9372 + Math.PI;
  const dec = Math.asin(Math.sin(e) * Math.sin(L));
  const ra = Math.atan2(Math.sin(L) * Math.cos(e), Math.cos(L));
  const lw = rad * -lon, phi = rad * lat;
  const H = rad * (280.16 + 360.9856235 * d) - lw - ra;
  const elevation = Math.asin(Math.sin(phi) * Math.sin(dec) + Math.cos(phi) * Math.cos(dec) * Math.cos(H));
  const azSouth = Math.atan2(Math.sin(H), Math.cos(H) * Math.sin(phi) - Math.tan(dec) * Math.cos(phi));
  return { azimuth: azSouth + Math.PI, elevation };
}

/** Brisbane local time (AEST, UTC+10, no daylight saving) -> Date. */
export function brisbaneDate(isoDate, minutes) {
  const [y, m, d] = isoDate.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d, 0, 0) + (minutes - 600) * 60000);
}

export function brisbaneToday() {
  const now = new Date(Date.now() + 10 * 3600000);
  return { iso: now.toISOString().slice(0, 10), minutes: now.getUTCHours() * 60 + now.getUTCMinutes() };
}
