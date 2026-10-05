import test from "node:test";
import assert from "node:assert/strict";
import { createStore } from "../../web/director/core/store.mjs";
import { ORIGINAL, defaultComparePair } from "../../web/director/core/history.mjs";
import { SLOT_TABLE, TABS } from "../../web/director/frames/overlay.mjs";
import { resolvePair } from "../../web/director/ui/compare.mjs";
import {
  mountHistory, stripRows, detailFields, showRestart, restartPatch, restartHint,
  editHerePatch, rerunPatch, rerunButton, applyRerun, historyPatch, statusLabel, entryThumb,
  RUN_QUEUE_REASON,
} from "../../web/director/ui/history_panel.mjs";

const chain = [
  { id: "e1", parent: null, ts: "0", inputs: [{ name: "o.png" }], outputs: [{ name: "1.png" }] },
  { id: "e2", parent: "e1", ts: "5", outputs: [{ name: "2.png" }] },
  { id: "e3", parent: "e2", ts: "6", outputs: [{ name: "3.png" }] },
];

test("strip nests Refine and Final under their parent and chains edits after original", () => {
  const rows = stripRows([
    { id: "d", parent: null, tier: "draft", variant: 1, ts: "1", outputs: [{ name: "d.png" }] },
    { id: "r", parent: "d", tier: "draft", variant: 1, ts: "3", outputs: [{ name: "r.png" }] },
    { id: "f", parent: "d", tier: "final", variant: 1, ts: "2", outputs: [{ name: "f.png" }] },
    ...chain,
  ]);
  assert.deepEqual(rows.map((r) => [r.id, r.depth, r.label]), [
    [ORIGINAL, 0, "original"],
    ["e1", 1, "v1"],
    ["e2", 2, "v2"],
    ["e3", 3, "v3"],
    ["d", 0, "draft v1"],
    ["f", 1, "final v1"],
    ["r", 1, "draft v1"],
  ]);
  assert.equal(rows[5].parent, "d");
  assert.equal(rows[6].parent, "d");
});

test("default compare pair is previous vs selected, and null when a side has no picture", () => {
  assert.deepEqual(defaultComparePair(chain, "e3"), ["e2", "e3"]);
  const prev = resolvePair(chain, { selected: "e3", compare: { on: true, a: null, b: null } });
  assert.equal(prev.a.id, "e2");
  assert.equal(prev.b.id, "e3");
  const picked = resolvePair(chain, { selected: "e2", compare: { on: true, a: "e1", b: "e3" } });
  assert.equal(picked.a.id, "e1");
  assert.equal(picked.b.outputs[0].name, "3.png");
  const gen = [{ id: "g", parent: null, batch: "b", outputs: [{ name: "g.png" }] }];
  assert.equal(defaultComparePair(gen, "g"), null);
  assert.equal(resolvePair(gen, { selected: "g", compare: { on: true, a: null, b: null } }), null);
  assert.equal(resolvePair(gen, { selected: "g", compare: { on: true, a: ORIGINAL, b: "g" } }), null);
  assert.equal(resolvePair(chain, { selected: null, compare: { on: true, a: null, b: null } }), null);
});

const cloud = {
  id: "c", status: "done", request: "rust coat", prompt: "Change the sweater",
  engine: "cloud", model: "gpt-image-2",
  writer: {
    provider: "Gemini", model: "gemini-3-flash", preset: "Edit Rewrite",
    thinking: true, negative_on: true,
  },
  negative: "blur",
  params: {
    quality: "high", aspect_ratio: "16:9", resolution: "1K", background: "auto", n: 2,
    mask_mode: "crop", crop_padding: 0.25, feather_px: 8, outpaint: null,
  },
  seed: 482913, mode: "native", est_cost_usd: 0.05, cost_usd: 0.05, seconds: 21.4,
  batch: null, variant: null, tier: null, exact_text: null, sections: null,
};

test("fold-out lists writer and engine settings, and Generate adds its own fields", () => {
  const fields = detailFields(cloud);
  assert.deepEqual(fields.map((f) => f.id), [
    "request", "prompt", "engine", "provider", "writer_model", "preset", "thinking", "negative",
    "quality", "aspect_ratio", "resolution", "background", "count", "mask_mode", "padding",
    "feather", "outpaint", "seed", "mode", "cost", "duration", "status",
  ]);
  const value = (id) => fields.find((f) => f.id === id).value;
  assert.equal(value("engine"), "cloud: gpt-image-2");
  assert.equal(value("provider"), "Gemini");
  assert.equal(value("writer_model"), "gemini-3-flash");
  assert.equal(value("preset"), "Edit Rewrite");
  assert.equal(value("thinking"), "on");
  assert.equal(value("negative"), "on: blur");
  assert.equal(value("count"), "2");
  assert.equal(value("padding"), "0.25");
  assert.equal(value("feather"), "8");
  assert.equal(value("outpaint"), "—");
  assert.equal(value("seed"), "482913");
  assert.equal(value("mode"), "native");
  assert.equal(value("cost"), "est. $0.05 · actual $0.05");
  assert.equal(value("duration"), "21.4 s");
  assert.equal(value("status"), "done");

  const gen = detailFields({
    ...cloud, id: "g", batch: "b1", variant: 2, tier: "final", exact_text: "OPEN",
    sections: { subject: "fox", style: "ink", composition: "centred", lighting: "dusk", camera: "50mm" },
    writer: { ...cloud.writer, feedback: "warmer", variants_mode: "varied" },
  });
  const ids = gen.map((f) => f.id);
  for (const id of ["batch", "variant", "tier", "exact_text", "section:subject", "section:style",
    "section:composition", "section:lighting", "section:camera", "feedback", "prompts_mode"]) {
    assert.ok(ids.includes(id), id);
  }
  assert.equal(gen.find((f) => f.id === "tier").value, "final");
  assert.equal(gen.find((f) => f.id === "variant").value, "2");
  assert.equal(gen.find((f) => f.id === "exact_text").value, "OPEN");
  assert.equal(gen.find((f) => f.id === "section:subject").value, "fox");
  assert.equal(gen.find((f) => f.id === "feedback").value, "warmer");
  assert.equal(gen.find((f) => f.id === "prompts_mode").value, "varied");
  assert.equal(ids.indexOf("section:camera"), ids.indexOf("prompts_mode") - 2);
});

