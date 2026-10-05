// Mount every studio panel in a DOM shim. A missing import throws while a panel renders.
import "./_dom_shim.mjs";
import test from "node:test";
import assert from "node:assert/strict";
import { createStore, defaultState } from "../../web/director/core/store.mjs";
import { SLOT_TABLE, mountOverlay } from "../../web/director/frames/overlay.mjs";

const inputRef = { name: "source.png", subfolder: "", type: "input" };
const output = (name) => ({ name, subfolder: "", type: "output" });

function model(id) {
  return {
    id,
    ref_limit: { generate: id === "grok-imagine-image" ? 4 : 0, edit: 3, compose: 4, inpaint: 3, outpaint: 1 },
    controls: ["aspect_ratio", "resolution", "quality", "background", "seed"],
    options: {
      aspect_ratio: ["auto", "1:1", "16:9"],
      resolution: ["1K", "2K"],
      quality: ["auto", "high"],
      background: ["auto", "opaque", "transparent"],
      seed: { sent: false },
    },
    default_preset: "Edit Rewrite",
    generate_preset: "Generate",
    ratio_presets: ["", "1:1", "16:9"],
    draft: { quality: "low", resolution: "1K" },
    final_options: ["1K", "2K"],
  };
}

export const directorConfig = {
  writers: { "Local GGUF": ["writer.gguf"], providers: ["Gemini", "OpenAI"] },
  presets: [
    { title: "Edit Rewrite", edit_rewrite: true, generate: false },
    { title: "Generate", edit_rewrite: false, generate: true },
  ],
  engines: ["cloud"],
  settings: {},
  cloud: [
    { provider: "openai", models: [model("gpt-image-2")] },
    { provider: "xai", models: [model("grok-imagine-image")] },
  ],
};

export const directorHistory = {
  day_cost: 0.09,
  entries: [
    {
      id: "done-1", parent: null, ts: "1", status: "done", request: "warmer light", prompt: "Warm the light",
      engine: "cloud", model: "gpt-image-2", inputs: [inputRef], outputs: [output("done.png")],
      writer: { provider: "Local GGUF", model: "writer.gguf", preset: null, thinking: false },
      params: { quality: "auto", aspect_ratio: "1:1", resolution: "1K" },
      cost_usd: 0.04, est_cost_usd: 0.04, seconds: 3,
    },
    {
      id: "cancel-1", parent: "done-1", ts: "2", status: "cancelled", prompt: "stop",
      engine: "cloud", model: "gpt-image-2", outputs: [output("kept.png")], cost_usd: 0.02,
    },
    {
      id: "draft-1", parent: null, ts: "3", status: "done", batch: "batch-1", tier: "draft", variant: 1,
      prompt: "a fox", engine: "cloud", model: "grok-imagine-image", outputs: [output("draft.png")],
    },
    {
      id: "final-1", parent: "draft-1", ts: "4", status: "done", batch: "batch-1", tier: "final", variant: 1,
      prompt: "a fox final", engine: "cloud", model: "grok-imagine-image", outputs: [output("final.png")],
    },
  ],
};

function fakeClient() {
  const ok = (body) => Promise.resolve(structuredClone(body));
  return {
    config: () => ok(directorConfig),
    history: () => ok(directorHistory),
    entry: () => ok(directorHistory.entries[0]),
    patchHistory: () => ok({ ok: true }),
    estimate: () => ok({ total: 0.05, est_cost_usd: 0.05, per_model: [{ model: "gpt-image-2", est_cost_usd: 0.05 }] }),
    write: () => ok({ positive: "A warm portrait", negative: "", variants: [{ prompt: "A warm portrait", negative: "", sections: {} }] }),
    importAsset: () => ok({ ref: { name: "upload.png", subfolder: "", type: "input" } }),
    importFromRef: () => ok({ ref: inputRef }),
    resize: () => ok({ items: [] }),
    viewUrl: (ref) => "/view?filename=" + encodeURIComponent(ref?.name || "")
      + "&subfolder=" + encodeURIComponent(ref?.subfolder || "")
      + "&type=" + encodeURIComponent(ref?.type || "input"),
    runCloud: () => ok({ job_id: "job-1", est_cost_usd: 0.05 }),
    cloudJobs: () => ok({ jobs: [] }),
    runBatch: () => ok({ batch_id: "batch-1", jobs: [] }),
    final: () => ok({ job_id: "job-2" }),
    cancelCloud: () => ok({}),
  };
}

