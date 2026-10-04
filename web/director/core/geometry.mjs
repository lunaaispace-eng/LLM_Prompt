// View transform and coordinate maths. Pure: no DOM. screen = image * zoom + pan.
const MIN_ZOOM = 0.01;
const MAX_ZOOM = 64;
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

export function makeView({ imgW, imgH, viewW, viewH }) {
  const zoom = Math.min(viewW / imgW, viewH / imgH);
  return {
    imgW, imgH, viewW, viewH, zoom,
    panX: (viewW - imgW * zoom) / 2,
    panY: (viewH - imgH * zoom) / 2,
  };
}

// Zoom by `factor` keeping the image point under screen (sx, sy) fixed.
export function zoomAt(view, factor, sx, sy) {
  const zoom = clamp(view.zoom * factor, MIN_ZOOM, MAX_ZOOM);
  const k = zoom / view.zoom;
  return { ...view, zoom, panX: sx - (sx - view.panX) * k, panY: sy - (sy - view.panY) * k };
}

export function pan(view, dx, dy) {
  return { ...view, panX: view.panX + dx, panY: view.panY + dy };
}

export function toImage(view, sx, sy) {
  return [(sx - view.panX) / view.zoom, (sy - view.panY) / view.zoom];
}

export function toScreen(view, x, y) {
  return [x * view.zoom + view.panX, y * view.zoom + view.panY];
}

// Image-space corners (floats) -> native integer box [x, y, w, h], clamped to w x h.
// floor for x0 / y0, ceil for x1 / y1, so a fractional zoom never loses an edge pixel.
export function normBox(x0, y0, x1, y1, w, h) {
  const ax = clamp(Math.floor(Math.min(x0, x1)), 0, w);
  const ay = clamp(Math.floor(Math.min(y0, y1)), 0, h);
  const bx = clamp(Math.ceil(Math.max(x0, x1)), 0, w);
  const by = clamp(Math.ceil(Math.max(y0, y1)), 0, h);
  return [ax, ay, bx - ax, by - ay];
}

// handleDrag = {side, dx, dy} in screen px -> native margins [l, t, r, b], never negative.
export function outpaintMargins(view, { side, dx, dy }) {
  const m = [0, 0, 0, 0];
  const px = { left: -dx, top: -dy, right: dx, bottom: dy }[side];
  const i = { left: 0, top: 1, right: 2, bottom: 3 }[side];
  if (i === undefined) throw new Error(`unknown side: ${side}`);
  m[i] = Math.max(0, Math.round(px / view.zoom));
  return m;
}

// The dragged crop frame's offset as 0..1 fractions of the free slack; no slack -> centred.
export function anchorFromFrame(left, top, slackW, slackH) {
  return {
    x: slackW > 0 ? clamp(left / slackW, 0, 1) : 0.5,
    y: slackH > 0 ? clamp(top / slackH, 0, 1) : 0.5,
  };
}
