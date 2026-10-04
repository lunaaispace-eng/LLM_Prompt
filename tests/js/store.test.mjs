import test from "node:test";
import assert from "node:assert/strict";
import { createStore, defaultState } from "../../web/director/core/store.mjs";

test("store notifies once per set and unsubscribe stops it", () => {
  const s = createStore({});
  let n = 0;
  const off = s.subscribe(() => { n += 1; });
  s.set({ request: "a", prompt: "b", tool: "box" });
  assert.equal(n, 1);
  off();
  s.set({ request: "c" });
  assert.equal(n, 1);
});

test("changing engine.kind keeps mask, request and prompt", () => {
  const s = createStore({ mask: { name: "m.png" }, request: "red door", prompt: "paint it red" });
  s.act("setEngineKind", "workflow");
  const st = s.get();
  assert.equal(st.engine.kind, "workflow");
  assert.deepEqual(st.mask, { name: "m.png" });
  assert.equal(st.request, "red door");
  assert.equal(st.prompt, "paint it red");
  assert.equal(st.engine.package, null);
});

test("a tab switch keeps generate and the Edit tab's mask, prompt and engine.params", () => {
  const s = createStore({ mask: { name: "m.png" }, prompt: "p" });
  s.set({ generate: { ...s.get().generate, idea: "a boat", models: ["gpt-image-2"] } });
  const params = s.get().engine.params;
  s.act("setMode", "generate");
  s.act("setMode", "edit");
  const st = s.get();
  assert.equal(st.generate.idea, "a boat");
  assert.deepEqual(st.generate.models, ["gpt-image-2"]);
  assert.deepEqual(st.mask, { name: "m.png" });
  assert.equal(st.prompt, "p");
  assert.equal(st.engine.params, params);
});

test("registered actions run with the store; unknown ones throw", () => {
  const s = createStore({});
  s.register("useInGraph", (store, id) => store.set({ selected: id }));
  s.act("useInGraph", "e1");
  assert.equal(s.get().selected, "e1");
  assert.throws(() => s.act("nope"), /unknown store action/);
});

test("default state has the documented keys", () => {
  const d = defaultState();
  for (const k of ["project", "mode", "asset", "refs", "mask", "tool", "brush", "engine", "writer", "request",
    "prompt", "negative", "jobs", "history", "selected", "dayCost", "compare", "resize", "generate"]) {
    assert.ok(k in d, k);
  }
  assert.equal(d.engine.kind, "cloud");
  assert.equal(d.engine.package, null);
  assert.equal(d.engine.version, null);
  for (const k of ["operation", "quality", "aspect_ratio", "resolution", "background", "n", "mask_mode",
    "crop_padding", "feather_px", "outpaint", "seed"]) assert.ok(k in d.engine.params, k);
});
