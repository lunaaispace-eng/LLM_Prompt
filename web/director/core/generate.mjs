// Generate-tab models (pure): prompt assembly, automatic operation, variant edits, result grouping.
export const SECTION_ORDER = ["subject", "style", "composition", "lighting", "camera"];

export function assemblePrompt(sections) {
  const parts = [];
  for (const name of SECTION_ORDER) {
    const t = String((sections && sections[name]) ?? "").trim();
    if (!t) continue;
    parts.push(/[.!?]$/.test(t) ? t : `${t}.`);
  }
  return parts.join(" ");
}

// The config's compose limit for one model (0 when unknown). ref_limit is {operation: n}, or a bare number.
export function composeLimit(model, config) {
  const id = typeof model === "string" ? model : model && model.id;
  for (const g of (config && config.cloud) || []) {
    for (const m of g.models || []) {
      if (m.id !== id) continue;
      const l = m.ref_limit;
      if (l && typeof l === "object") return Number(l.compose) || 0;
      if (typeof l === "number") return l;
      return Number(m.max_inputs) || 0;
    }
  }
  return 0;
}

// Labels the chip only; the server decides. Compose needs a sent reference and a model that takes them.
export function autoOperation(model, sentRefCount, config) {
  return sentRefCount > 0 && composeLimit(model, config) > 0 ? "compose" : "generate";
}

export function editSection(variant, name, text) {
  const sections = { ...(variant.sections || {}), [name]: text };
  return { ...variant, sections, prompt: assemblePrompt(sections), edited: true, sectionsStale: false };
}

export function editPrompt(variant, text) {
  return { ...variant, prompt: text, edited: true, sectionsStale: true };
}

const modelId = (m) => (typeof m === "string" ? m : m && m.id);
const entryModel = (e) => e.model ?? (typeof e.engine === "string" ? e.engine : e.engine && e.engine.model);
const hasImage = (e) => (Array.isArray(e.outputs) ? e.outputs.length > 0 : Boolean(e.output));

// One row per chosen model, in that order; tiles in variant order. A cancelled entry with an image stays.
export function groupByModel(entries, batchId, models) {
  const mine = (entries || []).filter((e) => e.batch === batchId && e.status !== "error" && hasImage(e));
  return (models || []).map(modelId).map((model) => ({
    model,
    tiles: mine.filter((e) => entryModel(e) === model).sort((a, b) => (a.variant || 0) - (b.variant || 0)),
  }));
}

export function batchSummary(batch) {
  const n = (batch.models || []).length;
  const c = batch.count || 1;
  const idea = String(batch.idea || "").trim();
  return `${idea} / ${n} ${n === 1 ? "model" : "models"} · ${c} ${c === 1 ? "image" : "images"} each`;
}
