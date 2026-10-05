import { el } from "./dom.mjs";
import { findModel, entryModel } from "../core/generate.mjs";
import { buildWriteBody, buildRefineBody, mergeGenerateResult, mergeWriterError } from "../core/writer_req.mjs";
import { writerProviders, THINKING_HELP } from "./writer_panel.mjs";

export function generateWriterPreset(state, config) {
  return state.writer.preset.generate || findModel(config, state.generate.models[0])?.generate_preset || "";
}

export function generateWriteReason(state, config, refining = false) {
  if (state.writer.busy || state.generate.submitting) return "Writer is busy";
  if (!refining && !state.generate.idea.trim()) return "Type an idea first";
  if (!config) return "Waiting for config";
  if (!state.generate.models.length) return "Choose a target model first";
  if (!state.writer.model.trim()) return "Choose a writer model in the Edit tab";
  if (state.writer.provider === "Custom" && !state.writer.serverUrl.trim()) return "Enter a server URL in the Edit tab";
  return "";
}

export async function writeGenerate(store, client, config, { entry = null, feedback = "", confirm = () => false } = {}) {
  const state = store.get();
  const bodyState = { ...state, mode: "generate", generate: entry
    ? { ...state.generate, models: [entryModel(entry)] } : state.generate };
  if (generateWriteReason(bodyState, config, !!entry)) return false;
  // Ask through the core's overwrite guard before spending a writer call.
  const needsConfirm = mergeGenerateResult(state, { variants: [] }).needsConfirm;
  if (needsConfirm && !await confirm("Replace the prompts you edited?")) return false;
  const body = entry ? buildRefineBody(bodyState, config, entry, feedback) : buildWriteBody(bodyState, config);
  store.set({ writer: { ...state.writer, busy: true, error: null }, generate: { ...state.generate, error: null } });
  try {
    const result = await client.write(body);
    store.set(mergeGenerateResult(store.get(), result, { force: true }));
    return true;
  } catch (e) {
    store.set(mergeWriterError(store.get(), e));
    return false;
  }
}

export function mountGenerateWriter(root, store, config) {
  const provider = el("select", { "aria-label": "Generate writer provider" }, writerProviders(config).map((p) => el("option", { value: p, text: p })));
  const preset = el("select", { "aria-label": "Generate writer preset" });
  const auto = el("span", { class: "ld-g-tag", text: "auto" });
  const thinking = el("input", { type: "checkbox", title: THINKING_HELP });
  const panel = el("section", { class: "ld-g-card" }, [el("h3", { text: "Writer" }),
    el("label", { class: "ld-field" }, ["provider", provider]),
    el("label", { class: "ld-field" }, ["preset", el("div", { class: "ld-g-preset" }, [preset, auto])]),
    el("label", { class: "ld-g-toggle", title: THINKING_HELP }, [thinking, "thinking"])]);
  root.append(panel);
  const patch = (part) => store.set({ writer: { ...store.get().writer, ...part } });
  provider.addEventListener("change", () => patch({ provider: provider.value }));
  preset.addEventListener("change", () => patch({ preset: { ...store.get().writer.preset, generate: preset.value || null } }));
  thinking.addEventListener("change", () => patch({ thinking: thinking.checked }));
  let shown = "";
  function render() {
    const s = store.get(), w = s.writer, automatic = findModel(config, s.generate.models[0])?.generate_preset || "";
    if (![...provider.options].some((p) => p.value === w.provider)) provider.append(el("option", { value: w.provider, text: w.provider }));
    provider.value = w.provider; thinking.checked = w.thinking;
    const signature = JSON.stringify([automatic, w.preset.generate]);
    if (signature !== shown) {
      shown = signature;
      const titles = [...new Set((config.presets || []).map((p) => typeof p === "string" ? p : p.title))];
      if (w.preset.generate && !titles.includes(w.preset.generate)) titles.push(w.preset.generate);
      preset.replaceChildren(el("option", { value: "", text: automatic || "Auto preset" }),
        ...titles.map((p) => el("option", { value: p, text: p })));
    }
    preset.value = w.preset.generate || ""; auto.hidden = !!w.preset.generate;
    for (const input of [provider, preset, thinking]) input.disabled = w.busy;
  }
  const off = store.subscribe(render); render();
  return { destroy() { off(); panel.remove(); } };
}
