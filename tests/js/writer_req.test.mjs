import test from "node:test";
import assert from "node:assert/strict";
import { createStore } from "../../web/director/core/store.mjs";
import {
  refLimit, sentRefs, buildWriteBody, buildRefineBody, mergeWriterResult, mergeWriterError,
  useAsEditPrompt, mergeGenerateResult,
} from "../../web/director/core/writer_req.mjs";

const config = {
  cloud: [{ provider: "OpenAI", models: [
    { id: "gpt-image-2", ref_limit: { generate: 0, compose: 4, edit: 3, inpaint: 3, outpaint: 3 } },
    { id: "nano-banana", ref_limit: { generate: 0, compose: 2, edit: 1 } },
    { id: "nano", ref_limit: { generate: 0, compose: 0, edit: 0, inpaint: 0 } },
  ] }],
};
const refs = (n) => Array.from({ length: n }, (_, i) => ({ ref: { name: `r${i}.png` }, role: `role${i}` }));
const edit = (over = {}) => {
  const base = createStore({}).get();
  return { ...base, asset: { name: "a.png" }, mask: { name: "m.png" }, request: "red door", prompt: "mine",
    engine: { ...base.engine, model: "gpt-image-2", params: { ...base.engine.params, operation: "inpaint" } },
    ...over };
};

test("refLimit: generate 0, compose max_inputs, inpaint max_inputs - 1 (server-computed)", () => {
  const s = (operation) => edit({ engine: { ...edit().engine, params: { ...edit().engine.params, operation } } });
  assert.equal(refLimit(s("generate"), config), 0);
  assert.equal(refLimit(s("compose"), config), 4);
  assert.equal(refLimit(s("inpaint"), config), 3);
  assert.equal(refLimit(edit({ engine: { ...edit().engine, model: "ghost" } }), config), 0);
});

test("sentRefs keeps order and cuts at the limit", () => {
  const st = edit({ refs: refs(5) });
  assert.deepEqual(sentRefs(st, config).map((r) => r.role), ["role0", "role1", "role2"]);
  assert.equal(st.refs.length, 5);
});

const WRITE_KEYS = ["canvas", "mask", "refs", "provider", "model", "preset", "target_model", "operation", "request",
  "send", "thinking", "negative", "mask_mode", "crop_padding", "vision_mp", "server_url", "gguf",
  "size", "variants", "sections", "exact_text", "feedback", "prior_prompt", "result"].sort();

test("buildWriteBody has exactly the A6 keys, carries switches and drops refs past the limit", () => {
  const st = edit({ refs: refs(5), writer: { ...edit().writer, provider: "Custom", model: "m", thinking: true,
    negativeOn: true, serverUrl: "http://x/v1", preset: { edit: "Edit Rewrite - GPT Image", generate: null } } });
  const b = buildWriteBody(st, config);
  assert.deepEqual(Object.keys(b).sort(), WRITE_KEYS);
  assert.equal(b.preset, "Edit Rewrite - GPT Image");
  assert.equal(b.thinking, true);
  assert.equal(b.negative, true);
  assert.equal(b.server_url, "http://x/v1");
  assert.equal(b.target_model, "gpt-image-2");
  assert.equal(b.operation, "inpaint");
  assert.equal(b.request, "red door");
  assert.deepEqual(b.canvas, { name: "a.png" });
  assert.deepEqual(b.mask, { name: "m.png" });
  assert.deepEqual(b.refs, [
    { role: "role0", ref: { name: "r0.png" } }, { role: "role1", ref: { name: "r1.png" } },
    { role: "role2", ref: { name: "r2.png" } }]);
  assert.equal(b.variants, 1);
  assert.equal(b.sections, false);
  assert.equal(b.exact_text, "");
  assert.equal(b.result, null);
  assert.equal(b.size, null);
});

test("mergeWriterResult fills only writer positive/negative", () => {
  const st = edit({ writer: { ...edit().writer, negativeOn: true, busy: true } });
  const patch = mergeWriterResult(st, { positive: "P", negative: "N" });
  assert.deepEqual(Object.keys(patch), ["writer"]);
  assert.equal(patch.writer.positive, "P");
  assert.equal(patch.writer.negative, "N");
  assert.equal(patch.writer.busy, false);
  const off = mergeWriterResult(edit(), { positive: "P", negative: "N" });
  assert.equal(off.writer.negative, "");
});

test("mergeWriterError leaves request and prompt and sets writer.error", () => {
  const patch = mergeWriterError(edit(), { code: "busy", message: "GGUF busy" });
  assert.deepEqual(Object.keys(patch), ["writer"]);
  assert.deepEqual(patch.writer.error, { code: "busy", message: "GGUF busy" });
  assert.equal(patch.writer.busy, false);
  assert.deepEqual(mergeWriterError(edit(), new Error("boom")).writer.error, { code: "error", message: "boom" });
});

test("useAsEditPrompt copies both; negativeOn false leaves negative empty", () => {
  const w = { ...edit().writer, positive: "P", negative: "N" };
  assert.deepEqual(useAsEditPrompt(edit({ writer: { ...w, negativeOn: true } })), { prompt: "P", negative: "N" });
  assert.deepEqual(useAsEditPrompt(edit({ writer: { ...w, negativeOn: false } })), { prompt: "P", negative: "" });
});

