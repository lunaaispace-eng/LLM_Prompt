import { el } from "./dom.mjs";
import { entryModel, findModel, groupByModel } from "../core/generate.mjs";
import { editFromHere } from "../core/history.mjs";
import { ACTIVE } from "../core/queue.mjs";
import { USE_IN_GRAPH_REASON } from "./compare.mjs";
import { elapsedLabel } from "./queue_panel.mjs";
import { submitFinal } from "./run_queue.mjs";

export function resultEntries(state) {
  const entries = new Map((state.history || []).map((e) => [e.id, e]));
  for (const job of Object.values(state.jobs || {})) if (job.entry?.id) {
    entries.set(job.entry.id, { ...job.entry, cancelled: job.cancelledAfterSend || job.state === "cancelled" });
  }
  return [...entries.values()];
}

export function finalMenuEntries(config, entry, resolution) {
  const model = findModel(config, entryModel(entry));
  return (model?.final_options || []).map((value) => ({ label: value,
    choice: optionsInclude(model, "quality", value) ? { quality: value, resolution } : { resolution: value } }));
}
const optionsInclude = (model, key, value) => (model?.options?.[key] || []).includes(value);

export function variantsModePatch(generate, mode) {
  return { ...generate, variantsMode: mode, active: mode === "same" ? 0 : generate.active };
}

// Estimate the server's single-image Final edit. No request to a provider is made here.
export function finalEstimateSpec(entry, choice, config) {
  const model = findModel(config, entryModel(entry));
  return { ...entry.params, ...choice, model: entryModel(entry), operation: "edit", n: 1,
    quality: choice.quality || model?.options?.quality?.at(-1) || "auto",
    image: entry.outputs?.[0] || entry.output, prompt: entry.prompt || "", refs: [],
  };
}

export function generateStatus(state, config, now = Date.now()) {
  const g = state.generate, batchId = g.batch?.batch_id;
  const jobs = Object.values(state.jobs || {}).filter((j) => batchId && j.batch === batchId);
  const failed = jobs.find((j) => j.error);
  const error = g.error || state.writer.error || failed?.error;
  if (error) {
    const models = error.models || [failed?.model || g.models[0]];
    const providers = [...new Set(models.map((model) => (config?.cloud || []).find((group) => group.models?.some((m) => m.id === model))?.provider).filter(Boolean))];
    const provider = error === state.writer.error ? state.writer.provider : error.provider || providers.join(", ") || state.writer.provider;
    return { kind: "error", text: `${provider}: ${error.message}. Your idea is kept.` };
  }
  const active = jobs.find((j) => ACTIVE.has(j.state) || j.state === "queued");
  if (active || g.submitting) {
    const n = (g.batch?.count || g.params.count) * (g.batch?.models?.length || g.models.length);
    return { kind: "busy", text: `Generating ${n} images · ${elapsedLabel(active || {}, now) || "0:00"}` };
  }
  if (state.writer.busy) return { kind: "busy", text: `Writing… ${state.writer.provider}` };
  return { kind: "empty", text: "Type an idea to start" };
}

export async function starResult(store, client, entry) {
  const patch = { star: !entry.star };
  await client.patchHistory(store.get().project, entry.id, patch);
  const s = store.get();
  store.set({ history: s.history.map((e) => e.id === entry.id ? { ...e, ...patch } : e),
    jobs: Object.fromEntries(Object.entries(s.jobs).map(([id, job]) => [id,
      job.entry?.id === entry.id ? { ...job, entry: { ...job.entry, ...patch } } : job])) });
}

