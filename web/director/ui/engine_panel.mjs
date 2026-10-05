// Cloud controls come from /config. Common region controls use the shared engine state.
import { el, ensureCss } from "./dom.mjs";
import { defaultState } from "../core/store.mjs";
import { findModel } from "../core/generate.mjs";
import { refLimit, sentRefs } from "../core/writer_req.mjs";
import { runCurrent, installQueueActions } from "./run_queue.mjs";

const defaults = defaultState().engine.params;
const sides = new Map(), listeners = new Set();

export function registerEngineSide(kind, side) {
  if (!kind || !side?.label || typeof side.mount !== "function" || typeof side.canRun !== "function") {
    throw new TypeError("engine side needs kind, label, mount and canRun");
  }
  sides.set(kind, side);
  for (const fn of listeners) fn();
  return () => {
    if (sides.get(kind) !== side) return;
    sides.delete(kind);
    for (const fn of listeners) fn();
  };
}

export function engineSides() {
  return [...sides].map(([kind, side]) => ({ kind, ...side }));
}

export function controlsFor(config, id, operation) {
  const model = findModel(config, id);
  if (!model) return [];
  const common = ["operation", "n"];
  if (operation === "inpaint" || operation === "outpaint") common.push("mask_mode", "crop_padding");
  if (operation === "inpaint") common.push("feather_px");
  if (operation === "outpaint") common.push("outpaint");
  return [...new Set([...common, ...(model.controls || []), "seed"])];
}

export function optionsFor(model, key) {
  if (Array.isArray(model?.options?.[key])) return model.options[key];
  // Config's ref-limit keys are the server's operation list; no model table here.
  if (key === "operation") return Object.keys(model?.ref_limit || {});
  return [];
}

export function selectCloudModel(state, config, id) {
  const model = findModel(config, id);
  if (!model) return {};
  const params = { ...state.engine.params };
  for (const key of Object.keys(defaults)) {
    if (key === "seed") continue;
    const choices = optionsFor(model, key);
    if (choices.length && !choices.includes(params[key])) {
      params[key] = choices.includes(defaults[key]) ? defaults[key]
        : choices.includes(model.draft?.[key]) ? model.draft[key] : choices[0];
    }
  }
  // Hidden provider controls must not leak the previous model's unsupported values into a run.
  for (const key of ["quality", "background"]) {
    if (!model.controls?.includes(key)) params[key] = defaults[key];
  }
  return { engine: { ...state.engine, model: id, params } };
}

export function seedLabel(model) {
  return model?.options?.seed?.sent === false ? "Seed · recorded, not sent" : "Seed";
}

export function cloudCanRun(state) {
  if (!state.asset) return { ok: false, reason: "Run disabled: no image" };
  if (state.engine.params.operation === "inpaint" && state.maskEmpty !== false) {
    return { ok: false, reason: "Run disabled: the mask is empty" };
  }
  if (!String(state.prompt || "").trim()) return { ok: false, reason: "Run disabled: no prompt" };
  return { ok: true, reason: "" };
}

const refOf = (value) => value?.ref || (value?.name ? value : null);

export function cloudSpec(state, config, maskRef = refOf(state.mask)) {
  const p = state.engine.params, op = p.operation;
  return {
    ...Object.fromEntries(Object.keys(defaults).map((k) => [k, p[k] ?? defaults[k]])),
    model: state.engine.model, prompt: state.prompt, negative: state.negative || "", request: state.request || "",
    image: op === "generate" || op === "compose" ? null : refOf(state.asset),
    mask: op === "inpaint" ? maskRef : null,
    refs: sentRefs(state, config).map((r) => r.ref), parent: state.parent,
    writer: { provider: state.writer.provider, model: state.writer.model, negative_on: state.writer.negativeOn },
    outpaint: op === "outpaint" ? p.outpaint : null,
  };
}

export function estimateLabel(result) {
  if (result?.unknown?.length || result?.per_model?.some((m) => m.est_cost_usd == null)) return "estimate: after run";
  return Number.isFinite(result?.total) ? `estimate: $${result.total.toFixed(2)}` : "estimate: unavailable";
}

