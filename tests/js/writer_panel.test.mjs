import test from "node:test";
import assert from "node:assert/strict";
import { createStore, defaultState } from "../../web/director/core/store.mjs";
import { createClient } from "../../web/director/core/api_client.mjs";
import { SLOT_TABLE } from "../../web/director/frames/overlay.mjs";
import {
  mountWriter, writerFields, writerProviders, defaultWriterPreset, negativeEnabled,
  writerButtonState, loadWriterModels, writePrompt, useAsEditPrompt, THINKING_HELP,
} from "../../web/director/ui/writer_panel.mjs";

const providers = ["Gemini", "Grok (xAI)", "OpenAI", "Claude (Max)", "Codex (ChatGPT)", "Grok (SuperGrok)", "Custom"];
const config = {
  writers: { "Local GGUF": ["local.gguf"], providers },
  presets: [{ title: "Server preset" }],
  cloud: [{ provider: "test", models: [
    { id: "target", default_preset: "Server preset", ref_limit: { inpaint: 1 } },
    { id: "other", default_preset: "Other server preset" },
  ] }],
};
function ready(over = {}) {
  const s = defaultState();
  return { ...s, request: "  Keep my words\nexactly.  ",
    engine: { ...s.engine, model: "target", params: { ...s.engine.params, operation: "edit" } },
    writer: { ...s.writer, model: "local.gguf" }, ...over };
}

test("writer mounts in the shell slot and uses every configured provider", () => {
  assert.deepEqual(writerProviders(config), ["Local GGUF", ...providers]);
  const row = SLOT_TABLE.find((r) => r.slot === "writer");
  assert.equal(row.mount, mountWriter);
  assert.equal(SLOT_TABLE.filter((r) => r.slot === "writer").length, 1);
});

test("Custom alone shows server URL; API, CLI and GGUF all show thinking and negative switches", () => {
  for (const provider of writerProviders(config)) {
    assert.deepEqual(writerFields(provider), {
      model: true, serverUrl: provider === "Custom", thinking: true, negativeOn: true,
    });
  }
  assert.match(THINKING_HELP, /lowest effort \(low\)/);
  assert.match(THINKING_HELP, /Grok cannot turn reasoning off/);
});

test("auto preset follows the chosen image model's server config, never request words", () => {
  const s = ready({ request: "other image model" });
  assert.equal(defaultWriterPreset(s, config), "Server preset");
  assert.equal(defaultWriterPreset({ ...s, engine: { ...s.engine, model: "other" } }, config), "Other server preset");
  assert.equal(defaultWriterPreset(s, { cloud: [] }), "");
});

test("write button distinguishes empty request, busy, config, model and Custom URL", () => {
  const s = ready();
  assert.deepEqual(writerButtonState(s), { disabled: false, label: "Write prompt" });
  assert.deepEqual(writerButtonState({ ...s, request: " \n\t " }), {
    disabled: true, label: "Write prompt — type a request first",
  });
  assert.deepEqual(writerButtonState({ ...s, writer: { ...s.writer, busy: true } }), {
    disabled: true, label: "Writing… Local GGUF",
  });
  assert.equal(writerButtonState(s, false).disabled, true);
  assert.equal(writerButtonState({ ...s, writer: { ...s.writer, model: " " } }).disabled, true);
  const custom = { ...s, writer: { ...s.writer, provider: "Custom", serverUrl: " " } };
  assert.equal(writerButtonState(custom).disabled, true);
  assert.equal(writerButtonState({ ...custom, writer: { ...custom.writer, serverUrl: "http://localhost:8000/v1" } }).disabled, false);
});

test("negative off clears output immediately and Use as edit prompt clears an existing edit negative", () => {
  const s = ready();
  s.writer = { ...s.writer, positive: "P", negative: "N", negativeOn: true };
  const on = useAsEditPrompt(s);
  assert.deepEqual(on, { prompt: "P", negative: "N" });
  const w = negativeEnabled(s.writer, false);
  assert.equal(w.negative, "");
  assert.equal(s.writer.negative, "N");
  const store = createStore({ ...s, writer: w, prompt: "old", negative: "old negative" });
  store.set(useAsEditPrompt(store.get()));
  assert.equal(store.get().prompt, "P");
  assert.equal(store.get().negative, "");
  assert.equal(store.get().request, s.request);
  assert.equal(negativeEnabled(w, true).negative, "");
});

