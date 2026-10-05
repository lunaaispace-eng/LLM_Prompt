import test from "node:test";
import assert from "node:assert/strict";
import { createStore, defaultState } from "../../web/director/core/store.mjs";
import { refLimit } from "../../web/director/core/writer_req.mjs";
import { SLOT_TABLE } from "../../web/director/frames/overlay.mjs";
import {
  controlsFor, optionsFor, selectCloudModel, seedLabel, cloudCanRun, cloudSpec,
  estimateLabel, submitCloud, registerEngineSide, engineSides, mountEngine,
} from "../../web/director/ui/engine_panel.mjs";

const operations = { generate: 0, edit: 3, compose: 4, inpaint: 3, outpaint: 3 };
const config = { cloud: [
  { provider: "first-provider", models: [{
    id: "first", ref_limit: operations,
    controls: ["aspect_ratio", "resolution", "quality", "background", "seed"],
    options: { aspect_ratio: ["auto", "1:1", "3:2"], resolution: ["1K", "4K"],
      quality: ["auto", "high"], background: ["auto", "transparent"], seed: { sent: false } },
    draft: { quality: "auto", resolution: "1K" },
  }] },
  { provider: "new-provider", models: [{
    id: "new-model", ref_limit: { ...operations, edit: 1, inpaint: 1 },
    controls: ["aspect_ratio", "resolution", "seed"],
    options: { aspect_ratio: ["1:1", "16:9"], resolution: ["2K", "4K"], mask_mode: ["auto", "crop"], seed: { sent: false } },
    draft: { resolution: "2K" },
  }] },
] };

function state(operation = "inpaint") {
  const s = defaultState();
  return { ...s, project: "test", asset: { name: "canvas.png", type: "input", subfolder: "" },
    mask: { w: 2, h: 1, data: new Uint8ClampedArray([0, 255]) }, prompt: "Keep my exact prompt",
    engine: { ...s.engine, model: "first", params: { ...s.engine.params, operation, seed: 482913 } },
    refs: Array.from({ length: 5 }, (_, i) => ({ role: "style", ref: { name: `ref${i}.png`, type: "input" } })),
  };
}

test("model controls and options come from config, including an unfamiliar provider/model", () => {
  const controls = controlsFor(config, "first", "inpaint");
  assert.ok(controls.includes("quality") && controls.includes("background"));
  assert.ok(controls.includes("mask_mode") && controls.includes("feather_px") && controls.includes("n"));
  const next = controlsFor(config, "new-model", "edit");
  assert.deepEqual(next, ["operation", "n", "aspect_ratio", "resolution", "seed"]);
  assert.deepEqual(optionsFor(config.cloud[1].models[0], "aspect_ratio"), ["1:1", "16:9"]);
  assert.deepEqual(optionsFor(config.cloud[0].models[0], "operation"), Object.keys(operations));
  assert.deepEqual(controlsFor(config, "missing", "inpaint"), []);
});

test("region controls follow the operation, outpaint has L/T/R/B and no ignored feather", () => {
  const out = controlsFor(config, "first", "outpaint");
  assert.ok(out.includes("outpaint") && out.includes("mask_mode") && out.includes("crop_padding"));
  assert.ok(!out.includes("feather_px"));
  const whole = controlsFor(config, "first", "generate");
  assert.ok(!whole.includes("mask_mode") && !whole.includes("outpaint"));
});

test("switch falls back for unsupported values, preserves mask, prompt, seed and unrelated state", () => {
  const before = state();
  Object.assign(before.engine.params, { quality: "high", background: "transparent", resolution: "1K",
    aspect_ratio: "3:2", mask_mode: "native" });
  const next = { ...before, ...selectCloudModel(before, config, "new-model") };
  assert.deepEqual(next.engine.params, { ...before.engine.params, quality: "auto", background: "auto",
    resolution: "2K", aspect_ratio: "1:1", mask_mode: "auto" });
  assert.equal(next.mask, before.mask);
  assert.equal(next.prompt, before.prompt);
  assert.equal(next.refs, before.refs);
  assert.equal(next.writer, before.writer);
  assert.equal(next.engine.params.seed, 482913);
  assert.equal(before.engine.params.resolution, "1K");
  assert.equal(refLimit(before, config), 3);
  assert.equal(refLimit(next, config), 1);
});

test("switch keeps supported values and missing models produce no patch", () => {
  const before = state("edit");
  Object.assign(before.engine.params, { resolution: "4K", aspect_ratio: "1:1", mask_mode: "crop" });
  const patch = selectCloudModel(before, config, "new-model");
  assert.equal(patch.engine.params.resolution, "4K");
  assert.equal(patch.engine.params.mask_mode, "crop");
  assert.deepEqual(selectCloudModel(before, config, "missing"), {});
});

