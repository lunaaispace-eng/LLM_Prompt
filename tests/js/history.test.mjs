import test from "node:test";
import assert from "node:assert/strict";
import {
  ORIGINAL, childOf, chainDepth, defaultComparePair, comparePair, editFromHere,
} from "../../web/director/core/history.mjs";

const entries = [
  { id: "e1", parent: null, inputs: [{ name: "orig.png" }], outputs: [{ name: "1.png" }] },
  { id: "e2", parent: "e1", outputs: [{ name: "2.png" }] },
  { id: "e3", parent: "e2", outputs: [{ name: "3.png" }] },
];

test("chainDepth is 2 after two re-runs", () => {
  assert.equal(chainDepth(entries, "e1"), 0);
  assert.equal(chainDepth(entries, "e3"), 2);
  assert.equal(chainDepth(entries, "missing"), 0);
});

test("chainDepth survives a cycle", () => {
  const cyc = [{ id: "a", parent: "b" }, { id: "b", parent: "a" }];
  assert.equal(chainDepth(cyc, "a"), 2);
});

test("childOf sets the parent", () => {
  assert.deepEqual(childOf(entries[1], { prompt: "x" }), { prompt: "x", parent: "e2" });
});

test("defaultComparePair: parent vs selected, else the original", () => {
  assert.deepEqual(defaultComparePair(entries, "e3"), ["e2", "e3"]);
  assert.deepEqual(defaultComparePair(entries, "e1"), [ORIGINAL, "e1"]);
  assert.throws(() => defaultComparePair(entries, "zz"), /unknown history entry/);
});

test("defaultComparePair: a Final picks its draft parent, a Refine child its parent", () => {
  const gen = [
    { id: "d", parent: null, tier: "draft" },
    { id: "f", parent: "d", tier: "final" },
    { id: "r", parent: "d", tier: "draft" },
  ];
  assert.deepEqual(defaultComparePair(gen, "f"), ["d", "f"]);
  assert.deepEqual(defaultComparePair(gen, "r"), ["d", "r"]);
});

test("comparePair accepts any two ids in either order", () => {
  const ab = comparePair(entries, "e1", "e3");
  const ba = comparePair(entries, "e3", "e1");
  assert.equal(ab.a.id, "e1");
  assert.equal(ab.b.id, "e3");
  assert.equal(ba.a.id, "e3");
  const o = comparePair(entries, ORIGINAL, "e2");
  assert.equal(o.a.id, ORIGINAL);
  assert.deepEqual(o.a.outputs[0], { name: "orig.png" });
  assert.throws(() => comparePair(entries, "e1", "zz"), /unknown history entry/);
});

test("editFromHere makes the output the canvas and clears the mask", () => {
  const patch = editFromHere(entries[1]);
  assert.deepEqual(patch.asset, { name: "2.png" });
  assert.equal(patch.mask, null);
  assert.equal(patch.parent, "e2");
  assert.equal(patch.mode, "edit");
  assert.ok(!("request" in patch) && !("prompt" in patch));
});

test("a Generate root has no original: no default pair, and comparePair with ORIGINAL is null", () => {
  const gen = [{ id: "g", parent: null, batch: "b", inputs: [{ name: "ref.png" }], outputs: [{ name: "g.png" }] },
    { id: "g2", parent: "g", batch: "b", outputs: [{ name: "g2.png" }] }];
  assert.equal(defaultComparePair(gen, "g"), null);
  assert.deepEqual(defaultComparePair(gen, "g2"), ["g", "g2"]);
  assert.equal(comparePair(gen, ORIGINAL, "g2"), null);
});

test("editFromHere with state resets outpaint margins and the resize plan", () => {
  const state = { engine: { kind: "cloud", params: { operation: "outpaint", outpaint: { left: 10 }, seed: 5 } },
    resize: { state: { all: 1 }, plan: { x: 1 } } };
  const p = editFromHere(entries[1], state);
  assert.equal(p.engine.params.outpaint, null);
  assert.equal(p.engine.params.seed, 5);
  assert.equal(p.resize.plan, null);
  assert.deepEqual(p.resize.state, { all: 1 });
  assert.equal(state.engine.params.outpaint.left, 10);
});