export function mountGenerateResults(root, store, client, config, refine) {
  const mode = el("div", { class: "ld-g-segment" }), grid = el("div", { class: "ld-g-result-grid" });
  const status = el("div", { class: "ld-g-state", role: "status" });
  const actions = el("div", { class: "ld-g-actions" }), menu = el("div", { class: "ld-g-final-menu", hidden: true });
  const feedback = el("input", { type: "text", "aria-label": "Refine feedback" });
  const refineButton = el("button", { type: "button", class: "ld-btn ld-g-primary", text: "Refine" });
  const panel = el("section", { class: "ld-g-card ld-g-results" }, [
    el("div", { class: "ld-g-heading" }, [el("h3", { text: "Results" }), el("span", { class: "ld-spacer" }), "prompts:", mode]),
    status, grid, actions, menu,
    el("label", { class: "ld-g-refine" }, ["Refine", feedback, refineButton]),
  ]);
  root.append(panel);
  let alive = true, menuSeq = 0, gridSignature = "", actionSignature = "", currentEntry = null, busy = false;
  const patch = (part) => store.set({ generate: { ...store.get().generate, ...part } });
  const fail = (e) => patch({ error: { message: e.message || String(e), models: [entryModel(currentEntry || {})] } });
  for (const value of ["same", "varied"]) mode.append(el("button", { type: "button", class: "ld-btn", text: value,
    onclick: () => store.set({ generate: variantsModePatch(store.get().generate, value) }) }));
  feedback.addEventListener("input", () => patch({ refineText: feedback.value }));
  refineButton.addEventListener("click", async () => {
    if (!currentEntry || refineButton.disabled) return;
    busy = true; render();
    try { await refine(currentEntry, store.get().generate.refineText); }
    catch (e) { fail(e); }
    finally { busy = false; if (alive) render(); }
  });
  async function finalMenu(entry) {
    const seq = ++menuSeq; menu.hidden = false;
    const options = finalMenuEntries(config, entry, store.get().generate.params.resolution);
    menu.replaceChildren();
    if (!options.length) menu.append(el("span", { class: "ld-muted", text: "No Final settings offered" }));
    for (const option of options) {
      const button = el("button", { type: "button", class: "ld-btn", text: `${option.label} · estimating…`, disabled: true });
      menu.append(button);
      button.addEventListener("click", async () => {
        if (button.disabled) return;
        button.disabled = true;
        try { await submitFinal(store, client, entry, option.choice); menu.hidden = true; }
        catch (e) { fail(e); }
        finally { if (alive) button.disabled = false; }
      });
      try {
        const estimate = await client.estimate({ spec: finalEstimateSpec(entry, option.choice, config) });
        if (alive && seq === menuSeq) {
          button.textContent = `${option.label} · ${estimate.unknown?.length || !Number.isFinite(estimate.total) ? "after run" : `$${estimate.total.toFixed(2)}`}`;
          button.disabled = false;
        }
      } catch (_) { if (alive && seq === menuSeq) { button.textContent = `${option.label} · estimate unavailable`; button.disabled = false; } }
    }
  }
  function render() {
    if (!alive) return;
    const s = store.get(), g = s.generate, entries = resultEntries(s), batch = g.batch;
    currentEntry = entries.find((e) => e.id === g.selected) || null;
    const rows = groupByModel(entries, batch?.batch_id, batch?.models || []);
    const nextGrid = JSON.stringify([rows, g.selected]);
    if (nextGrid !== gridSignature) {
      gridSignature = nextGrid; grid.replaceChildren();
      for (const row of rows) {
        const tiles = el("div", { class: "ld-g-tiles" });
        for (const entry of row.tiles) {
          const ref = entry.outputs?.[0] || entry.output;
          const tile = el("button", { type: "button", class: "ld-g-tile", "aria-pressed": entry.id === g.selected,
            "aria-label": `${row.model} v${entry.variant}${entry.tier === "final" ? " Final" : ""}`,
            onclick: () => patch({ selected: entry.id }) }, [
            el("img", { src: client.viewUrl(ref), alt: `${row.model} v${entry.variant}` }),
            el("span", { text: `v${entry.variant}${entry.tier === "final" ? " · Final" : ""}${entry.cancelled || entry.status === "cancelled" ? " · cancelled" : ""}` }),
          ]);
          tiles.append(tile);
        }
        grid.append(el("div", { class: "ld-g-result-row" }, [el("span", { class: "ld-g-model-label", text: row.model }), tiles]));
      }
    }
    const nextActions = JSON.stringify([currentEntry?.id, currentEntry?.star, !!store.boundNode]);
    if (nextActions !== actionSignature) {
      actionSignature = nextActions; ++menuSeq; menu.hidden = true; actions.replaceChildren();
      if (currentEntry) {
        const entry = currentEntry;
        const action = (text, onclick, attrs = {}) => el("button", { type: "button", class: "ld-btn", text, onclick, ...attrs });
        actions.append(action("Edit from here", () => store.set(editFromHere(entry, store.get()))),
          action("Refine", () => feedback.focus()), action("Final", () => { void finalMenu(entry); }),
          action("Use in graph", () => { try { const reason = store.act("useInGraph", entry.id); if (reason) fail(new Error(reason)); } catch (e) { fail(e); } },
            { disabled: !store.boundNode || !store.has("useInGraph"), title: store.boundNode ? "" : USE_IN_GRAPH_REASON }),
          action(entry.star ? "★" : "☆", () => { void starResult(store, client, entry).catch(fail); }, { "aria-label": "Star result", "aria-pressed": !!entry.star }));
      }
    }
    const state = generateStatus(s, config);
    status.hidden = state.kind === "empty" && rows.some((r) => r.tiles.length);
    status.className = `ld-g-state ld-g-${state.kind}`;
    status.replaceChildren(el("span", { text: state.text }), ...(state.kind === "busy" ? [el("div", { class: "ld-g-indeterminate" })] : []));
    for (const button of mode.children) { button.setAttribute("aria-pressed", String(button.textContent === g.variantsMode)); button.disabled = s.writer.busy; }
    if (document.activeElement !== feedback) feedback.value = g.refineText;
    refineButton.disabled = busy || s.writer.busy || g.submitting || !currentEntry || !g.refineText.trim();
  }
  const off = store.subscribe(render), timer = setInterval(render, 1000); render();
  return { destroy() { alive = false; ++menuSeq; off(); clearInterval(timer); panel.remove(); } };
}
