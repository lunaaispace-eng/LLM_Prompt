import test from "node:test";
import assert from "node:assert/strict";
import {
  makeView, zoomAt, pan, toImage, toScreen, normBox, outpaintMargins, anchorFromFrame,
} from "../../web/director/core/geometry.mjs";

const near = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);
const mk = () => makeView({ imgW: 2000, imgH: 1000, viewW: 800, viewH: 600 });

test("makeView fits and centres", () => {
  const v = mk();
  near(v.zoom, 0.4);
  const [sx, sy] = toScreen(v, 1000, 500);
  near(sx, 400);
  near(sy, 300);
});

test("toImage(toScreen(p)) is p", () => {
  const v = pan(zoomAt(mk(), 1.7, 120, 90), 33, -21);
  const [sx, sy] = toScreen(v, 321.5, 77.25);
  const [x, y] = toImage(v, sx, sy);
  near(x, 321.5);
  near(y, 77.25);
});

test("zoomAt keeps the point under the cursor fixed", () => {
  const v = mk();
  const before = toImage(v, 250, 175);
  const v2 = zoomAt(v, 3, 250, 175);
  const after = toImage(v2, 250, 175);
  near(before[0], after[0]);
  near(before[1], after[1]);
  near(v2.zoom, v.zoom * 3);
});

test("zoomAt does not mutate and clamps zoom", () => {
  const v = mk();
  const z = v.zoom;
  zoomAt(v, 2, 0, 0);
  assert.equal(v.zoom, z);
  assert.ok(zoomAt(v, 1e9, 0, 0).zoom <= 64);
  assert.ok(zoomAt(v, 1e-9, 0, 0).zoom >= 0.01);
});

test("Review Focus 1: zoom 0.33, pan (-120, 40) -> exact native box", () => {
  const v = { imgW: 1000, imgH: 800, viewW: 500, viewH: 400, zoom: 0.33, panX: -120, panY: 40 };
  // screen box (10, 100)-(200, 250)
  const [ax, ay] = toImage(v, 10, 100);
  const [bx, by] = toImage(v, 200, 250);
  assert.deepEqual(normBox(ax, ay, bx, by, 1000, 800), [393, 181, 577, 456]);
});

test("normBox floors x0/y0, ceils x1/y1, clamps, orders", () => {
  assert.deepEqual(normBox(10.2, 5.9, 20.1, 9.1, 100, 100), [10, 5, 11, 5]);
  assert.deepEqual(normBox(-5, -5, 500, 500, 100, 60), [0, 0, 100, 60]);
  assert.deepEqual(normBox(20, 9, 10, 5, 100, 100), [10, 5, 10, 4]);
});

test("outpaintMargins: right drag 100 screen px at zoom 0.5 -> 200", () => {
  const v = { imgW: 1000, imgH: 1000, viewW: 500, viewH: 500, zoom: 0.5, panX: 0, panY: 0 };
  assert.deepEqual(outpaintMargins(v, { side: "right", dx: 100, dy: 0 }), [0, 0, 200, 0]);
  assert.deepEqual(outpaintMargins(v, { side: "left", dx: -50, dy: 0 }), [100, 0, 0, 0]);
  assert.deepEqual(outpaintMargins(v, { side: "top", dx: 0, dy: -25 }), [0, 50, 0, 0]);
  assert.deepEqual(outpaintMargins(v, { side: "bottom", dx: 0, dy: 10 }), [0, 0, 0, 20]);
  // dragging inward never gives a negative margin
  assert.deepEqual(outpaintMargins(v, { side: "right", dx: -100, dy: 0 }), [0, 0, 0, 0]);
});

test("anchorFromFrame at both edges and zero slack", () => {
  assert.deepEqual(anchorFromFrame(0, 0, 100, 50), { x: 0, y: 0 });
  assert.deepEqual(anchorFromFrame(100, 50, 100, 50), { x: 1, y: 1 });
  assert.deepEqual(anchorFromFrame(25, 10, 100, 40), { x: 0.25, y: 0.25 });
  assert.deepEqual(anchorFromFrame(7, 3, 0, 0), { x: 0.5, y: 0.5 });
  assert.deepEqual(anchorFromFrame(-9, 999, 100, 50), { x: 0, y: 1 });
});
