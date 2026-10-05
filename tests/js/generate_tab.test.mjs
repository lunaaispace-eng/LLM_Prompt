import test from "node:test";
import assert from "node:assert/strict";
import { createStore } from "../../web/director/core/store.mjs";
import { editPrompt, editSection, groupByModel } from "../../web/director/core/generate.mjs";
import { editFromHere } from "../../web/director/core/history.mjs";
import { generateCanRun, batchBody, generateEstimateLabel, runGenerate, generateTargets } from "../../web/director/ui/generate_engine.mjs";
import { generateWriterPreset, writeGenerate } from "../../web/director/ui/generate_writer.mjs";
import { refineGenerate, mountGenerate } from "../../web/director/ui/generate_tab.mjs";
import { finalMenuEntries, finalEstimateSpec, generateStatus, resultEntries, starResult, variantsModePatch } from "../../web/director/ui/generate_results.mjs";
import { submitFinal, installQueueActions, cancelBatch } from "../../web/director/ui/run_queue.mjs";
import { queueRows } from "../../web/director/ui/queue_panel.mjs";
import { registerEngineSide } from "../../web/director/ui/engine_panel.mjs";
import { SLOT_TABLE } from "../../web/director/frames/overlay.mjs";

const config = { writers: { providers: ["Gemini"] }, presets: [{ title: "Generate A" }], cloud: [{ provider: "Gemini", models: [
  { id: "a", generate_preset: "Generate A", ref_limit: { compose: 2, edit: 1 }, controls: ["resolution"], options: { resolution: ["1K", "2K", "4K"] },
    draft: { quality: "auto", resolution: "1K" }, final_options: ["2K", "4K"] },
  { id: "b", generate_preset: "Generate B", ref_limit: { compose: 1 }, controls: ["quality", "resolution"],
    options: { quality: ["low", "high", "max"], resolution: ["1K", "2K"] }, final_options: ["high", "max"] },
] }] };
const output = { name: "result.png", type: "output", subfolder: "test" };
const original = { id: "entry-a", engine: "cloud", model: "b", prompt: "Original composition", outputs: [output],
  batch: "old", variant: 1, tier: "draft", params: { aspect_ratio: "16:9", resolution: "1K", quality: "low" } };
function setup() {
  const store = createStore({ mode: "generate", project: "test", socketSid: "owned-sid", cloudConfig: config });
  const s = store.get();
  store.set({ writer: { ...s.writer, provider: "Gemini", model: "writer-model" },
    generate: { ...s.generate, models: ["a", "b"], idea: "A lighthouse", exactText: "SEA WATCH 1887", params: { ...s.generate.params, count: 2 },
      variants: [{ prompt: "Painted lighthouse", negative: "blur", sections: { subject: "lighthouse" } }] } });
  const calls = [], server = [];
  const client = {
    async write(body) { calls.push(["write", body]); return { variants: Array.from({ length: body.variants }, (_, i) =>
      ({ positive: `Warm tower ${i + 1}`, negative: "blur", sections: { subject: `tower ${i + 1}` } })) }; },
    async runBatch(sid, project, batch) {
      calls.push(["batch", sid, project, batch]);
      const jobs = batch.models.flatMap((model) => Array.from({ length: batch.count }, (_, i) => ({ job_id: `${model}-${i}`, model, variant: i + 1, est_cost_usd: model === "a" ? 0.1 : null })));
      server.push(...jobs.map((j) => ({ ...j, batch: "new", state: "queued" })));
      return { batch_id: "new", jobs };
    },
    async cloudJobs(sid) { assert.equal(sid, "owned-sid"); return { jobs: server }; },
    async history() { return { day_cost: 1.25 }; },
    async final(sid, project, id, choice) { calls.push(["final", sid, project, id, choice]); server.push({ job_id: "final", state: "running" }); return { job_id: "final", est_cost_usd: 0.4 }; },
    async patchHistory(project, id, patch) { calls.push(["patch", project, id, patch]); },
    async cancelCloud(body) { calls.push(["cancel", body]); },
  };
  return { store, client, calls, server };
}
const patchGenerate = (store, part) => store.set({ generate: { ...store.get().generate, ...part } });