test("model list reuses the existing route with Custom URL encoded and drops placeholder values", async () => {
  let call;
  const list = await loadWriterModels("Custom", "http://localhost:8000/v1?x=1&y=2", async (path, opts) => {
    call = { path, opts };
    return { ok: true, json: async () => ({ models: ["m", "m", "<set server_url and refresh>", 1, "n"] }) };
  });
  assert.deepEqual(list, ["m", "n"]);
  const url = new URL(call.path, "http://localhost");
  assert.equal(url.pathname, "/llm_prompt_api/models");
  assert.equal(url.searchParams.get("provider"), "Custom");
  assert.equal(url.searchParams.get("server_url"), "http://localhost:8000/v1?x=1&y=2");
  assert.deepEqual(call.opts, { method: "GET" });
  await loadWriterModels("Gemini", "http://localhost:8000", async (path) => {
    assert.equal(new URL(path, "http://localhost").searchParams.has("server_url"), false);
    return { ok: true, json: async () => ({ models: ["gemini"] }) };
  });
});

test("model list failures produce a bounded retryable message", async () => {
  for (const response of [
    { ok: false, json: async () => ({}) },
    { ok: true, json: async () => ({ error: "unavailable" }) },
  ]) await assert.rejects(loadWriterModels("Gemini", "", async () => response), /Model list unavailable/);
});

test("write keeps the request byte for byte, sends switches on every route and keeps both outputs", async () => {
  for (const provider of writerProviders(config)) {
    const s = ready();
    s.writer = { ...s.writer, provider, thinking: true, negativeOn: true,
      serverUrl: "http://localhost:8000/v1", preset: { edit: "My override", generate: "Separate" } };
    const store = createStore(s);
    await writePrompt(store, { write: async (body) => {
      assert.equal(body.provider, provider);
      assert.equal(body.request, s.request);
      assert.equal(body.thinking, true);
      assert.equal(body.negative, true);
      assert.equal(body.preset, "My override");
      assert.equal(body.target_model, "target");
      assert.equal(body.server_url, s.writer.serverUrl);
      assert.equal(store.get().writer.busy, true);
      return { positive: "P", negative: "N" };
    } }, config);
    assert.equal(store.get().request, s.request);
    assert.equal(store.get().writer.positive, "P");
    assert.equal(store.get().writer.negative, "N");
    assert.equal(store.get().writer.busy, false);
    assert.equal(store.get().prompt, "");
  }
});

test("negative off is sent to the writer and drops even an unexpected returned negative", async () => {
  const store = createStore(ready());
  await writePrompt(store, { write: async (body) => {
    assert.equal(body.negative, false);
    assert.equal(body.thinking, false);
    assert.equal(body.preset, null); // auto remains server-owned
    return { positive: "P", negative: "unexpected" };
  } }, config);
  assert.equal(store.get().writer.negative, "");
});

test("a pending write preserves newly typed request and ignores a second click", async () => {
  const store = createStore(ready());
  let resolve, calls = 0;
  const client = { write: () => { ++calls; return new Promise((r) => { resolve = r; }); } };
  const pending = writePrompt(store, client, config);
  store.set({ request: "New words while writing" });
  assert.equal(await writePrompt(store, client, config), false);
  resolve({ positive: "P", negative: "N" });
  await pending;
  assert.equal(calls, 1);
  assert.equal(store.get().request, "New words while writing");
});

test("named 409 busy error retains request, previous outputs and edit prompt", async () => {
  const s = ready({ prompt: "edit prompt" });
  s.writer = { ...s.writer, positive: "previous positive" };
  const store = createStore(s);
  const client = createClient(async () => ({ ok: false, status: 409,
    json: async () => ({ error: { code: "busy", message: "GGUF busy: a graph run holds the model" } }),
  }));
  await writePrompt(store, client, config);
  assert.equal(store.get().request, s.request);
  assert.equal(store.get().prompt, "edit prompt");
  assert.equal(store.get().writer.positive, "previous positive");
  assert.equal(store.get().writer.busy, false);
  assert.deepEqual(store.get().writer.error, { code: "busy", message: "GGUF busy: a graph run holds the model" });
});

test("failed write preserves request edits made while pending and can retry", async () => {
  const store = createStore(ready());
  let reject;
  const pending = writePrompt(store, { write: () => new Promise((_, r) => { reject = r; }) }, config);
  store.set({ request: "Keep the revised request too" });
  reject(Object.assign(new Error("Bad model"), { code: "provider" }));
  await pending;
  assert.equal(store.get().request, "Keep the revised request too");
  assert.equal(store.get().writer.error.code, "provider");
  await writePrompt(store, { write: async () => ({ positive: "retry" }) }, config);
  assert.equal(store.get().writer.error, null);
  assert.equal(store.get().writer.positive, "retry");
});

test("disabled write performs no call or state change", async () => {
  const store = createStore(ready({ request: " " }));
  const before = store.get();
  assert.equal(await writePrompt(store, { write: () => assert.fail("unexpected write") }, config), false);
  assert.equal(store.get(), before);
});