test("Restart from original is offered at chain depth 2 and restores the input picture", () => {
  assert.equal(showRestart(chain, "e1"), false);
  assert.equal(showRestart(chain, "e2"), false);
  assert.equal(showRestart(chain, "e3"), true);
  assert.equal(restartHint(1), "");
  assert.equal(restartHint(2), "2 chained edits — restart from the clean original?");
  assert.equal(restartPatch(chain, "e2"), null);
  const state = {
    engine: { kind: "cloud", params: { outpaint: { left: 4 }, seed: 9 } },
    resize: { state: { all: 1 }, plan: { w: 2 } },
  };
  const patch = restartPatch(chain, "e3", state);
  assert.equal(patch.asset.name, "o.png");
  assert.equal(patch.parent, null);
  assert.equal(patch.mask, null);
  assert.equal(patch.mode, "edit");
  assert.equal(patch.engine.params.outpaint, null);
  assert.equal(patch.engine.params.seed, 9);
  assert.equal(patch.resize.plan, null);
  assert.equal(state.engine.params.outpaint.left, 4);
  const gen = [
    { id: "d", parent: null, batch: "b", tier: "draft" },
    { id: "f", parent: "d", tier: "final" },
    { id: "x", parent: "f", tier: "final" },
  ];
  assert.equal(showRestart(gen, "x"), true);
  assert.equal(restartPatch(gen, "x", state), null);
});

test("re-run patch sets the edited prompt and the entry as parent", () => {
  assert.deepEqual(rerunPatch({ id: "e2", prompt: "old" }, "edited"), { prompt: "edited", parent: "e2" });
  assert.deepEqual(rerunButton(false), { disabled: true, reason: RUN_QUEUE_REASON });
  assert.equal(RUN_QUEUE_REASON, "run queue not loaded");
  const idle = createStore({ prompt: "keep", parent: null });
  assert.equal(idle.has("runCurrent"), false);
  assert.deepEqual(applyRerun(idle, { id: "e2" }, "edited"), { disabled: true, reason: RUN_QUEUE_REASON });
  assert.equal(idle.get().prompt, "keep");
  assert.equal(idle.get().parent, null);
  let ran = 0;
  idle.register("runCurrent", () => { ran += 1; });
  applyRerun(idle, { id: "e2" }, "edited");
  assert.equal(ran, 1);
  assert.equal(idle.get().prompt, "edited");
  assert.equal(idle.get().parent, "e2");
});

test("star, note and hide patch sends only those keys", () => {
  assert.deepEqual(historyPatch({
    star: true, note: "look", hidden: false, prompt: "no", parent: "e1", status: "done",
  }), { star: true, note: "look", hidden: false });
  assert.deepEqual(historyPatch({ star: false }), { star: false });
  assert.deepEqual(historyPatch({ note: "a" }), { note: "a" });
  assert.deepEqual(historyPatch({ hidden: true }), { hidden: true });
  assert.deepEqual(Object.keys(historyPatch({ prompt: "x" })), []);
});

test("a cancelled entry is labelled cancelled, with its cost and kept image", () => {
  const kept = { status: "cancelled", cost_usd: 0.04, outputs: [{ name: "kept.png" }] };
  assert.equal(statusLabel(kept), "cancelled · $0.04");
  assert.deepEqual(entryThumb(kept), { name: "kept.png" });
  assert.equal(detailFields(kept).find((f) => f.id === "status").value, "cancelled · $0.04");
  assert.equal(statusLabel({ status: "cancelled", cost_usd: null, outputs: [] }), "cancelled");
  assert.equal(entryThumb({ status: "cancelled", outputs: [] }), null);
});

test("Edit from here passes the studio state and makes the version the canvas", () => {
  const state = {
    engine: { kind: "cloud", params: { outpaint: { top: 2 }, seed: 3 } },
    resize: { state: {}, plan: { w: 1 } },
  };
  const patch = editHerePatch({ id: "e2", outputs: [{ name: "2.png" }], tier: "final" }, state);
  assert.equal(patch.mode, "edit");
  assert.equal(patch.parent, "e2");
  assert.deepEqual(patch.asset, { name: "2.png" });
  assert.equal(patch.mask, null);
  assert.equal(patch.engine.params.outpaint, null);
  assert.equal(patch.resize.plan, null);
});

test("history mounts from the Edit and Generate slot lists", () => {
  const row = SLOT_TABLE.find((r) => r.slot === "history");
  assert.equal(row.mount, mountHistory);
  assert.equal(row.module, "ui/history_panel.mjs");
  assert.equal(SLOT_TABLE.filter((r) => r.slot === "history").length, 1);
  assert.ok(TABS.find((t) => t.id === "edit").slots.includes("history"));
  assert.ok(TABS.find((t) => t.id === "generate").slots.includes("history"));
});
