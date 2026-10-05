// Writer request (pure): the /luna/director/write body, reference limits, and merging results by patch.
// A merge never touches request or prompt: the user's words stay.
import { autoOperation, composeLimit, limitFor, entryModel } from "./generate.mjs";

const SIZE_LONG = { "1K": 1024, "2K": 2048, "4K": 4096 };

const leadModel = (state) => {
  const m = state.generate.models[0];
  return typeof m === "string" ? m : m && m.id;
};

export function refLimit(state, config) {
  if (state.mode === "generate") return composeLimit(leadModel(state), config); // 0 means generate: none sent
  return limitFor(config, state.engine.model, state.engine.params.operation);
}

export function sentRefs(state, config) {
  return state.refs.slice(0, refLimit(state, config));
}

// NOMINAL aspect carrier for the writer's canvas line only (the canvas block is AR-only). These pixels are
// not the provider's real output size and must never be sent as width/height in a run or batch body.
function sizeFor(aspect, resolution) {
  const m = /^(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)$/.exec(String(aspect || ""));
  if (!m) return null;
  const [w, h] = [Number(m[1]), Number(m[2])];
  if (!(w > 0 && h > 0)) return null;
  const long = SIZE_LONG[resolution] || 1024;
  const k = long / Math.max(w, h);
  return [Math.round(w * k), Math.round(h * k)];
}

// Writer preset override per tab (null = auto); a pick on one tab is never sent from the other.
const presetOf = (state) => (state.writer.preset
  && state.writer.preset[state.mode === "generate" ? "generate" : "edit"]) || null;

// `target` (Generate only): the model the write is for when it is not the lead (Refine uses the result's).
export function buildWriteBody(state, config, target = null) {
  const w = state.writer;
  const refs = (target ? state.refs.slice(0, limitFor(config, target, "compose")) : sentRefs(state, config)).map((r) => ({ role: r.role, ref: r.ref }));
  const common = {
    refs, provider: w.provider, model: w.model, preset: presetOf(state), send: { ...w.send },
    thinking: w.thinking, negative: w.negativeOn, vision_mp: w.visionMp ?? 1.0,
    server_url: w.serverUrl || "", gguf: w.gguf || {},
  };
  if (state.mode === "generate") {
    const g = state.generate;
    return {
      ...common, canvas: null, mask: null, target_model: target || leadModel(state),
      operation: autoOperation(target || leadModel(state), refs.length, config), request: g.idea,
      mask_mode: state.engine.params.mask_mode, crop_padding: state.engine.params.crop_padding,
      size: sizeFor(g.params.aspect_ratio, g.params.resolution),
      variants: g.variantsMode === "varied" ? g.params.count : 1, sections: true,
      exact_text: g.exactText || "", feedback: "", prior_prompt: "", result: null, outpaint: null,
    };
  }
  const p = state.engine.params;
  return {
    ...common, canvas: state.asset, mask: state.mask, target_model: state.engine.model, operation: p.operation,
    request: state.request, mask_mode: p.mask_mode, crop_padding: p.crop_padding, size: null, variants: 1,
    sections: false, exact_text: "", feedback: "", prior_prompt: "", result: null,
    outpaint: p.operation === "outpaint" && Array.isArray(p.outpaint) ? p.outpaint : null, // the route grows the canvas
  };
}

// Refine (Generate tab): the chosen result goes to the writer with the previous prompt and the feedback.
export function buildRefineBody(state, config, entry, feedback) {
  const out = (Array.isArray(entry.outputs) ? entry.outputs[0] : entry.output) || null;
  const model = entryModel(entry) || leadModel(state); // Refine runs on the result's model
  return { ...buildWriteBody(state, config, model), result: out, prior_prompt: entry.prompt || "", feedback };
}

export function mergeWriterResult(state, result) {
  const w = state.writer;
  return { writer: { ...w, positive: result.positive || "", negative: w.negativeOn ? result.negative || "" : "",
    busy: false, error: null } };
}

export function mergeWriterError(state, err) {
  const error = { code: (err && err.code) || "error", message: (err && err.message) || String(err) };
  return { writer: { ...state.writer, busy: false, error } };
}

export function useAsEditPrompt(state) {
  const w = state.writer;
  return { prompt: w.positive, negative: w.negativeOn ? w.negative : "" };
}

// Fills generate.variants. A variant the user edited is never overwritten without a yes ({force: true}).
export function mergeGenerateResult(state, result, opts = {}) {
  if (!opts.force && state.generate.variants.some((v) => v.edited)) return { needsConfirm: true };
  const w = state.writer;
  const variants = (result.variants || []).map((v) => ({
    prompt: v.positive || "", negative: w.negativeOn ? v.negative || "" : "", sections: v.sections || null,
    edited: false, sectionsStale: false,
  }));
  return {
    generate: { ...state.generate, variants, active: 0 },
    writer: { ...w, busy: false, error: null },
  };
}
