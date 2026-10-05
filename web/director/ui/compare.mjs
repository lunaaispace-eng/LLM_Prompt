// Compare pair, history-strip rules, and the before/after overlay.
import { el } from "./dom.mjs";
import { assetUrl } from "./tools_bar.mjs";
import { SECTION_ORDER } from "../core/generate.mjs";
import { ORIGINAL, chainDepth, comparePair, defaultComparePair, editFromHere } from "../core/history.mjs";

export const RUN_QUEUE_REASON = "run queue not loaded";
export const USE_IN_GRAPH_REASON =
  "open the studio from a launcher node to send a result to the graph";
export const NO_ORIGINAL_REASON = "no original picture";

const PARAM_FIELDS = [
  ["quality", "quality", "quality"], ["aspect_ratio", "aspect ratio", "aspect_ratio"],
  ["resolution", "resolution", "resolution"], ["background", "background", "background"],
  ["n", "count", "count"], ["mask_mode", "mask_mode", "mask_mode"],
  ["crop_padding", "padding", "padding"], ["feather_px", "feather", "feather"],
  ["outpaint", "outpaint", "outpaint"],
];

export function historyEntries(data) {
  if (Array.isArray(data)) return data;
  if (Array.isArray(data?.entries)) return data.entries;
  return [];
}

export function isGenerateEntry(entry) {
  return !!(entry && (entry.batch != null || entry.tier != null || entry.variant != null
    || entry.exact_text != null || entry.sections != null));
}

function byId(entries, id) {
  return (entries || []).find((e) => e && e.id === id) || null;
}

