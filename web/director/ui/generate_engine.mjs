// Generate controls are config driven; batch expansion and tier settings belong to the server.
import { el } from "./dom.mjs";
import { autoOperation, findModel } from "../core/generate.mjs";
import { engineSides, optionsFor, seedLabel } from "./engine_panel.mjs";
import { submitBatch } from "./run_queue.mjs";

// Registered sides may expose generateTargets(config) for future local engines.
export function generateTargets(config) {
  return engineSides().flatMap((side) => side.generateTargets ? side.generateTargets(config)
    : side.kind === "cloud" ? (config?.cloud || []).flatMap((group) => group.models || []) : []);
}

export function generateCanRun(state) {
  const g = state.generate, count = g.params.count;
  if (!g.models.length) return { ok: false, reason: "Run disabled: no model" };
  if (g.models.length > 4) return { ok: false, reason: "Run disabled: choose at most 4 models" };
  if (!Number.isInteger(count) || count < 1) return { ok: false, reason: "Run disabled: count must be a whole number ≥ 1" };
  const want = g.variantsMode === "varied" ? count : 1;
  if (g.variants.length < want || g.variants.slice(0, want).some((v) => !v.prompt?.trim())) {
    return { ok: false, reason: "Run disabled: no prompt — write every variant first" };
  }
  if (state.writer.busy || g.submitting) return { ok: false, reason: "Run disabled: working…" };
  return { ok: true, reason: "" };
}

export function batchBody(state, models = state.generate.models, parent = null) {
  const g = state.generate, w = state.writer;
  return {
    models: [...models], ...g.params, variants_mode: g.variantsMode, tier: g.tier,
    prompts: g.variants.slice(0, g.variantsMode === "varied" ? g.params.count : 1).map((v) => ({
      prompt: v.prompt, negative: w.negativeOn ? v.negative || "" : "", sections: v.sections || null,
    })),
    refs: state.refs.map((r) => r.ref), exact_text: g.exactText, request: g.idea, parent,
    writer: { provider: w.provider, model: w.model, negative_on: w.negativeOn },
  };
}

export function generateEstimateLabel(result, count) {
  if (!result) return "estimate: unavailable";
  const unknown = [...new Set([...(result.unknown || []),
    ...(result.per_model || []).filter((m) => m.est_cost_usd == null).map((m) => m.model)])];
  const allUnknown = unknown.length >= count;
  const known = Number.isFinite(result.total) && !allUnknown ? `$${result.total.toFixed(2)}` : "after run";
  return `estimate: ${known} (${count} ${count === 1 ? "model" : "models"})`
    + (unknown.length ? ` + ${unknown.join(", ")} after run` : "");
}

export async function runGenerate(store, client, models = null, parent = null) {
  const state = store.get();
  const check = generateCanRun(models ? { ...state, generate: { ...state.generate, models } } : state);
  if (!check.ok) return check;
  store.set({ generate: { ...state.generate, submitting: true, error: null } });
  try { return await submitBatch(store, client, batchBody(state, models || state.generate.models, parent)); }
  catch (e) {
    store.set({ generate: { ...store.get().generate, error: { message: e.message || String(e), models: models || state.generate.models } } });
    throw e;
  } finally { store.set({ generate: { ...store.get().generate, submitting: false } }); }
}