function makeStore(patch = {}) {
  const errors = [];
  const base = defaultState();
  const store = createStore({
    project: "album",
    asset: { name: "canvas.png", type: "input", subfolder: "", w: 32, h: 24 },
    writer: { ...base.writer, model: "writer.gguf" },
    ...patch,
  });
  const subscribe = store.subscribe.bind(store);
  store.subscribe = (fn) => subscribe((state) => {
    try {
      return fn(state);
    } catch (err) {
      errors.push(err);
      throw err;
    }
  });
  return { store, errors };
}

const rejections = [];
process.on("unhandledRejection", (err) => { rejections.push(err); });

async function settle() {
  for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 240));
  for (let i = 0; i < 3; i++) await new Promise((r) => setTimeout(r, 0));
}

function buttonNamed(root, text) {
  return [...root.querySelectorAll("button")].find((node) => node.textContent.trim() === text) || null;
}

async function drive(root, store) {
  await settle();
  const thumb = root.querySelector("[data-id='done-1'] .ld-hthumb");
  if (thumb) thumb.click();
  else store.set({ selected: "done-1" });
  const generate = buttonNamed(root, "Generate");
  if (generate) generate.click();
  else store.act("setMode", "generate");
  await settle();
  const edit = buttonNamed(root, "Edit");
  if (edit) edit.click();
  else store.act("setMode", "edit");
  await settle();
  const compare = buttonNamed(root, "Compare");
  if (compare) compare.click();
  else {
    const c = store.get().compare || {};
    store.set({ compare: { on: !c.on, a: c.a ?? null, b: c.b ?? null } });
  }
  const modelSelect = root.querySelector("[aria-label='Cloud model']");
  if (modelSelect) {
    modelSelect.value = "grok-imagine-image";
    modelSelect.dispatchEvent({ type: "change" });
  } else {
    const engine = store.get().engine;
    store.set({ engine: { ...engine, model: "grok-imagine-image" } });
  }
  const request = root.querySelector("[aria-label='Writer request']");
  if (request) {
    request.focus();
    request.value = "make the coat red";
    request.dispatchEvent({ type: "input" });
  } else store.set({ request: "make the coat red" });
  await settle();
  const details = root.querySelector(".ld-history-details");
  if (details) assert.match(details.textContent, /Re-run/);
}

function check(errors) {
  const bad = [...errors, ...rejections];
  errors.length = 0;
  rejections.length = 0;
  if (bad.length) {
    assert.fail(bad.map((err) => (err && err.stack) || String(err)).join("\n"));
  }
}

function resetDom() {
  document.body.replaceChildren();
  document.head.replaceChildren();
  document.activeElement = null;
}

for (const row of SLOT_TABLE) {
  test("mount slot " + row.slot, async () => {
    resetDom();
    const { store, errors } = makeStore();
    const host = document.createElement("div");
    document.body.append(host);
    const handle = row.mount(host, store, fakeClient());
    try {
      await drive(host, store);
    } finally {
      handle?.destroy?.();
      host.remove();
    }
    check(errors);
  });
}

for (const mode of ["edit", "generate"]) {
  test("mount overlay on " + mode, async () => {
    resetDom();
    const { store, errors } = makeStore({ mode });
    const handle = mountOverlay({
      store, client: fakeClient(), node: null,
      socket: { subscribe(fn) { fn("open"); return () => {}; } },
    });
    try {
      await drive(document.body, store);
    } finally {
      handle.close();
    }
    check(errors);
  });
}