const LABELS = { operation: "Operation", quality: "Quality", aspect_ratio: "Aspect ratio", resolution: "Resolution",
  background: "Background", n: "Count", mask_mode: "Mask mode", crop_padding: "Padding", feather_px: "Feather", outpaint: "Outpaint" };

function mountCloud(root, store, client) {
  let config = null, alive = true, error = "", sequence = 0, timer = null, signature = "", shown = "";
  const model = el("select", { "aria-label": "Cloud model", disabled: true });
  const fields = el("div"), refs = el("p", { class: "ld-muted" });
  const estimate = el("p", { class: "ld-muted", role: "status", text: "estimate: …" });
  const failure = el("p", { class: "ld-engine-error", role: "alert" });
  const retry = el("button", { type: "button", class: "ld-btn", text: "Retry config", hidden: true });
  const field = (text, input) => el("label", { class: "ld-field" }, [el("span", { text }), input]);
  root.append(field("Model", model), fields, refs, estimate, failure, retry);
  const inputs = new Map();

  function patch(key, value) {
    const engine = store.get().engine;
    store.set({ engine: { ...engine, params: { ...engine.params, [key]: value } } });
  }
  function buildFields(s, info) {
    fields.replaceChildren(); inputs.clear();
    for (const key of controlsFor(config, info.id, s.engine.params.operation)) {
      const label = key === "seed" ? seedLabel(info) : LABELS[key] || key;
      if (key === "outpaint") {
        const row = el("div", { class: "ld-engine-margins" });
        const margins = ["L", "T", "R", "B"].map((side, i) => {
          const input = el("input", { type: "number", min: 0, step: 1, "aria-label": `Outpaint ${side} px` });
          input.addEventListener("change", () => {
            if (!input.checkValidity() || input.value === "") return;
            const value = [...(store.get().engine.params.outpaint || [0, 0, 0, 0])];
            value[i] = Number(input.value); patch(key, value);
          });
          row.append(field(side, input)); return input;
        });
        inputs.set(key, margins); fields.append(el("div", { class: "ld-field" }, [label + " (px)", row]));
        continue;
      }
      const choices = optionsFor(info, key);
      const numeric = key in defaults && typeof defaults[key] === "number" || key === "seed";
      const input = choices.length ? el("select", {}, choices.map((value) => el("option", { value, text: value })))
        : el("input", { type: numeric ? "number" : "text", step: key === "crop_padding" ? "0.05" : "1",
          min: key === "n" ? 1 : numeric ? 0 : null });
      input.setAttribute("aria-label", label);
      input.addEventListener("change", () => {
        if (!input.checkValidity() || (numeric && key !== "seed" && input.value === "")) return;
        patch(key, numeric ? (input.value === "" ? null : Number(input.value)) : input.value);
      });
      inputs.set(key, input);
      if (key === "seed") {
        const dice = el("button", { type: "button", class: "ld-btn", text: "⚄", "aria-label": "Random seed",
          onclick: () => patch("seed", Math.floor(Math.random() * 2147483647)) });
        fields.append(field(label, el("div", { class: "ld-engine-seed" }, [input, dice])));
      } else fields.append(field(label, input));
    }
  }
  function render() {
    if (!alive) return;
    const s = store.get(), info = findModel(config, s.engine.model);
    model.disabled = !config;
    model.value = s.engine.model || "";
    if (info) {
      const key = JSON.stringify([info.id, controlsFor(config, info.id, s.engine.params.operation)]);
      if (shown !== key) { shown = key; buildFields(s, info); }
      for (const [key, input] of inputs) {
        const values = Array.isArray(input) ? s.engine.params.outpaint || [0, 0, 0, 0] : [s.engine.params[key]];
        for (const [i, node] of (Array.isArray(input) ? input : [input]).entries()) {
          if (document.activeElement !== node) node.value = values[i] ?? "";
        }
      }
      refs.textContent = `Reference limit: ${refLimit(s, config)}`;
      const spec = cloudSpec(s, config);
      const next = JSON.stringify(spec);
      if (signature !== next) {
        signature = next; const seq = ++sequence;
        clearTimeout(timer); estimate.textContent = "estimate: …";
        timer = setTimeout(async () => {
          try {
            const result = await client.estimate({ spec });
            if (alive && seq === sequence) estimate.textContent = estimateLabel(result);
          } catch (_) {
            if (alive && seq === sequence) estimate.textContent = "estimate: unavailable";
          }
        }, 150);
      }
    }
    failure.textContent = error; failure.hidden = !error; retry.hidden = !!config;
  }
  async function load() {
    error = ""; retry.disabled = true;
    try {
      const cfg = await client.config();
      if (!alive) return;
      config = cfg;
      store.set({ cloudConfig: cfg });
      model.replaceChildren(...(cfg.cloud || []).map((g) => el("optgroup", { label: g.provider },
        (g.models || []).map((m) => el("option", { value: m.id, text: m.id })))));
      const id = findModel(cfg, store.get().engine.model)?.id || cfg.cloud?.flatMap((g) => g.models)[0]?.id;
      if (id) store.set(selectCloudModel(store.get(), cfg, id));
      else error = "No cloud models available.";
    } catch (_) { if (alive) error = "Engine config unavailable; retry."; }
    finally { if (alive) { retry.disabled = false; render(); } }
  }
  model.addEventListener("change", () => store.set(selectCloudModel(store.get(), config, model.value)));
  retry.addEventListener("click", () => { void load(); });
  const off = store.subscribe(render);
  render(); void load();
  return {
    get ready() { return !!findModel(config, store.get().engine.model); },
    destroy() { alive = false; ++sequence; clearTimeout(timer); off(); },
  };
}