test("Generate mounts in exactly one shell slot; target registration is extensible", () => {
  assert.equal(SLOT_TABLE.filter((row) => row.slot === "generate").length, 1);
  assert.equal(SLOT_TABLE.find((row) => row.slot === "generate").mount, mountGenerate);
  assert.deepEqual(generateTargets(config).map((m) => m.id), ["a", "b"]);
  const off = registerEngineSide("test-local", { label: "Local", mount() {}, canRun() { return true; }, generateTargets() { return [{ id: "local" }]; } });
  try { assert.deepEqual(generateTargets(config).map((m) => m.id), ["a", "b", "local"]); } finally { off(); }
});

test("Run names missing prompt/model, incomplete varied prompts, invalid count and busy reasons", () => {
  const { store } = setup();
  assert.equal(generateCanRun(store.get()).ok, true);
  patchGenerate(store, { variants: [] }); assert.match(generateCanRun(store.get()).reason, /no prompt/);
  patchGenerate(store, { models: [] }); assert.match(generateCanRun(store.get()).reason, /no model/);
  patchGenerate(store, { models: ["a"], variantsMode: "varied", variants: [{ prompt: "One" }] });
  assert.equal(generateCanRun(store.get()).ok, false);
  patchGenerate(store, { variantsMode: "same", params: { ...store.get().generate.params, count: 1.5 } });
  assert.match(generateCanRun(store.get()).reason, /whole number/);
});

test("Estimate sums known amounts and explicitly names unknown models without presenting them as free", () => {
  assert.equal(generateEstimateLabel({ total: 0.62, unknown: ["b"], per_model: [{ model: "b", est_cost_usd: null }] }, 3), "estimate: $0.62 (3 models) + b after run");
  assert.equal(generateEstimateLabel({ total: 0, unknown: ["b"] }, 1), "estimate: after run (1 model) + b after run");
  assert.equal(generateEstimateLabel({ total: 0.3, unknown: [] }, 2), "estimate: $0.30 (2 models)");
});

test("Batch same/varied shapes preserve exact text, sections, all refs and avoid writer size pixels", () => {
  const { store } = setup();
  store.set({ refs: [1, 2, 3].map((i) => ({ role: "style", ref: { name: `ref${i}.png` } })) });
  let body = batchBody(store.get());
  assert.equal(body.prompts.length, 1); assert.equal(body.count, 2); assert.equal(body.variants_mode, "same");
  assert.equal(body.exact_text, "SEA WATCH 1887"); assert.equal(body.request, "A lighthouse"); assert.equal(body.refs.length, 3);
  assert.deepEqual(body.prompts[0].sections, { subject: "lighthouse" }); assert.equal(body.prompts[0].negative, "");
  assert.equal(body.width, undefined); assert.equal(body.size, undefined); assert.equal(body.operation, undefined);
  patchGenerate(store, { variantsMode: "varied", variants: [{ prompt: "First" }, { prompt: "Second" }] });
  body = batchBody(store.get(), ["b"], "parent");
  assert.deepEqual(body.prompts.map((p) => p.prompt), ["First", "Second"]); assert.deepEqual(body.models, ["b"]); assert.equal(body.parent, "parent");
});

test("Same mode shows the first prompt that Run sends, retaining the other variants", () => {
  const { store } = setup();
  patchGenerate(store, { variantsMode: "varied", active: 1, variants: [{ prompt: "First" }, { prompt: "Second" }] });
  store.set({ generate: variantsModePatch(store.get().generate, "same") });
  assert.equal(store.get().generate.active, 0); assert.equal(store.get().generate.variants.length, 2);
  assert.equal(batchBody(store.get()).prompts[0].prompt, store.get().generate.variants[store.get().generate.active].prompt);
});

