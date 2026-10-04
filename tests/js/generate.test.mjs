import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  assemblePrompt, autoOperation, composeLimit, limitFor, editSection, editPrompt, groupByModel, batchSummary,
} from "../../web/director/core/generate.mjs";

const config = {
  cloud: [{ provider: "OpenAI", models: [
    { id: "gpt-image-2", ref_limit: { generate: 0, compose: 4, edit: 3 } },
    { id: "no-compose", ref_limit: { generate: 0, compose: 0, edit: 0 } },
  ] }],
};

test("assemblePrompt equals every case of assemble_cases.json", () => {
  const url = new URL("../fixtures/director/assemble_cases.json", import.meta.url);
  const { cases } = JSON.parse(readFileSync(url, "utf8"));
  assert.ok(cases.length >= 5);
  for (const c of cases) assert.equal(assemblePrompt(c.sections), c.prompt, c.name);
  assert.equal(assemblePrompt(null), "");
});

test("editSection re-assembles and clears sectionsStale", () => {
  const v = { prompt: "old", sections: { subject: "a fox", style: "photo" }, edited: false, sectionsStale: true };
  const out = editSection(v, "style", "oil painting");
  assert.equal(out.prompt, "a fox. oil painting.");
  assert.equal(out.sectionsStale, false);
  assert.equal(out.edited, true);
  assert.equal(v.prompt, "old");
});

test("editPrompt sets edited and marks the sections stale", () => {
  const out = editPrompt({ prompt: "a", sections: { subject: "a" }, edited: false, sectionsStale: false }, "b");
  assert.equal(out.prompt, "b");
  assert.equal(out.edited, true);
  assert.equal(out.sectionsStale, true);
});

test("autoOperation: no refs generate, one ref compose, compose limit 0 generate", () => {
  assert.equal(autoOperation("gpt-image-2", 0, config), "generate");
  assert.equal(autoOperation("gpt-image-2", 1, config), "compose");
  assert.equal(autoOperation("no-compose", 2, config), "generate");
  assert.equal(autoOperation("unknown", 1, config), "generate");
});

test("groupByModel orders rows by chosen models, tiles by variant, keeps a cancelled tile", () => {
  const entries = [
    { id: "1", batch: "b", model: "m2", variant: 2, outputs: [{ name: "a" }], status: "done" },
    { id: "2", batch: "b", model: "m2", variant: 1, outputs: [{ name: "b" }], status: "done" },
    { id: "3", batch: "b", model: "m1", variant: 1, outputs: [{ name: "c" }], status: "cancelled" },
    { id: "4", batch: "b", model: "m1", variant: 2, outputs: [], status: "error" },
    { id: "5", batch: "other", model: "m1", variant: 1, outputs: [{ name: "d" }], status: "done" },
  ];
  const rows = groupByModel(entries, "b", ["m2", { id: "m1" }, "m3"]);
  assert.deepEqual(rows.map((r) => r.model), ["m2", "m1", "m3"]);
  assert.deepEqual(rows[0].tiles.map((t) => t.id), ["2", "1"]);
  assert.deepEqual(rows[1].tiles.map((t) => t.id), ["3"]);
  assert.deepEqual(rows[2].tiles, []);
});

test("batchSummary reads like the queue row", () => {
  assert.equal(batchSummary({ idea: "a boat", models: ["a", "b", "c"], count: 4 }), "a boat / 3 models · 4 images each");
  assert.equal(batchSummary({ idea: " x ", models: ["a"], count: 1 }), "x / 1 model · 1 image each");
});

test("composeLimit and limitFor agree (one reader of ref_limit)", () => {
  for (const id of ["gpt-image-2", "no-compose", "unknown"]) {
    assert.equal(composeLimit(id, config), limitFor(config, id, "compose"));
  }
  assert.equal(limitFor(config, "gpt-image-2", "inpaint"), 3);
});