registerEngineSide("cloud", { label: "Cloud", mount: mountCloud, canRun: cloudCanRun });

/** A side mounts its controls and returns {destroy, ready}; ready defaults to true. */
export function mountEngine(root, store, client) {
  installQueueActions(store, client);
  ensureCss(new URL("./engine_panel.css", import.meta.url));
  const panel = el("div", { class: "ld-engine" });
  const switcher = el("div", { class: "ld-engine-switch", role: "group", "aria-label": "Engine kind" });
  const body = el("div");
  const run = el("button", { type: "button", class: "ld-btn ld-engine-run", text: "Run" });
  const reason = el("p", { class: "ld-engine-error", role: "status" });
  panel.append(switcher, body, run, reason); root.append(panel);
  let kind = null, side = null, handle = null, busy = false, alive = true, error = "";
  function render() {
    if (!alive) return;
    const s = store.get();
    switcher.replaceChildren(...engineSides().map((item) => el("button", {
      type: "button", class: "ld-btn", text: item.label, "aria-pressed": item.kind === s.engine.kind,
      onclick: () => store.act("setEngineKind", item.kind),
    })));
    if (kind !== s.engine.kind || side !== sides.get(s.engine.kind)) {
      handle?.destroy?.(); body.replaceChildren(); kind = s.engine.kind; side = sides.get(kind);
      error = ""; handle = side?.mount(body, store, client);
    }
    const check = side?.canRun(s) ?? { ok: false, reason: "Engine unavailable" };
    const allowed = typeof check === "boolean" ? check : check.ok;
    run.disabled = busy || !allowed || handle?.ready === false;
    run.textContent = busy ? "Submitting…" : "Run";
    reason.textContent = error || (allowed ? "" : check.reason || "Run disabled");
    reason.hidden = !reason.textContent;
  }
  run.addEventListener("click", async () => {
    if (run.disabled) return;
    busy = true; error = ""; render();
    try { await runCurrent(store, client); }
    catch (_) { error = "Run failed; image, mask and prompt kept. Try again."; }
    finally { busy = false; render(); }
  });
  const off = store.subscribe(render);
  listeners.add(render); render();
  return { destroy() { alive = false; off(); listeners.delete(render); handle?.destroy?.(); panel.remove(); } };
}
