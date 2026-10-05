import { el, ensureCss } from "./dom.mjs";
import { SECTION_ORDER, editPrompt, editSection, assemblePrompt, entryModel } from "../core/generate.mjs";
import { negativeEnabled } from "./writer_panel.mjs";
import { generateTargets, mountGenerateEngine, runGenerate } from "./generate_engine.mjs";
import { mountGenerateWriter, writeGenerate, generateWriteReason } from "./generate_writer.mjs";
import { mountGenerateResults } from "./generate_results.mjs";

export async function refineGenerate(store, client, config, entry, feedback, confirm = () => false) {
  if (!feedback.trim()) return false;
  if (!await writeGenerate(store, client, config, { entry, feedback, confirm })) return false;
  return runGenerate(store, client, [entryModel(entry)], entry.id);
}

export function mountGenerate(root, store, client) {
  ensureCss(new URL("./generate.css", import.meta.url));
  const panel = el("div", { class: "ld-generate" }), promptPanel = el("section", { class: "ld-g-card ld-g-prompt" });
  const sidebar = el("aside", { class: "ld-g-sidebar" });
  const idea = el("textarea", { rows: 2, "aria-label": "Generate idea" });
  const write = el("button", { type: "button", class: "ld-btn ld-g-primary", text: "Write prompt" });
  const writeReason = el("p", { class: "ld-muted", role: "status" });
  const prompt = el("textarea", { rows: 3, "aria-label": "Generate prompt" });
  const variants = el("div", { class: "ld-g-segment" }), exact = el("input", { type: "text", "aria-label": "Generate exact text" });
  const stale = el("span", { class: "ld-muted", text: "edited by hand" });
  const reassemble = el("button", { type: "button", class: "ld-btn", text: "Re-assemble" });
  const sectionFields = new Map();
  const sections = el("details", { open: true }, [el("summary", { text: "sections" }), el("div", { class: "ld-g-heading" }, [stale, reassemble])]);
  const negative = el("textarea", { rows: 2, "aria-label": "Generate negative" }), negativeOn = el("input", { type: "checkbox", "aria-label": "Negative on" });
  const field = (label, node) => el("label", { class: "ld-field" }, [el("span", { text: label }), node]);
  promptPanel.append(field("idea", el("div", { class: "ld-g-idea" }, [idea, write])), writeReason,
    field("prompt", el("div", {}, [variants, prompt])),
    field("exact text", el("div", {}, [exact, el("small", { class: "ld-muted", text: "rendered exactly" })])), sections,
    field("negative", el("div", { class: "ld-g-negative" }, [negative, negativeOn])));
  panel.append(promptPanel, sidebar); root.append(panel);
  let config = null, alive = true, handles = [], variantShape = "";
  const patch = (part) => store.set({ generate: { ...store.get().generate, ...part } });
  function changeVariant(fn) {
    const g = store.get().generate, list = g.variants.length ? [...g.variants] : [{ prompt: "", negative: "", sections: {} }];
    const active = Math.min(g.active, list.length - 1); list[active] = fn(list[active]); patch({ variants: list, active });
  }
  for (const name of SECTION_ORDER) {
    const input = el("input", { type: "text", "aria-label": `Generate section ${name}` });
    input.addEventListener("input", () => changeVariant((v) => editSection(v, name, input.value)));
    sectionFields.set(name, input); sections.append(field(name, input));
  }
  idea.addEventListener("input", () => patch({ idea: idea.value }));
  exact.addEventListener("input", () => patch({ exactText: exact.value }));
  prompt.addEventListener("input", () => changeVariant((v) => editPrompt(v, prompt.value)));
  negative.addEventListener("input", () => changeVariant((v) => ({ ...v, negative: negative.value, edited: true })));
  negativeOn.addEventListener("change", () => store.set({ writer: negativeEnabled(store.get().writer, negativeOn.checked) }));
  reassemble.addEventListener("click", () => changeVariant((v) => ({ ...v, prompt: assemblePrompt(v.sections), edited: true, sectionsStale: false })));
  const confirm = (text) => globalThis.confirm(text);
  write.addEventListener("click", () => { void writeGenerate(store, client, config, { confirm }); });
  function render() {
    if (!alive) return;
    const s = store.get(), g = s.generate, v = g.variants[g.active] || {};
    const fill = (input, value) => { if (document.activeElement !== input) input.value = value || ""; };
    fill(idea, g.idea); fill(prompt, v.prompt); fill(exact, g.exactText); fill(negative, v.negative);
    for (const [name, input] of sectionFields) fill(input, v.sections?.[name]);
    const reason = generateWriteReason(s, config); write.disabled = !!reason;
    write.textContent = s.writer.busy ? "Writing…" : "Write prompt"; writeReason.textContent = reason;
    negativeOn.checked = s.writer.negativeOn; negative.disabled = !s.writer.negativeOn || s.writer.busy;
    for (const input of [idea, prompt, exact, negativeOn, reassemble, ...sectionFields.values()]) input.disabled = s.writer.busy;
    stale.hidden = !v.sectionsStale; reassemble.hidden = !v.sectionsStale;
    variants.hidden = g.variantsMode !== "varied";
    const shape = JSON.stringify([g.variants.length, g.active, s.writer.busy]);
    if (shape !== variantShape) {
      variantShape = shape;
      variants.replaceChildren(...g.variants.map((_, i) => el("button", { type: "button", class: "ld-btn", text: `v${i + 1}`,
        "aria-pressed": g.active === i, disabled: s.writer.busy, onclick: () => patch({ active: i }) })));
    }
  }
  const off = store.subscribe(render);
  const loadError = el("p", { class: "ld-g-error", role: "alert" }), retry = el("button", { type: "button", class: "ld-btn", text: "Retry config", hidden: true });
  sidebar.append(loadError, retry);
  async function load() {
    retry.disabled = true;
    try {
      const cfg = store.get().cloudConfig || await client.config();
      if (!alive) return;
      config = cfg; loadError.textContent = ""; retry.hidden = true;
      const target = generateTargets(cfg)[0];
      // Only the first mount selects a default; clearing all models remains a valid disabled state.
      const g = store.get().generate;
      store.set({ cloudConfig: cfg, generate: { ...g, initialized: true,
        models: !g.initialized && !g.models.length && target ? [target.id] : g.models } });
      handles = [mountGenerateWriter(sidebar, store, cfg), mountGenerateEngine(sidebar, store, client, cfg),
        mountGenerateResults(panel, store, client, cfg, (entry, feedback) => refineGenerate(store, client, cfg, entry, feedback, confirm))];
      render();
    } catch (_) { if (alive) { loadError.textContent = "Generate config unavailable; retry. Your idea is kept."; retry.hidden = false; } }
    finally { if (alive) retry.disabled = false; }
  }
  retry.addEventListener("click", () => { void load(); }); render(); void load();
  return { destroy() { alive = false; off(); for (const handle of handles) handle.destroy(); panel.remove(); } };
}