// ---- Generate mode (S10)
const gen = (over = {}) => {
  const base = edit();
  return { ...base, mode: "generate", mask: { name: "keep.png" }, request: "", refs: refs(6),
    generate: { ...base.generate, idea: "a lighthouse", exactText: "SEA WATCH 1887", variantsMode: "varied",
      models: ["gpt-image-2", "nano"], params: { ...base.generate.params, aspect_ratio: "16:9", resolution: "2K", count: 3 } },
    ...over };
};

test("Generate mode refLimit follows the lead model and the automatic operation", () => {
  assert.equal(refLimit(gen(), config), 4);
  const lead = gen();
  lead.generate = { ...lead.generate, models: ["nano", "gpt-image-2"] };
  assert.equal(refLimit(lead, config), 0);
  assert.equal(sentRefs(lead, config).length, 0);
});

test("Generate buildWriteBody: A6 keys, variants 3 when varied else 1, canvas and mask null", () => {
  const b = buildWriteBody(gen(), config);
  assert.deepEqual(Object.keys(b).sort(), WRITE_KEYS);
  assert.equal(b.variants, 3);
  assert.equal(b.canvas, null);
  assert.equal(b.mask, null);
  assert.equal(b.sections, true);
  assert.equal(b.exact_text, "SEA WATCH 1887");
  assert.equal(b.target_model, "gpt-image-2");
  assert.equal(b.operation, "compose");
  assert.equal(b.request, "a lighthouse");
  assert.equal(b.refs.length, 4);
  assert.deepEqual(b.size, [2048, 1152]);
  const same = gen();
  same.generate = { ...same.generate, variantsMode: "same" };
  assert.equal(buildWriteBody(same, config).variants, 1);
  const none = gen({ refs: [] });
  assert.equal(buildWriteBody(none, config).operation, "generate");
});

test("buildRefineBody carries result, prior_prompt and feedback; request unchanged", () => {
  const entry = { id: "e", outputs: [{ name: "out.png" }], prompt: "old prompt" };
  const st = gen();
  const b = buildRefineBody(st, config, entry, "more fog");
  assert.deepEqual(b.result, { name: "out.png" });
  assert.equal(b.prior_prompt, "old prompt");
  assert.equal(b.feedback, "more fog");
  assert.equal(b.request, "a lighthouse");
  assert.deepEqual(Object.keys(b).sort(), WRITE_KEYS);
});

const reply = { variants: [
  { positive: "p1", negative: "n1", sections: { subject: "s1" } },
  { positive: "p2", negative: "n2", sections: { subject: "s2" } },
  { positive: "p3", negative: "", sections: null },
] };

test("mergeGenerateResult fills three variants", () => {
  const st = gen({ writer: { ...gen().writer, negativeOn: true, busy: true } });
  const patch = mergeGenerateResult(st, reply);
  assert.deepEqual(Object.keys(patch).sort(), ["generate", "writer"]);
  const v = patch.generate.variants;
  assert.equal(v.length, 3);
  assert.deepEqual(v[1], { prompt: "p2", negative: "n2", sections: { subject: "s2" }, edited: false, sectionsStale: false });
  assert.equal(v[2].sections, null);
  assert.equal(patch.writer.busy, false);
  assert.equal(patch.generate.idea, "a lighthouse");
});

test("mergeGenerateResult asks first when a variant was edited, and then changes nothing", () => {
  const st = gen();
  st.generate = { ...st.generate, variants: [
    { prompt: "x", negative: "", sections: null, edited: false, sectionsStale: false },
    { prompt: "mine", negative: "", sections: null, edited: true, sectionsStale: false }] };
  assert.deepEqual(mergeGenerateResult(st, reply), { needsConfirm: true });
  assert.equal(st.generate.variants[1].prompt, "mine");
  const forced = mergeGenerateResult(st, reply, { force: true });
  assert.equal(forced.generate.variants.length, 3);
});

test("negative off drops every variant negative", () => {
  const patch = mergeGenerateResult(gen(), reply);
  assert.ok(patch.generate.variants.every((v) => v.negative === ""));
});

test("Refine writes for the result's model, not the lead", () => {
  const st = gen();
  const entry = { id: "e", model: "nano-banana", outputs: [{ name: "o.png" }], prompt: "old" };
  const b = buildRefineBody(st, config, entry, "f");
  assert.equal(b.target_model, "nano-banana");
  assert.equal(b.refs.length, 2);
  assert.equal(b.operation, "compose");
  assert.equal(buildRefineBody(st, config, { ...entry, model: "nano" }, "f").operation, "generate");
  assert.equal(buildRefineBody(st, config, { ...entry, model: "nano" }, "f").refs.length, 0);
});

test("a writer preset picked on one tab is not sent from the other", () => {
  const st = gen({ writer: { ...gen().writer, preset: { edit: "E", generate: "G" } } });
  assert.equal(buildWriteBody(st, config).preset, "G");
  assert.equal(buildWriteBody({ ...st, mode: "edit" }, config).preset, "E");
  const auto = gen({ writer: { ...gen().writer, preset: { edit: "E", generate: null } } });
  assert.equal(buildWriteBody(auto, config).preset, null);
});

test("the nominal write size never becomes width/height", () => {
  const b = buildWriteBody(gen(), config);
  assert.ok(!("width" in b) && !("height" in b));
  assert.deepEqual(b.size, [2048, 1152]);
  const st = gen();
  assert.ok(!("width" in st.generate.params) && !("height" in st.generate.params));
});
