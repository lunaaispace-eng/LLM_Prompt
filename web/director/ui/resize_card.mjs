// Resize card: Asset Loader controls, A3a state {all, items}. The canvas stores a crop
// anchor on the state root; the server only reads all and items, so every request folds it in.
import { el } from "./dom.mjs";
import { findModel } from "../core/generate.mjs";

export const RESIZE_DEFAULT = {
  mode: "off", max_mp: 2.0, longest_side: 1024, scale_factor: 1.0, ratio: "",
  ratio_action: "crop", crop_anchor: "center", pad_color: "#000000", snap: 0, allow_upscale: false,
};

export const FALLBACK_RATIOS = ["", "1:1", "2:3", "3:2", "9:16", "16:9", "4:3", "3:4", "21:9"];

const SIZE_MODES = [["off", "Off"], ["max_mp", "Max MP"], ["longest_side", "Longest"], ["scale_factor", "Scale ×"]];
const SNAPS = [0, 8, 16, 32, 64];
const KEYS = Object.keys(RESIZE_DEFAULT);

export function sameRef(a, b) {
  return !!a && !!b && a.name === b.name
    && (a.subfolder || "") === (b.subfolder || "")
    && (a.type || "input") === (b.type || "input");
}

export function assetIndex(assets, asset) {
  if (!Array.isArray(assets) || !asset) return -1;
  return assets.findIndex((item) => sameRef(item, asset));
}

function anchorOf(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const x = Number(value.x);
  const y = Number(value.y);
  return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : null;
}

function copyItems(items, count) {
  const src = Array.isArray(items) ? items : [];
  const out = src.map((item) => (item && typeof item === "object" ? { ...item } : {}));
  while (out.length < count) out.push({});
  return out;
}

// Payload is {all, items} only. Top-level crop_anchor is folded into items[index], or into all.
export function buildResizeState(resizeState, { index = 0, count = 0, applyAll = false } = {}) {
  const src = resizeState && typeof resizeState === "object" ? resizeState : {};
  const anchor = anchorOf(src.crop_anchor);
  const allIn = src.all && typeof src.all === "object" ? { ...src.all } : null;
  let items = copyItems(src.items, Math.max(count, index + 1, 0));
  if (applyAll) {
    items = items.map((item) => {
      const copy = { ...item };
      delete copy.crop_anchor;
      return copy;
    });
    const all = anchor ? { ...(allIn || {}), crop_anchor: anchor } : allIn;
    return { all, items };
  }
  if (anchor) items[index] = { ...(items[index] || {}), crop_anchor: anchor };
  return { all: allIn, items };
}

export function resizeRequest(resizeState, { index = 0, applyAll = false, assets = [] } = {}) {
  const list = Array.isArray(assets) ? assets : [];
  const folded = buildResizeState(resizeState, { index, count: list.length, applyAll });
  if (applyAll) return { refs: list.slice(), state: { all: folded.all, items: [] } };
  const at = list.length ? Math.min(Math.max(index, 0), list.length - 1) : 0;
  return { refs: list[at] ? [list[at]] : [], state: { all: folded.all, items: [folded.items[at] || {}] } };
}

function pick(base, over) {
  const out = { ...RESIZE_DEFAULT };
  for (const key of KEYS) {
    if (base && base[key] !== undefined) out[key] = base[key];
    if (over && over[key] !== undefined) out[key] = over[key];
  }
  return out;
}

export function effectiveItem(resizeState, index = 0, applyAll = false) {
  const src = resizeState && typeof resizeState === "object" ? resizeState : {};
  const all = src.all && typeof src.all === "object" ? src.all : null;
  const items = Array.isArray(src.items) ? src.items : [];
  const item = applyAll ? pick(null, all) : pick(all, items[index]);
  const anchor = anchorOf(src.crop_anchor);
  if (anchor) item.crop_anchor = anchor;
  return item;
}

export function patchResize(resizeState, patch, { index = 0, applyAll = false } = {}) {
  const src = resizeState && typeof resizeState === "object" ? resizeState : {};
  const next = { ...src };
  if (applyAll) {
    next.all = { ...(src.all && typeof src.all === "object" ? src.all : {}), ...patch };
    return next;
  }
  const items = copyItems(src.items, index + 1);
  items[index] = { ...items[index], ...patch };
  next.items = items;
  return next;
}

// Turning Apply to all on copies the current asset's effective item into `all`.
export function withApplyAll(resizeState, index, on) {
  if (!on) return { ...(resizeState || {}) };
  return { ...(resizeState || {}), all: { ...effectiveItem(resizeState, index, false) } };
}

export function resetResize(resizeState, { index = 0, applyAll = false } = {}) {
  const next = patchResize(resizeState, { ...RESIZE_DEFAULT }, { index, applyAll });
  delete next.crop_anchor;
  return next;
}

