import test from "node:test";
import assert from "node:assert/strict";
import { createUndo } from "../../web/director/core/undo.mjs";

test("push, undo, redo walk the history", () => {
  const u = createUndo();
  assert.equal(u.canUndo, false);
  u.push("a");
  u.push("b");
  u.push("c");
  assert.equal(u.canUndo, true);
  assert.equal(u.undo(), "b");
  assert.equal(u.undo(), "a");
  assert.equal(u.canUndo, false);
  assert.equal(u.undo(), undefined);
  assert.equal(u.canRedo, true);
  assert.equal(u.redo(), "b");
  assert.equal(u.redo(), "c");
  assert.equal(u.canRedo, false);
  assert.equal(u.redo(), undefined);
});

test("the limit drops the oldest entry", () => {
  const u = createUndo(3);
  for (const s of ["a", "b", "c", "d"]) u.push(s);
  assert.equal(u.undo(), "c");
  assert.equal(u.undo(), "b");
  assert.equal(u.undo(), undefined);
});

test("a push clears redo", () => {
  const u = createUndo();
  u.push("a");
  u.push("b");
  u.undo();
  assert.equal(u.canRedo, true);
  u.push("x");
  assert.equal(u.canRedo, false);
  assert.equal(u.undo(), "a");
});