test("cloud canRun uses all three S4 named reasons and treats whitespace prompt as empty", () => {
  const s = state();
  assert.deepEqual(cloudCanRun({ ...s, asset: null }), { ok: false, reason: "Run disabled: no image" });
  for (const mask of [null, { w: 2, h: 1, data: new Uint8ClampedArray(2) }, { name: "mask.png", empty: true }]) {
    assert.deepEqual(cloudCanRun({ ...s, mask }), { ok: false, reason: "Run disabled: the mask is empty" });
  }
  assert.deepEqual(cloudCanRun({ ...s, prompt: " \n " }), { ok: false, reason: "Run disabled: no prompt" });
  assert.deepEqual(cloudCanRun(s), { ok: true, reason: "" });
  assert.equal(cloudCanRun({ ...state("edit"), mask: null }).ok, true);
  assert.equal(cloudCanRun({ ...s, mask: { name: "mask.png" } }).ok, true);
});

test("seed remains visible even without config control; sent:false labels it recorded, not sent", () => {
  assert.equal(seedLabel(config.cloud[0].models[0]), "Seed · recorded, not sent");
  assert.equal(seedLabel({ options: { seed: { sent: true } } }), "Seed");
  assert.equal(seedLabel(null), "Seed");
  assert.ok(controlsFor({ cloud: [{ models: [{ id: "future", controls: [] }] }] }, "future", "edit").includes("seed"));
});

test("Cloud is the only registered side; another side registers and unregisters without layout changes", () => {
  assert.deepEqual(engineSides().map(({ kind, label }) => [kind, label]), [["cloud", "Cloud"]]);
  const side = { label: "Another engine", mount: () => ({ destroy() {} }), canRun: () => false };
  const remove = registerEngineSide("test-side", side);
  assert.equal(engineSides()[1].mount, side.mount);
  assert.equal(engineSides()[1].canRun(state()), false);
  remove();
  assert.deepEqual(engineSides().map((s) => s.label), ["Cloud"]);
  assert.throws(() => registerEngineSide("bad", { label: "bad" }), TypeError);
});

test("overlay mounts engine in the existing slot", () => {
  const rows = SLOT_TABLE.filter((r) => r.slot === "engine");
  assert.equal(rows.length, 1);
  assert.equal(rows[0].mount, mountEngine);
});

test("spec uses file refs, trims sent refs by the shared limit, keeps recorded seed and writer metadata", () => {
  const s = { ...state(), ...selectCloudModel(state(), config, "new-model") };
  const mask = { name: "mask.png", type: "input" };
  const spec = cloudSpec(s, config, mask);
  assert.equal(spec.image, s.asset);
  assert.equal(spec.mask, mask);
  assert.deepEqual(spec.refs, [s.refs[0].ref]);
  assert.equal(spec.seed, 482913);
  assert.equal(spec.prompt, s.prompt);
  assert.equal(spec.writer.negative_on, false);
  assert.equal(spec.outpaint, null);
  const out = state("outpaint"); out.engine.params.outpaint = [1, 2, 3, 4];
  assert.deepEqual(cloudSpec(out, config).outpaint, [1, 2, 3, 4]);
  assert.equal(cloudSpec(out, config).mask, null);
  assert.equal(cloudSpec(state("generate"), config).image, null);
  assert.deepEqual(cloudSpec(state("generate"), config).refs, []);
});

test("estimate formats known cost, xAI/unknown cost, and unavailable replies", () => {
  assert.equal(estimateLabel({ total: 0.045, unknown: [], per_model: [{ est_cost_usd: 0.045 }] }), "estimate: $0.04");
  assert.equal(estimateLabel({ total: 0, unknown: ["unknown-model"] }), "estimate: after run");
  assert.equal(estimateLabel({ total: 0, per_model: [{ est_cost_usd: null }] }), "estimate: after run");
  assert.equal(estimateLabel(null), "estimate: unavailable");
});

test("submission uploads a snapshot of pixel mask, sends spec and records the returned cloud job", async () => {
  const store = createStore(state()), calls = [];
  const before = store.get();
  const mask = { name: "uploaded-mask.png", type: "input" };
  const client = {
    async importAsset(blob, project) { calls.push([blob, project]); return { ref: mask }; },
    async runCloud(body) { calls.push(body); return { job_id: "job-1", est_cost_usd: 0.05 }; },
  };
  await submitCloud(store, client, config, { sid: "test-sid", maskBlob: async (snapshot) => {
    assert.notEqual(snapshot.data, before.mask.data);
    assert.deepEqual(snapshot.data, before.mask.data);
    return "mask-blob";
  } });
  assert.deepEqual(calls[0], ["mask-blob", "test"]);
  assert.deepEqual(calls[1], { sid: "test-sid", project: "test", spec: cloudSpec(before, config, mask) });
  assert.equal(store.get().jobs["job-1"].state, "queued");
  assert.equal(store.get().jobs["job-1"].serverId, "job-1");
  assert.equal(store.get().mask, before.mask);
  assert.equal(store.get().prompt, before.prompt);
});

test("disabled submission never calls the API; a failed run preserves all inputs", async () => {
  const store = createStore({ ...state(), asset: null });
  const disabled = await submitCloud(store, { runCloud() { assert.fail("disabled run sent"); } }, config);
  assert.equal(disabled.reason, "Run disabled: no image");
  const live = createStore(state("edit")), before = live.get();
  await assert.rejects(submitCloud(live, { async runCloud() { throw new Error("refused"); } }, config), /refused/);
  assert.equal(live.get(), before);
});