test("Compact writer uses shared provider/model and lead generate preset with overrides", async () => {
  const { store, client, calls } = setup();
  assert.equal(generateWriterPreset(store.get(), config), "Generate A");
  store.set({ writer: { ...store.get().writer, preset: { edit: "Edit", generate: "Override" }, thinking: true } });
  assert.equal(generateWriterPreset(store.get(), config), "Override");
  assert.equal(await writeGenerate(store, client, config), true);
  const body = calls[0][1]; assert.equal(body.provider, "Gemini"); assert.equal(body.model, "writer-model"); assert.equal(body.preset, "Override");
  assert.equal(body.target_model, "a"); assert.equal(body.exact_text, "SEA WATCH 1887"); assert.equal(body.thinking, true);
});

test("Varied Write asks once for count prompts and edits/stale sections use core functions", async () => {
  const { store, client, calls } = setup(); patchGenerate(store, { variantsMode: "varied" });
  await writeGenerate(store, client, config); assert.equal(calls[0][1].variants, 2);
  assert.notEqual(store.get().generate.variants[0].prompt, store.get().generate.variants[1].prompt);
  const edited = editPrompt(store.get().generate.variants[0], "My words"); assert.equal(edited.sectionsStale, true);
  const section = editSection(edited, "lighting", "golden"); assert.equal(section.sectionsStale, false); assert.match(section.prompt, /golden/);
});

test("Edited variants require confirmation before Write or Refine; decline sends nothing", async () => {
  const { store, client, calls } = setup(); patchGenerate(store, { variants: [{ prompt: "User words", edited: true }] });
  let confirmations = 0;
  assert.equal(await writeGenerate(store, client, config, { confirm: () => { confirmations++; return false; } }), false);
  assert.equal(await refineGenerate(store, client, config, original, "warmer", () => false), false);
  assert.equal(calls.length, 0); assert.equal(confirmations, 1);
  assert.equal(await writeGenerate(store, client, config, { confirm: () => true }), true);
});

test("Run registers one batch row, socket IDs, variant order and queue updates without resubmission", async () => {
  const { store, client, calls, server } = setup();
  await runGenerate(store, client);
  assert.deepEqual(calls[0].slice(0, 3), ["batch", "owned-sid", "test"]);
  assert.equal(queueRows(store.get()).length, 1); assert.equal(queueRows(store.get())[0].jobs.length, 4);
  const entry = { ...original, id: "a-done", model: "a", batch: "new", variant: 2 };
  server[0].state = "done"; server[0].entry = entry;
  await installQueueActions(store, client).message({ type: "luna.job", data: { job_id: "a-0", state: "done", entry } });
  assert.equal(store.get().jobs["a-0"].entry.id, "a-done"); assert.equal(store.get().dayCost, 1.25);
  assert.equal(groupByModel(resultEntries(store.get()), "new", ["a"])[0].tiles[0].variant, 2);
  assert.equal(calls.filter((c) => c[0] === "batch").length, 1);
});

test("Fresh Final run stays one batch row while per-result Final is a separate row", async () => {
  const { store, client } = setup(); patchGenerate(store, { tier: "final" });
  await runGenerate(store, client); assert.equal(queueRows(store.get()).length, 1);
  await submitFinal(store, client, { ...original, batch: "new" }, { quality: "high", resolution: "2K" });
  assert.equal(queueRows(store.get()).length, 2); assert.equal(store.get().jobs.final.tier, "final");
});

test("Early batch socket event is replayed before reconciliation", async () => {
  const { store, client } = setup(), control = installQueueActions(store, client), submit = client.runBatch;
  client.runBatch = async (...args) => {
    await control.message({ type: "luna.job", data: { job_id: "a-0", state: "done", entry: { ...original, id: "early" } } });
    return submit(...args);
  };
  await runGenerate(store, client); assert.equal(store.get().jobs["a-0"].state, "done");
  assert.equal(store.get().jobs["a-0"].entry.id, "early");
});