export function readoutText(plan) {
  const inn = plan?.in;
  const out = plan?.out;
  if (!Array.isArray(inn) || !Array.isArray(out) || inn.length < 2 || out.length < 2) return "";
  return `${inn[0]}\u00d7${inn[1]} \u2192 ${out[0]}\u00d7${out[1]}`;
}

export function ratiosFor(config, modelId) {
  const list = findModel(config, modelId)?.ratio_presets;
  return Array.isArray(list) && list.length ? list.slice() : FALLBACK_RATIOS.slice();
}

export function ratioLabel(ratio) {
  return ratio ? String(ratio) : "Source";
}

export function chosenModel(state) {
  return state?.engine?.model || "";
}

export function planFromResponse(response, { applyAll, index }) {
  const items = response?.items;
  if (!Array.isArray(items)) return null;
  return items[applyAll ? index : 0] || null;
}

export function applyOne(assets, index, result) {
  const list = Array.isArray(assets) ? assets.slice() : [];
  const current = list[index] || null;
  const ref = result?.ref;
  if (!result?.changed || !ref || sameRef(current, ref)) return { assets: list, index, asset: current };
  const existing = list.findIndex((item) => sameRef(item, ref));
  if (existing >= 0) return { assets: list, index: existing, asset: list[existing] };
  const at = Math.min(Math.max(index, -1) + 1, list.length);
  list.splice(at, 0, ref);
  return { assets: list, index: at, asset: ref };
}

export function applyMany(assets, results) {
  let list = Array.isArray(assets) ? assets.slice() : [];
  const indexes = [];
  let shift = 0;
  for (let i = 0; i < (results || []).length; i++) {
    const before = list.length;
    const step = applyOne(list, i + shift, results[i]);
    list = step.assets;
    if (list.length !== before) shift += list.length - before;
    indexes.push(step.index);
  }
  return { assets: list, indexes };
}

function field(label, control) {
  return el("label", { class: "ld-field" }, [label, control]);
}

