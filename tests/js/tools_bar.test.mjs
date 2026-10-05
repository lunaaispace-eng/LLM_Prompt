import test from "node:test";
import assert from "node:assert/strict";
import { buildToolRegistry, commandForKey, toolIdForKey } from "../../web/director/ui/tools_bar.mjs";

test("B, R, E, I, O select tools; X and S select nothing", () => {
  const keys = { B: "brush", R: "box", E: "eraser", I: "invert", O: "outpaint", b: "brush", r: "box" };
  for (const [key, id] of Object.entries(keys)) assert.equal(toolIdForKey(key), id, key);
  assert.equal(toolIdForKey("X"), null);
  assert.equal(toolIdForKey("x"), null);
  assert.equal(toolIdForKey("S"), null);
  assert.equal(toolIdForKey("s"), null);
  assert.equal(toolIdForKey("Box"), null);
  const registry = buildToolRegistry();
  assert.equal(registry.some((tool) => tool.key === "s" || tool.key === "x"), false);
  assert.deepEqual(registry.map((tool) => tool.key), ["b", "r", "e", "i", "o"]);
});

test("registry entries are {id, key, icon, onPointer} and paint routes the value", () => {
  const calls = [];
  const registry = buildToolRegistry({
    paint: (info, value) => calls.push([info.phase, value]),
    box: () => calls.push(["box"]),
    invert: () => calls.push(["invert"]),
    outpaint: () => calls.push(["outpaint"]),
  });
  for (const tool of registry) {
    assert.equal(typeof tool.id, "string");
    assert.equal(typeof tool.key, "string");
    assert.equal(typeof tool.icon, "string");
    assert.equal("onPointer" in tool, true);
    assert.equal(typeof tool.onPointer, "function");
  }
  registry.find((tool) => tool.id === "brush").onPointer({ phase: "down" });
  registry.find((tool) => tool.id === "eraser").onPointer({ phase: "move" });
  assert.deepEqual(calls, [["down", 255], ["move", 0]]);
  assert.equal(commandForKey({ key: "R" }).tool, "box");
  assert.equal(commandForKey({ key: "X" }), null);
  assert.equal(commandForKey({ key: "S" }), null);
  assert.deepEqual(commandForKey({ key: "z", ctrlKey: true }), { command: "undo" });
  assert.deepEqual(commandForKey({ key: "y", ctrlKey: true }), { command: "redo" });
  assert.deepEqual(commandForKey({ key: "z", ctrlKey: true, shiftKey: true }), { command: "redo" });
  assert.equal(commandForKey({ key: "b", shiftKey: true }), null);
});