test("Final entries come only from final_options and estimate one edit at the chosen setting", async () => {
  const { store, client, calls } = setup();
  assert.deepEqual(finalMenuEntries(config, { ...original, model: "a" }, "1K"), [
    { label: "2K", choice: { resolution: "2K" } }, { label: "4K", choice: { resolution: "4K" } }]);
  const choices = finalMenuEntries(config, original, "2K"); assert.deepEqual(choices.map((c) => c.label), ["high", "max"]);
  const spec = finalEstimateSpec(original, choices[0].choice, config);
  assert.equal(spec.operation, "edit"); assert.equal(spec.n, 1); assert.equal(spec.quality, "high"); assert.equal(spec.resolution, "2K");
  await submitFinal(store, client, original, choices[0].choice);
  assert.deepEqual(calls[0], ["final", "owned-sid", "test", "entry-a", { quality: "high", resolution: "2K" }]);
});

test("Refine writes result/feedback/prior prompt then runs result model with parent/current tier/count", async () => {
  const { store, client, calls } = setup(); patchGenerate(store, { tier: "final", variantsMode: "varied" });
  await refineGenerate(store, client, config, original, "warmer light, closer on the tower");
  assert.deepEqual(calls.map((c) => c[0]), ["write", "batch"]);
  const write = calls[0][1], batch = calls[1][3];
  assert.deepEqual(write.result, output); assert.equal(write.prior_prompt, "Original composition");
  assert.equal(write.feedback, "warmer light, closer on the tower"); assert.equal(write.target_model, "b");
  assert.deepEqual(batch.models, ["b"]); assert.equal(batch.parent, original.id); assert.equal(batch.count, 2); assert.equal(batch.tier, "final");
  assert.equal(store.get().generate.variants[0].prompt, "Warm tower 1"); assert.deepEqual(store.get().generate.models, ["a", "b"]);
});

test("Writer and batch failures retain idea, prompt and sections and prevent a run after a failed Refine", async () => {
  const { store, client, calls } = setup(), before = store.get().generate;
  client.write = async () => { throw new Error("quota exceeded"); };
  assert.equal(await refineGenerate(store, client, config, original, "warmer"), false); assert.equal(calls.length, 0);
  assert.equal(store.get().generate.idea, before.idea); assert.deepEqual(store.get().generate.variants, before.variants);
  assert.match(generateStatus(store.get(), config).text, /Gemini: quota exceeded.*Your idea is kept/);
  client.runBatch = async () => { throw new Error("unavailable"); };
  await assert.rejects(runGenerate(store, client), /unavailable/);
  assert.deepEqual(store.get().generate.variants, before.variants); assert.equal(store.get().generate.idea, before.idea);
  assert.equal(store.get().generate.submitting, false); assert.equal(Object.keys(store.get().jobs).length, 0);
});

test("Star persists through patchHistory and updates live entries; Edit from here resets canvas state", async () => {
  const { store, client, calls } = setup();
  store.set({ history: [original], jobs: { job: { id: "job", entry: original } } });
  await starResult(store, client, original); assert.deepEqual(calls[0], ["patch", "test", original.id, { star: true }]);
  assert.equal(resultEntries(store.get())[0].star, true);
  store.set({ engine: { ...store.get().engine, params: { ...store.get().engine.params, outpaint: [1, 2, 3, 4] } },
    resize: { ...store.get().resize, plan: { old: true } } });
  store.set(editFromHere(original, store.get())); assert.deepEqual(store.get().asset, output);
  assert.equal(store.get().mode, "edit"); assert.equal(store.get().parent, original.id);
  assert.equal(store.get().engine.params.outpaint, null); assert.equal(store.get().resize.plan, null);
});

test("Busy state has elapsed time; cancelled sent image remains after batch cancel", async () => {
  const { store, client, server } = setup(); await runGenerate(store, client);
  server[0].state = "running";
  await installQueueActions(store, client).reconnect();
  assert.match(generateStatus(store.get(), config).text, /Generating 4 images · 0:00/);
  assert.deepEqual(await cancelBatch(store, client, "new"), { needsConfirm: true });
  await cancelBatch(store, client, "new", true);
  const entry = { ...original, model: "a", batch: "new", status: "cancelled" };
  await installQueueActions(store, client).message({ type: "luna.job", data: { job_id: "a-0", state: "cancelled", entry } });
  assert.equal(groupByModel(resultEntries(store.get()), "new", ["a"])[0].tiles[0].cancelled, true);
});