function ordered(list) {
  return list.map((e, i) => [e.ts == null ? "" : String(e.ts), i, e])
    .sort((a, b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : a[1] - b[1]).map((row) => row[2]);
}

export function versionLabel(entries, entry) {
  if (!entry) return "original";
  if (entry.tier || entry.variant != null) {
    const text = ((entry.tier ? entry.tier + " " : "") + (entry.variant == null ? "" : "v" + entry.variant)).trim();
    if (text) return text;
  }
  return "v" + (chainDepth(entries, entry.id) + 1);
}

// Preorder. Hidden rows drop out and their children keep that depth. Siblings sort oldest-first
// by ts. An Edit root with an input picture leads with "original".
export function stripRows(entries, opts = {}) {
  const includeHidden = !!opts.includeHidden;
  const list = (entries || []).filter((e) => e && e.id);
  const ids = new Set(list.map((e) => e.id));
  const kids = new Map();
  const roots = [];
  for (const e of list) {
    if (e.parent && ids.has(e.parent)) {
      if (!kids.has(e.parent)) kids.set(e.parent, []);
      kids.get(e.parent).push(e);
    } else roots.push(e);
  }
  const rows = [];
  const visit = (entry, depth) => {
    let next = depth;
    if (!entry.hidden || includeHidden) {
      const editRoot = !entry.parent && !entry.batch && !entry.tier && entry.inputs && entry.inputs[0];
      if (editRoot && !entry.hidden) {
        rows.push({
          id: ORIGINAL, depth, label: "original", synthetic: true, rootId: entry.id, entry: null,
          inputs: entry.inputs,
        });
        next = depth + 1;
      }
      rows.push({
        id: entry.id, depth: next, label: versionLabel(list, entry), synthetic: false,
        tier: entry.tier || null, parent: entry.parent || null, entry,
      });
      next += 1;
    }
    for (const child of ordered(kids.get(entry.id) || [])) visit(child, next);
  };
  for (const root of ordered(roots)) visit(root, 0);
  return rows;
}

export function showRestart(entries, id) {
  return chainDepth(entries, id) >= 2;
}

export function restartHint(depth) {
  return depth >= 2 ? depth + " chained edits — restart from the clean original?" : "";
}

function chainRoot(entries, id) {
  const seen = new Set();
  let e = byId(entries, id);
  while (e && e.parent && !seen.has(e.id)) {
    seen.add(e.id);
    const parent = byId(entries, e.parent);
    if (!parent) break;
    e = parent;
  }
  return e;
}

export function restartPatch(entries, id, state = null) {
  if (!showRestart(entries, id)) return null;
  const root = chainRoot(entries, id);
  const asset = root && !root.batch && root.inputs && root.inputs[0];
  if (!asset) return null;
  const patch = { mode: "edit", asset, mask: null, parent: null };
  if (state) {
    patch.engine = { ...state.engine, params: { ...state.engine.params, outpaint: null } };
    patch.resize = { ...state.resize, plan: null };
  }
  return patch;
}

export function editHerePatch(entry, state) {
  return editFromHere(entry, state);
}

export function rerunPatch(entry, prompt) {
  return { prompt: String(prompt ?? ""), parent: entry.id };
}

export function rerunButton(hasAction) {
  return hasAction ? { disabled: false, reason: "" } : { disabled: true, reason: RUN_QUEUE_REASON };
}

export function applyRerun(store, entry, prompt) {
  const button = rerunButton(typeof store.has === "function" && store.has("runCurrent"));
  if (button.disabled) return button;
  store.set(rerunPatch(entry, prompt));
  store.act("runCurrent");
  return { disabled: false, reason: "" };
}

export function historyPatch(input) {
  const src = input || {};
  const out = {};
  if (Object.prototype.hasOwnProperty.call(src, "star")) out.star = !!src.star;
  if (Object.prototype.hasOwnProperty.call(src, "note")) out.note = String(src.note ?? "");
  if (Object.prototype.hasOwnProperty.call(src, "hidden")) out.hidden = !!src.hidden;
  return out;
}

export function money(n) {
  const x = Number(n);
  return n == null || n === "" || !Number.isFinite(x) ? null : "$" + x.toFixed(2);
}

export function isBilledEntry(entry) {
  return !!(entry && entry.error && typeof entry.error === "object" && entry.error.code === "billed");
}

export function billedCostLabel(cost) {
  const figure = money(cost);
  return "billed — no image · " + (figure == null ? "cost unknown" : figure);
}

export function statusLabel(entry) {
  if (!entry) return "";
  if (isBilledEntry(entry)) return billedCostLabel(entry.cost_usd);
  if (entry.status !== "cancelled") return entry.status || "";
  const cost = money(entry.cost_usd);
  return cost == null ? "cancelled" : "cancelled · " + cost;
}

export function entryThumb(entry) {
  // A synthetic "original" row has no output. Its picture is the root entry's first input.
  if (entry?.synthetic) return Array.isArray(entry.inputs) && entry.inputs.length ? entry.inputs[0] : null;
  return Array.isArray(entry?.outputs) && entry.outputs.length ? entry.outputs[0] : null;
}

function shown(value) {
  if (value == null || value === "") return "—";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

// preset: null means the writer picked the target's automatic preset. No writer object stays "—".
function presetShown(entry) {
  const w = entry?.writer;
  const used = !!w && typeof w === "object"
    && !!(w.provider || w.model || Object.prototype.hasOwnProperty.call(w, "preset"));
  if (!used) return "";
  return w.preset == null || w.preset === "" ? "auto" : w.preset;
}

function negativeValue(entry) {
  const w = entry.writer || {};
  const on = !!(w.negative_on ?? w.negativeOn);
  const text = String(entry.negative ?? w.negative ?? "");
  return (on ? "on" : "off") + (text ? ": " + text : "");
}

export function detailFields(entry) {
  const e = entry || {};
  const w = e.writer || {};
  const p = e.params || {};
  const field = (id, label, value) => ({ id, label, value: shown(value) });
  const fields = [
    field("request", "request", e.request || ""),
    field("prompt", "prompt", e.prompt || ""),
    field("engine", "engine", [e.engine, e.model].filter(Boolean).join(": ")),
    field("provider", "provider", w.provider || ""),
    field("writer_model", "writer model", w.model || ""),
    field("preset", "preset", presetShown(e)),
    field("thinking", "thinking", w.thinking ? "on" : "off"),
    field("negative", "negative", negativeValue(e)),
    ...PARAM_FIELDS.map(([key, label, id]) => field(id, label, p[key])),
    field("seed", "seed", e.seed),
    field("mode", "mode", e.mode || ""),
    field("cost", "est. vs actual", "est. " + (money(e.est_cost_usd) || "—") + " · actual " + (money(e.cost_usd) || "—")),
    field("duration", "duration", e.seconds == null || e.seconds === "" || !Number.isFinite(Number(e.seconds))
      ? "—" : Number(e.seconds).toFixed(1) + " s"),
    field("status", "status", statusLabel(e)),
  ];
  if (!isGenerateEntry(e)) return fields;
  const sections = e.sections || {};
  return fields.concat([
    field("batch", "batch", e.batch || ""),
    field("variant", "variant", e.variant),
    field("tier", "tier", e.tier || ""),
    field("exact_text", "exact text", e.exact_text || ""),
    ...SECTION_ORDER.map((name) => field("section:" + name, name, sections[name] || "")),
    field("feedback", "feedback", w.feedback || ""),
    field("prompts_mode", "prompts mode", w.variants_mode || ""),
  ]);
}

// The pair on screen: explicit A and B when both are set, otherwise previous vs selected.
// null when there is no picture to put on a side (a Generate root has no original).
export function resolvePair(entries, state) {
  const compare = (state && state.compare) || {};
  const list = entries || [];
  try {
    if (compare.a && compare.b) return comparePair(list, compare.a, compare.b);
    if (!state || !state.selected) return null;
    const ids = defaultComparePair(list, state.selected);
    if (!ids) return null;
    return comparePair(list, ids[0], ids[1]);
  } catch {
    return null;
  }
}

export function outputRef(side) {
  if (!side) return null;
  if (Array.isArray(side.outputs) && side.outputs.length) return side.outputs[0];
  return side.output || null;
}

function sameSrc(img, url) {
  if (img.dataset.src === url) return;
  img.dataset.src = url;
  if (url) img.src = url;
  else img.removeAttribute("src");
}

export function mountCompare(host, store, client) {
  const before = el("img", { class: "ld-compare-img", alt: "Before", draggable: "false" });
  const after = el("img", { class: "ld-compare-img", alt: "After", draggable: "false" });
  const clip = el("div", { class: "ld-compare-clip" }, [after]);
  const handle = el("div", { class: "ld-compare-handle", "aria-hidden": "true" });
  const frame = el("div", { class: "ld-compare-frame" }, [
    el("span", { class: "ld-compare-tag", text: "Before" }),
    before, clip, handle,
    el("span", { class: "ld-compare-tag ld-compare-tag-after", text: "After" }),
  ]);
  const slider = el("input", {
    type: "range", class: "ld-compare-slider", min: "0", max: "100", value: "50",
    "aria-label": "Before after",
  });
  const empty = el("p", { class: "ld-empty", hidden: true, text: "Nothing to compare." });
  const root = el("div", { class: "ld-compare", hidden: true }, [frame, slider, empty]);
  host.append(root);

  let pct = 50;

  function place() {
    const w = frame.clientWidth;
    const h = frame.clientHeight;
    after.style.width = w + "px";
    after.style.height = h + "px";
    clip.style.width = pct + "%";
    handle.style.left = pct + "%";
  }

  function render() {
    const state = store.get();
    const on = !!state.compare?.on;
    root.hidden = !on;
    if (!on) return;
    const pair = resolvePair(state.history, state);
    const a = pair && outputRef(pair.a);
    const b = pair && outputRef(pair.b);
    const ok = !!(a && b);
    frame.hidden = !ok;
    slider.hidden = !ok;
    empty.hidden = ok;
    if (!ok) return;
    sameSrc(before, assetUrl(a, client));
    sameSrc(after, assetUrl(b, client));
    place();
  }

  slider.addEventListener("input", () => {
    pct = Number(slider.value);
    place();
  });
  const off = store.subscribe(render);
  const ro = typeof ResizeObserver === "function" ? new ResizeObserver(() => place()) : null;
  ro?.observe(frame);
  render();

  return {
    destroy() {
      off();
      ro?.disconnect();
      root.remove();
    },
  };
}
