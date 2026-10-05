// S3 writer: the core owns bodies, result merging and copying into the edit prompt.
import { el } from "./dom.mjs";
import { buildWriteBody, refLimit, mergeWriterResult, mergeWriterError, useAsEditPrompt } from "../core/writer_req.mjs";

export { useAsEditPrompt };
export const THINKING_HELP = "On subscription CLIs, off means the CLI's lowest effort (low). Grok cannot turn reasoning off.";

export function writerFields(provider) {
  return { model: true, serverUrl: provider === "Custom", thinking: true, negativeOn: true };
}

export function writerProviders(config) {
  return ["Local GGUF", ...(config?.writers?.providers || [])];
}

export function defaultWriterPreset(state, config) {
  return (config?.cloud || []).flatMap((group) => group.models || [])
    .find((model) => model.id === state.engine.model)?.default_preset || "";
}

export function negativeEnabled(writer, on) {
  return { ...writer, negativeOn: !!on, negative: on ? writer.negative : "" };
}

export function writerButtonState(state, ready = true) {
  if (state.writer.busy) return { disabled: true, label: `Writing… ${state.writer.provider}` };
  if (!state.request.trim()) return { disabled: true, label: "Write prompt — type a request first" };
  if (!ready) return { disabled: true, label: "Write prompt — waiting for config" };
  if (!state.writer.model.trim()) return { disabled: true, label: "Write prompt — choose a model first" };
  if (writerFields(state.writer.provider).serverUrl && !state.writer.serverUrl.trim()) {
    return { disabled: true, label: "Write prompt — enter a server URL first" };
  }
  return { disabled: false, label: "Write prompt" };
}

// Dynamic import keeps this UI module importable in Node, while using ComfyUI's base-path-aware transport.
async function modelFetch(path, options) {
  const { api } = await import("/scripts/api.js");
  return api.fetchApi(path, options);
}

export async function loadWriterModels(provider, serverUrl = "", fetchApi = modelFetch) {
  const params = new URLSearchParams({ provider });
  if (provider === "Custom" && serverUrl) params.set("server_url", serverUrl);
  const res = await fetchApi(`/llm_prompt_api/models?${params}`, { method: "GET" });
  const data = await res.json();
  if (!res.ok || data?.error) throw new Error("Model list unavailable; enter a model or refresh.");
  return [...new Set((data?.models || []).filter((m) => typeof m === "string" && m && !m.startsWith("<")))];
}

export async function writePrompt(store, client, config) {
  if (writerButtonState(store.get(), !!config).disabled) return false;
  store.set({ writer: { ...store.get().writer, busy: true, error: null } });
  try {
    if (store.get().mode !== "generate" && store.get().engine.params.operation === "inpaint") await store.act("uploadMask");
    const result = await client.write(buildWriteBody(store.get(), config));
    store.set(mergeWriterResult(store.get(), result));
  } catch (err) {
    store.set(mergeWriterError(store.get(), err));
  }
  return true;
}

let nextId = 0;