export function mountResize(host, store, client) {
  const mode = el("select", { "aria-label": "Size" }, SIZE_MODES.map(([value, text]) => el("option", { value, text })));
  const amount = el("input", { type: "number", "aria-label": "Size amount", min: "0.01", step: "1" });
  const upscale = el("input", { type: "checkbox", "aria-label": "Upscale" });
  const ratio = el("select", { "aria-label": "Ratio" });
  const action = el("select", { "aria-label": "Ratio action" }, [
    el("option", { value: "crop", text: "Crop to fill" }),
    el("option", { value: "pad", text: "Pad" }),
  ]);
  const pad = el("input", { type: "color", "aria-label": "Pad colour", value: "#000000" });
  const snap = el("select", { "aria-label": "Snap" }, SNAPS.map((n) => el("option", { value: String(n), text: n ? String(n) : "Off" })));
  const applyAll = el("input", { type: "checkbox", "aria-label": "Apply to all" });
  const readout = el("p", { class: "ld-muted", text: "—" });
  const fault = el("p", { class: "ld-bad", hidden: true });
  const applyBtn = el("button", { type: "button", class: "ld-btn", text: "Apply" });
  const resetBtn = el("button", { type: "button", class: "ld-btn", text: "Reset" });
  const root = el("div", { class: "ld-resize" }, [
    field("Size", mode), field("Amount", amount), field("Upscale", upscale),
    field("Ratio", ratio), field("Fit", action), field("Pad colour", pad), field("Snap", snap),
    field("Apply to all", applyAll),
    el("div", { class: "ld-actions" }, [resetBtn, applyBtn]),
    readout, fault,
  ]);

  let config = null;
  let timer = 0;
  let seq = 0;
  let sig = "";
  let formSig = "";
  let filling = false;

  function current() {
    const state = store.get();
    const assets = Array.isArray(state.assets) ? state.assets : [];
    const index = assetIndex(assets, state.asset);
    return { state, assets, index, applyAll: !!state.resize?.applyAll, resizeState: state.resize?.state || {} };
  }

  function fillRatios(selected) {
    const ratios = ratiosFor(config, chosenModel(store.get()));
    const keep = selected ?? ratio.value;
    ratio.replaceChildren(...ratios.map((value) => el("option", { value, text: ratioLabel(value) })));
    if (![...ratio.options].some((opt) => opt.value === keep)) {
      ratio.append(el("option", { value: keep, text: ratioLabel(keep) }));
    }
    ratio.value = keep;
  }

  function fillForm() {
    const cur = current();
    const item = effectiveItem(cur.resizeState, Math.max(cur.index, 0), cur.applyAll);
    filling = true;
    mode.value = SIZE_MODES.some(([id]) => id === item.mode) ? item.mode : "off";
    const key = mode.value === "max_mp" || mode.value === "scale_factor" || mode.value === "longest_side" ? mode.value : "longest_side";
    amount.value = String(item[key] ?? RESIZE_DEFAULT[key]);
    amount.disabled = mode.value === "off";
    amount.step = mode.value === "longest_side" ? "1" : "0.1";
    upscale.checked = !!item.allow_upscale;
    fillRatios(item.ratio || "");
    action.value = item.ratio_action === "pad" ? "pad" : "crop";
    pad.value = /^#[0-9a-fA-F]{6}$/.test(item.pad_color || "") ? item.pad_color : "#000000";
    snap.value = SNAPS.includes(Number(item.snap)) ? String(item.snap) : "0";
    applyAll.checked = cur.applyAll;
    filling = false;
  }

  function write(patch) {
    if (filling) return;
    const cur = current();
    const resize = store.get().resize || {};
    const next = patchResize(cur.resizeState, patch, { index: Math.max(cur.index, 0), applyAll: cur.applyAll });
    store.set({ resize: { ...resize, state: next } });
  }

  function requestOf(cur) {
    if (cur.index < 0) return null;
    const built = resizeRequest(cur.resizeState, { index: cur.index, applyAll: cur.applyAll, assets: cur.assets });
    if (!built.refs.length) return null;
    return { ...built, index: cur.index, applyAll: cur.applyAll, project: cur.state.project || "default" };
  }

  function showPlan(plan) {
    const text = readoutText(plan);
    readout.textContent = text || "—";
  }

  async function run(dry) {
    const cur = current();
    const snap = requestOf(cur);
    if (!snap) { showPlan(null); return; }
    const base = cur.assets.slice();
    const my = ++seq;
    fault.hidden = true;
    try {
      const res = await client.resize(snap.project, snap.refs, snap.state, dry);
      if (my !== seq || !root.isConnected) return;
      const plan = planFromResponse(res, snap);
      if (!dry) {
        const placed = snap.applyAll
          ? applyMany(base, res?.items || [])
          : applyOne(base, snap.index, res?.items?.[0]);
        const asset = snap.applyAll ? placed.assets[placed.indexes[snap.index]] : placed.asset;
        const fresh = store.get().resize || {};
        store.set({ assets: placed.assets, asset: asset || store.get().asset, resize: { ...fresh, plan } });
      } else {
        const fresh = store.get().resize || {};
        store.set({ resize: { ...fresh, plan } });
      }
      showPlan(plan);
    } catch (err) {
      if (my !== seq) return;
      fault.hidden = false;
      fault.textContent = err?.message || "resize failed";
    }
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(() => run(true), 200);
  }

  function sync() {
    const cur = current();
    const next = JSON.stringify({
      assets: cur.assets, asset: cur.state.asset, applyAll: cur.applyAll,
      resizeState: cur.resizeState, model: chosenModel(cur.state), project: cur.state.project,
    });
    showPlan(store.get().resize?.plan);
    const formNext = `${cur.index}|${cur.applyAll}|${chosenModel(cur.state)}`;
    if (formNext !== formSig) {
      formSig = formNext;
      if (!root.contains(document.activeElement)) fillForm();
    }
    if (next === sig) return;
    sig = next;
    schedule();
  }

  mode.addEventListener("change", () => {
    amount.disabled = mode.value === "off";
    amount.step = mode.value === "longest_side" ? "1" : "0.1";
    const cur = current();
    const item = effectiveItem(cur.resizeState, Math.max(cur.index, 0), cur.applyAll);
    if (mode.value !== "off") amount.value = String(item[mode.value] ?? RESIZE_DEFAULT[mode.value]);
    write({ mode: mode.value });
  });
  amount.addEventListener("change", () => {
    const key = mode.value;
    if (key === "off") return;
    const n = Number(amount.value);
    if (Number.isFinite(n) && n > 0) write({ [key]: key === "longest_side" ? Math.round(n) : n });
  });
  upscale.addEventListener("change", () => write({ allow_upscale: upscale.checked }));
  ratio.addEventListener("change", () => write({ ratio: ratio.value }));
  action.addEventListener("change", () => write({ ratio_action: action.value }));
  pad.addEventListener("input", () => write({ pad_color: pad.value }));
  snap.addEventListener("change", () => write({ snap: Number(snap.value) }));
  applyAll.addEventListener("change", () => {
    const cur = current();
    const resize = store.get().resize || {};
    const next = withApplyAll(cur.resizeState, Math.max(cur.index, 0), applyAll.checked);
    store.set({ resize: { ...resize, applyAll: applyAll.checked, state: next } });
    fillForm();
  });
  resetBtn.addEventListener("click", () => {
    const cur = current();
    const resize = store.get().resize || {};
    const next = resetResize(cur.resizeState, { index: Math.max(cur.index, 0), applyAll: cur.applyAll });
    store.set({ resize: { ...resize, state: next } });
    fillForm();
  });
  applyBtn.addEventListener("click", () => { clearTimeout(timer); run(false); });

  const off = store.subscribe(() => sync());
  client.config().then((cfg) => { config = cfg; fillForm(); sync(); }).catch(() => fillForm());
  fillForm();
  sync();
  host.append(root);

  return {
    destroy() {
      seq += 1;
      clearTimeout(timer);
      off();
      root.remove();
    },
  };
}
