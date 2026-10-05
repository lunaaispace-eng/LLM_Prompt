// Mask edits on m = {w, h, data: Uint8ClampedArray}, at native image resolution. Pure.
// Boxes and bboxes are [x0, y0, x1, y1] with x1 / y1 exclusive.
export function create(w, h) {
  return { w, h, data: new Uint8ClampedArray(w * h) };
}

function stampDisc(m, cx, cy, r, value) {
  const r2 = r * r;
  const y0 = Math.max(0, cy - r);
  const y1 = Math.min(m.h - 1, cy + r);
  for (let y = y0; y <= y1; y++) {
    const dy = y - cy;
    const half = Math.floor(Math.sqrt(Math.max(0, r2 - dy * dy)));
    const x0 = Math.max(0, cx - half);
    const x1 = Math.min(m.w - 1, cx + half);
    for (let x = x0; x <= x1; x++) m.data[y * m.w + x] = value;
  }
}

// Brush / eraser: a disc of `radius` pixels along the line. value 255 marks, 0 erases.
// Radius is rounded: a brush size divided by the view zoom is often fractional, and a
// fractional radius makes the disc walk fractional row indices.
export function stampLine(m, x0, y0, x1, y1, radius, value) {
  const r = Math.max(0, Math.round(Number.isFinite(radius) ? radius : 0));
  const steps = Math.max(1, Math.ceil(Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0))));
  for (let i = 0; i <= steps; i++) {
    const t = i / steps;
    stampDisc(m, Math.round(x0 + (x1 - x0) * t), Math.round(y0 + (y1 - y0) * t), r, value);
  }
  return m;
}

export function fillBox(m, box, value) {
  const x0 = Math.max(0, Math.min(box[0], box[2]));
  const y0 = Math.max(0, Math.min(box[1], box[3]));
  const x1 = Math.min(m.w, Math.max(box[0], box[2]));
  const y1 = Math.min(m.h, Math.max(box[1], box[3]));
  for (let y = y0; y < y1; y++) m.data.fill(value, y * m.w + x0, y * m.w + x1);
  return m;
}

export function invert(m) {
  for (let i = 0; i < m.data.length; i++) m.data[i] = 255 - m.data[i];
  return m;
}

export function clear(m) {
  m.data.fill(0);
  return m;
}

export function union(a, b) {
  if (a.w !== b.w || a.h !== b.h) throw new Error("union: mask sizes differ");
  const out = create(a.w, a.h);
  for (let i = 0; i < out.data.length; i++) out.data[i] = Math.max(a.data[i], b.data[i]);
  return out;
}

export function bbox(m) {
  let x0 = m.w, y0 = m.h, x1 = -1, y1 = -1;
  for (let y = 0; y < m.h; y++) {
    for (let x = 0; x < m.w; x++) {
      if (!m.data[y * m.w + x]) continue;
      if (x < x0) x0 = x;
      if (x > x1) x1 = x;
      if (y < y0) y0 = y;
      y1 = y;
    }
  }
  return x1 < 0 ? null : [x0, y0, x1 + 1, y1 + 1];
}

export function isEmpty(m) {
  return !m.data.some((v) => v);
}
