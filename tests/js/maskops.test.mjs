import test from "node:test";
import assert from "node:assert/strict";
import {
  create, stampLine, fillBox, invert, union, bbox, isEmpty, clear,
} from "../../web/director/core/maskops.mjs";

const count = (m) => m.data.reduce((n, v) => n + (v ? 1 : 0), 0);

test("create is empty", () => {
  const m = create(5, 4);
  assert.equal(m.data.length, 20);
  assert.ok(isEmpty(m));
  assert.equal(bbox(m), null);
});

test("stamp radius 0 marks exactly one pixel; erase restores 0", () => {
  const m = create(9, 9);
  stampLine(m, 4, 4, 4, 4, 0, 255);
  assert.equal(count(m), 1);
  assert.equal(m.data[4 * 9 + 4], 255);
  stampLine(m, 4, 4, 4, 4, 0, 0);
  assert.equal(count(m), 0);
});

test("stampLine draws a connected line and clips at the edges", () => {
  const m = create(10, 10);
  stampLine(m, 0, 0, 9, 0, 0, 255);
  assert.equal(count(m), 10);
  stampLine(m, -5, 5, 20, 5, 1, 255);
  assert.deepEqual(bbox(m), [0, 0, 10, 7]);
});

test("stamp radius 2 is a disc", () => {
  const m = create(9, 9);
  stampLine(m, 4, 4, 4, 4, 2, 255);
  assert.equal(m.data[4 * 9 + 6], 255);
  assert.equal(m.data[2 * 9 + 2], 0);
  assert.equal(m.data[4 * 9 + 7], 0);
});

test("fillBox clamps at the edges", () => {
  const m = create(10, 8);
  fillBox(m, [-3, -3, 4, 3], 255);
  assert.deepEqual(bbox(m), [0, 0, 4, 3]);
  fillBox(m, [8, 6, 50, 50], 255);
  assert.deepEqual(bbox(m), [0, 0, 10, 8]);
  fillBox(m, [0, 0, 100, 100], 0);
  assert.ok(isEmpty(m));
});

test("invert twice is the identity", () => {
  const m = create(7, 5);
  stampLine(m, 1, 1, 5, 3, 1, 255);
  const orig = Uint8ClampedArray.from(m.data);
  invert(m);
  assert.equal(count(m), 35 - count({ data: orig }));
  invert(m);
  assert.deepEqual(Array.from(m.data), Array.from(orig));
});

test("union of two disjoint boxes has both bboxes", () => {
  const a = create(20, 20);
  const b = create(20, 20);
  fillBox(a, [1, 1, 4, 4], 255);
  fillBox(b, [10, 12, 15, 18], 255);
  const u = union(a, b);
  assert.deepEqual(bbox(u), [1, 1, 15, 18]);
  assert.deepEqual(bbox(a), [1, 1, 4, 4]);
  assert.equal(count(u), 9 + 30);
});

test("union refuses mismatched sizes", () => {
  assert.throws(() => union(create(2, 2), create(3, 2)));
});

test("clear empties the mask", () => {
  const m = create(6, 6);
  fillBox(m, [0, 0, 6, 6], 255);
  clear(m);
  assert.ok(isEmpty(m));
});