export function mountGenerateEngine(root, store, client, config) {
  const panel = el("section", { class: "ld-g-card ld-g-engine" }, [el("h3", { text: "Engine" })]);
  const models = el("div", { class: "ld-g-models" }), compare = el("span", { class: "ld-g-tag", text: "compare" });
  const operation = el("p", { class: "ld-g-operation" }), fields = el("div");
  const estimate = el("p", { class: "ld-muted", role: "status" }), tiers = el("div", { class: "ld-g-segment" });
  const hint = el("p", { class: "ld-muted" }), run = el("button", { type: "button", class: "ld-btn ld-g-primary", text: "Run" });
  const reason = el("p", { class: "ld-muted", role: "status" });
  panel.append(el("div", { class: "ld-g-heading" }, ["models", compare]), models, operation, fields, estimate, tiers, hint, run, reason);
  root.append(panel);
  let alive = true, signature = "", shape = "", seq = 0, timer;
  const inputs = new Map(), targets = generateTargets(config);
  const patch = (part) => store.set({ generate: { ...store.get().generate, ...part } });
  function buildFields(selected) {
    fields.replaceChildren(); inputs.clear();
    const keys = [...new Set(["aspect_ratio", "resolution", "count", "seed", ...selected.flatMap((m) => m.controls || [])])]
      .filter((k) => !["operation", "n"].includes(k));
    for (const key of keys) {
      const choices = [...new Set(selected.flatMap((m) => optionsFor(m, key)))];
      const numeric = key === "count" || key === "seed";
      const input = choices.length ? el("select", {}, choices.map((v) => el("option", { value: v, text: v })))
        : el("input", { type: numeric ? "number" : "text", min: numeric ? (key === "count" ? 1 : 0) : null, step: 1 });
      const label = key === "seed" ? (selected.some((m) => seedLabel(m).includes("recorded")) ? "seed · recorded, not sent" : "seed")
        : key.replaceAll("_", " ");
      input.setAttribute("aria-label", `Generate ${label}`);
      input.addEventListener("change", () => {
        if (!input.checkValidity()) return;
        patch({ params: { ...store.get().generate.params, [key]: numeric ? (input.value === "" ? null : Number(input.value)) : input.value } });
      });
      inputs.set(key, input);
      const row = key === "seed" ? el("div", { class: "ld-g-seed" }, [input, el("button", {
        type: "button", class: "ld-btn", text: "⚄", "aria-label": "Random Generate seed",
        onclick: () => patch({ params: { ...store.get().generate.params, seed: Math.floor(Math.random() * 2147483647) } }),
      })]) : input;
      fields.append(el("label", { class: "ld-field" }, [el("span", { text: label }), row]));
    }
  }
  for (const target of targets) {
    const input = el("input", { type: "checkbox", value: target.id });
    input.addEventListener("change", () => {
      const g = store.get().generate;
      patch({ models: input.checked ? [...g.models, target.id].slice(0, 4) : g.models.filter((id) => id !== target.id) });
    });
    models.append(el("label", { class: "ld-g-model" }, [input, target.id]));
  }
  for (const tier of ["draft", "final"]) tiers.append(el("button", { type: "button", class: "ld-btn", text: tier === "draft" ? "Draft" : "Final",
    onclick: () => patch({ tier }) }));
  run.addEventListener("click", () => { void runGenerate(store, client).catch(() => {}); });
  function render() {
    const s = store.get(), g = s.generate, selected = g.models.map((id) => findModel(config, id)).filter(Boolean);
    for (const input of models.querySelectorAll("input")) {
      input.checked = g.models.includes(input.value); input.disabled = !input.checked && g.models.length >= 4;
    }
    compare.hidden = g.models.length < 2;
    const operations = [...new Set(g.models.map((id) => autoOperation(id, s.refs.length, config)))];
    operation.textContent = `operation: ${operations.join(" / ") || "generate"} (auto)`;
    operation.title = g.models.map((id) => `${id}: ${autoOperation(id, s.refs.length, config)}`).join(" · ");
    const nextShape = JSON.stringify(selected.map((m) => m.id));
    if (nextShape !== shape) { shape = nextShape; buildFields(selected); }
    for (const [key, input] of inputs) {
      if (document.activeElement !== input) input.value = g.params[key] ?? "";
      // Keep the shared value selectable; the server falls back per model when unsupported.
      if (input.tagName === "SELECT" && input.value === "") {
        input.append(el("option", { value: g.params[key], text: g.params[key] })); input.value = g.params[key];
      }
    }
    for (const button of tiers.children) button.setAttribute("aria-pressed", String(button.textContent.toLowerCase() === g.tier));
    hint.textContent = g.tier === "draft" ? Object.values(selected[0]?.draft || {}).join(" · ") : "Final settings per model";
    const check = generateCanRun(s); run.disabled = !check.ok; reason.textContent = check.reason;
    const body = batchBody(s), next = JSON.stringify(body);
    if (next !== signature) {
      signature = next; const current = ++seq; clearTimeout(timer);
      estimate.textContent = "estimate: …";
      if (!g.models.length || !body.prompts.length || (g.variantsMode === "varied" && body.prompts.length !== g.params.count)) {
        estimate.textContent = "estimate: write prompts and choose models"; return;
      }
      timer = setTimeout(async () => {
        try {
          const result = await client.estimate({ batch: body });
          if (alive && current === seq) { estimate.textContent = generateEstimateLabel(result, body.models.length); patch({ estimate: result }); }
        } catch (_) { if (alive && current === seq) estimate.textContent = "estimate: unavailable"; }
      }, 150);
    }
  }
  const off = store.subscribe(render); render();
  return { destroy() { alive = false; ++seq; clearTimeout(timer); off(); panel.remove(); } };
}