export function mountWriter(root, store, client) {
  let config = null, alive = true, modelSeq = 0;
  let configError = "", modelError = "", modelBusy = false, shownRoute = null;
  const panel = el("div", { class: "ld-writer" });
  const provider = el("select", { "aria-label": "Writer provider" });
  const modelId = `ld-writer-models-${++nextId}`;
  const models = el("datalist", { id: modelId });
  const model = el("input", { type: "text", list: modelId, "aria-label": "Writer model", spellcheck: "false" });
  const refresh = el("button", { type: "button", class: "ld-btn", text: "Refresh models" });
  const server = el("input", { type: "url", "aria-label": "Writer server URL", spellcheck: "false" });
  const preset = el("select", { "aria-label": "Writer preset" });
  const autoTag = el("p", { class: "ld-muted" });
  const request = el("textarea", { rows: 3, "aria-label": "Writer request" });
  const positive = el("textarea", { rows: 4, "aria-label": "Writer positive prompt" });
  const negative = el("textarea", { rows: 2, "aria-label": "Writer negative prompt" });
  const write = el("button", { type: "button", class: "ld-btn ld-writer-primary" });
  const use = el("button", { type: "button", class: "ld-btn ld-writer-use", text: "Use as edit prompt" });
  const error = el("p", { class: "ld-writer-error", role: "alert" });
  const status = el("p", { class: "ld-muted", role: "status" });
  const field = (label, input) => el("label", { class: "ld-field" }, [el("span", { text: label }), input]);
  const serverField = field("Server URL", server);
  const negativeField = field("Negative", negative);
  const switches = {};
  const switchLabels = {};
  function toggle(key, text, title = "") {
    const input = el("input", { type: "checkbox", title });
    const label = el("span", { text });
    switches[key] = input;
    switchLabels[key] = label;
    input.addEventListener("change", () => {
      const w = store.get().writer;
      if (key === "negativeOn") store.set({ writer: negativeEnabled(w, input.checked) });
      else if (key === "thinking") patchWriter({ thinking: input.checked });
      else patchWriter({ send: { ...w.send, [key]: input.checked } });
    });
    return el("label", { class: "ld-writer-toggle", title }, [input, label]);
  }
  const send = el("div", { class: "ld-writer-switches" }, [
    toggle("canvas", "Marked canvas"), toggle("mask", "Region close-up"), toggle("refs", "References (0)"),
  ]);
  const options = el("div", { class: "ld-writer-switches" }, [
    toggle("negativeOn", "Negative on"), toggle("thinking", "Thinking", THINKING_HELP),
  ]);
  panel.append(field("Provider", provider), field("Model", model), models, refresh, serverField,
    field("Preset", preset), autoTag, send, options, field("Request", request), write, status, error,
    field("Positive", positive), negativeField, use);
  root.append(panel);

  function patchWriter(patch) { store.set({ writer: { ...store.get().writer, ...patch } }); }
  function fill(input, value) {
    if (document.activeElement !== input && input.value !== value) input.value = value;
  }
  function fillPresets() {
    const s = store.get();
    const auto = defaultWriterPreset(s, config);
    const titles = (config?.presets || []).map((p) => p.title);
    const selected = s.writer.preset?.edit || "";
    if (selected && !titles.includes(selected)) titles.unshift(selected);
    const items = [["", `Auto${auto ? ` · ${auto}` : " · target default"}`], ...titles.map((t) => [t, t])];
    const signature = JSON.stringify(items);
    if (preset.dataset.options !== signature) {
      preset.replaceChildren(...items.map(([value, text]) => el("option", { value, text })));
      preset.dataset.options = signature;
    }
    preset.value = selected;
    autoTag.textContent = `${selected ? "override" : "auto"} · target ${s.engine.model || "not selected"}`;
  }
  function render() {
    const s = store.get(), w = s.writer;
    const providers = writerProviders(config);
    if (!providers.includes(w.provider)) providers.push(w.provider);
    if (provider.dataset.options !== JSON.stringify(providers)) {
      provider.replaceChildren(...providers.map((p) => el("option", { value: p, text: p })));
      provider.dataset.options = JSON.stringify(providers);
    }
    provider.value = w.provider;
    fill(model, w.model); fill(server, w.serverUrl || "");
    fill(request, s.request); fill(positive, w.positive); fill(negative, w.negative);
    fillPresets();
    serverField.hidden = !writerFields(w.provider).serverUrl;
    negativeField.hidden = !w.negativeOn;
    for (const [key, input] of Object.entries(switches)) {
      input.checked = key in w.send ? w.send[key] : w[key];
      input.disabled = w.busy;
    }
    switchLabels.refs.textContent = `References (${Math.min(s.refs.length, refLimit(s, config || {}))})`;
    for (const input of [provider, model, server, preset, positive, negative]) input.disabled = w.busy;
    refresh.disabled = w.busy || modelBusy;
    const button = writerButtonState(s, !!config);
    write.disabled = button.disabled;
    write.replaceChildren(...(w.busy ? [el("span", { class: "ld-spin", "aria-hidden": "true" })] : []),
      el("span", { text: button.label }));
    write.setAttribute("aria-busy", String(w.busy));
    use.disabled = w.busy || !w.positive.trim();
    error.textContent = w.error ? `${w.error.code}: ${w.error.message} — request kept` : configError || modelError;
    error.hidden = !error.textContent;
    status.textContent = modelBusy ? "Loading models…" : "";
    status.hidden = !modelBusy;
    const route = JSON.stringify([w.provider, w.provider === "Custom" ? w.serverUrl : ""]);
    if (config && route !== shownRoute) {
      shownRoute = route;
      void refreshModels();
    }
  }
  async function refreshModels() {
    const seq = ++modelSeq;
    const w = store.get().writer;
    modelBusy = true; modelError = "";
    models.replaceChildren();
    render();
    try {
      const list = w.provider === "Local GGUF" ? config.writers["Local GGUF"] || []
        : await loadWriterModels(w.provider, w.serverUrl);
      if (!alive || seq !== modelSeq) return;
      models.replaceChildren(...list.map((value) => el("option", { value })));
      if (!store.get().writer.model && list.length) patchWriter({ model: list[0] });
    } catch (_) {
      if (!alive || seq !== modelSeq) return;
      modelError = "models: Model list unavailable; enter a model or refresh.";
    } finally {
      if (alive && seq === modelSeq) { modelBusy = false; render(); }
    }
  }
  async function loadConfig() {
    configError = "";
    try {
      const cfg = await client.config();
      if (!alive) return;
      config = cfg;
      render();
    } catch (_) {
      if (!alive) return;
      configError = "config: Writer config unavailable; use Refresh models to retry.";
      render();
    }
  }
  provider.addEventListener("change", () => patchWriter({ provider: provider.value, model: "", error: null }));
  model.addEventListener("input", () => patchWriter({ model: model.value }));
  server.addEventListener("change", () => patchWriter({ serverUrl: server.value.trim() }));
  preset.addEventListener("change", () => patchWriter({ preset: { ...store.get().writer.preset, edit: preset.value || null } }));
  request.addEventListener("input", () => store.set({ request: request.value }));
  positive.addEventListener("input", () => patchWriter({ positive: positive.value }));
  negative.addEventListener("input", () => patchWriter({ negative: negative.value }));
  refresh.addEventListener("click", () => { if (config) void refreshModels(); else void loadConfig(); });
  write.addEventListener("click", () => { void writePrompt(store, client, config); });
  use.addEventListener("click", () => store.set(useAsEditPrompt(store.get())));
  const off = store.subscribe(render);
  render();
  void loadConfig();
  return { destroy() { alive = false; ++modelSeq; off(); panel.remove(); } };
}
