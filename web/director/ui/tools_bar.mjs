// Region-tool registry and the canvas tool bar. Keys live here so tests need no DOM.
// B13 adds a SAM entry to buildToolRegistry; the bar renders whatever the registry lists.
import { el } from "./dom.mjs";

const SPECS = [
  { id: "brush", key: "b", icon: "brush", label: "Brush" },
  { id: "box", key: "r", icon: "box", label: "Box" },
  { id: "eraser", key: "e", icon: "eraser", label: "Eraser" },
  { id: "invert", key: "i", icon: "invert", label: "Invert" },
  { id: "outpaint", key: "o", icon: "outpaint", label: "Outpaint" },
];

const GLYPH = { brush: "✎", box: "▢", eraser: "⌫", invert: "⇄", outpaint: "⧉" };

// `actions.paint(info, value)` is shared by brush (255) and eraser (0). Other tools
// take `actions[id]`. onPointer is null until the canvas passes a handler.
export function buildToolRegistry(actions = {}) {
  const paint = actions.paint || null;
  return SPECS.map((spec) => ({
    id: spec.id,
    key: spec.key,
    icon: spec.icon,
    label: spec.label,
    onPointer: spec.id === "brush" ? (paint ? (info) => paint(info, 255) : null)
      : spec.id === "eraser" ? (paint ? (info) => paint(info, 0) : null)
      : (actions[spec.id] || null),
  }));
}

// One character, case-insensitive. X, S, and every unbound key return null.
export function toolIdForKey(key, registry = buildToolRegistry()) {
  if (typeof key !== "string" || key.length !== 1) return null;
  const hit = registry.find((tool) => tool.key === key.toLowerCase());
  return hit ? hit.id : null;
}

// Tool keys, plus undo / redo. Shift+letter is left free. Alt is ignored.
export function commandForKey(e = {}) {
  const key = String(e.key || "");
  if (e.altKey) return null;
  if (e.ctrlKey || e.metaKey) {
    const k = key.toLowerCase();
    if (k === "z") return { command: e.shiftKey ? "redo" : "undo" };
    if (k === "y" && !e.shiftKey) return { command: "redo" };
    return null;
  }
  if (e.shiftKey) return null;
  const id = toolIdForKey(key);
  return id ? { tool: id } : null;
}

function run(store, name) {
  try { store.act(name); } catch (_) { /* canvas registers these while it is mounted */ }
}

export function assetUrl(asset, client) {
  if (!asset || typeof asset !== "object") return "";
  if (typeof asset.url === "string" && asset.url) return asset.url;
  if (typeof asset.src === "string" && asset.src) return asset.src;
  if (asset.name && typeof client?.viewUrl === "function") return client.viewUrl(asset);
  return "";
}

export function assetSize(asset) {
  const w = Number(asset?.w ?? asset?.width);
  const h = Number(asset?.h ?? asset?.height);
  return w > 0 && h > 0 ? [w | 0, h | 0] : null;
}

export function assetIdentity(asset) {
  if (!asset || typeof asset !== "object") return "";
  return [asset.name, asset.subfolder, asset.type, asset.url, asset.src, asset.w, asset.width, asset.h, asset.height].join("|");
}

export function isTypingTarget(node) {
  if (!node) return false;
  if (node.isContentEditable) return true;
  const tag = node.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag !== "INPUT") return false;
  const type = String(node.type || "text").toLowerCase();
  return type !== "range" && type !== "checkbox" && type !== "button" && type !== "radio";
}

export function readMargins(state) {
  const m = state?.engine?.params?.outpaint;
  return [0, 1, 2, 3].map((i) => Math.max(0, Math.round(Number(Array.isArray(m) ? m[i] : 0) || 0)));
}

// Server crop_box is [x0, y0, x1, y1], the same corners plan_resize returns. A plan.in that
// names a different image hides the frame.
export function cropFrameOf(plan, w, h) {
  const box = plan?.crop_box;
  if (!Array.isArray(box) || box.length < 4 || !(w > 0)) return null;
  const inn = plan.in;
  if (Array.isArray(inn) && inn.length >= 2 && (Number(inn[0]) !== w || Number(inn[1]) !== h)) return null;
  const x0 = Number(box[0]);
  const y0 = Number(box[1]);
  const fw = Number(box[2]) - x0;
  const fh = Number(box[3]) - y0;
  if (!(fw > 0) || !(fh > 0)) return null;
  return { x0, y0, fw, fh, key: box.join(",") + "@" + w + "x" + h };
}

const TINT = [34, 201, 160, 110];
const EDGE = [232, 244, 236, 230];

export function maskTintRgba(mask) {
  const px = new Uint8ClampedArray(mask.w * mask.h * 4);
  const src = mask.data;
  const w = mask.w;
  for (let y = 0; y < mask.h; y++) {
    for (let x = 0; x < w; x++) {
      const i = y * w + x;
      if (!src[i]) continue;
      const edge = x === 0 || y === 0 || x === w - 1 || y === mask.h - 1
        || !src[i - 1] || !src[i + 1] || !src[i - w] || !src[i + w];
      const tint = edge ? EDGE : TINT;
      const o = i * 4;
      px[o] = tint[0];
      px[o + 1] = tint[1];
      px[o + 2] = tint[2];
      px[o + 3] = tint[3];
    }
  }
  return px;
}

// Opaque black / white, white = change, alpha 255. Empty mask -> 1×1 black.
export function maskExportRgba(mask) {
  const w = mask?.w || 1;
  const h = mask?.h || 1;
  const px = new Uint8ClampedArray(w * h * 4);
  const src = mask?.data;
  for (let i = 0; i < w * h; i++) {
    const v = src && src[i] ? 255 : 0;
    const o = i * 4;
    px[o] = v;
    px[o + 1] = v;
    px[o + 2] = v;
    px[o + 3] = 255;
  }
  return { w, h, px };
}

export function maskPngBlob(mask) {
  const { w, h, px } = maskExportRgba(mask);
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  const ctx = c.getContext("2d");
  const image = ctx.createImageData(w, h);
  image.data.set(px);
  ctx.putImageData(image, 0, 0);
  return new Promise((resolve, reject) => {
    c.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("mask png failed"))), "image/png");
  });
}

/**
 * Mount the tool column. `registry` defaults to the stage-A tools.
 * Returns `{destroy, setHistory}`.
 */
export function mountTools(root, store, registry = buildToolRegistry()) {
  const buttons = new Map();
  const column = el("div", { class: "ld-tools", role: "toolbar", "aria-label": "Region tools" });

  for (const tool of registry) {
    const btn = el("button", {
      type: "button",
      class: "ld-tool",
      "aria-label": tool.label + " (" + tool.key.toUpperCase() + ")",
      "aria-pressed": "false",
    }, [
      el("span", { class: "ld-tool-ico", "data-icon": tool.icon, "aria-hidden": "true", text: GLYPH[tool.icon] || "•" }),
      el("span", { class: "ld-tool-name", text: tool.label }),
      el("kbd", { text: tool.key.toUpperCase() }),
    ]);
    btn.addEventListener("click", () => {
      if (tool.id === "invert") {
        if (tool.onPointer) tool.onPointer({ phase: "click" });
        else run(store, "maskInvert");
        return;
      }
      store.set({ tool: tool.id });
    });
    buttons.set(tool.id, btn);
    column.append(btn);
  }

  const clearBtn = el("button", { type: "button", class: "ld-tool ld-clear", text: "Clear mask" });
  clearBtn.addEventListener("click", () => run(store, "maskClear"));

  const compareBtn = el("button", { type: "button", class: "ld-tool", "aria-pressed": "false", text: "Compare" });
  compareBtn.addEventListener("click", () => {
    const compare = store.get().compare || { on: false, a: null, b: null };
    store.set({ compare: { ...compare, on: !compare.on } });
  });

  const undoBtn = el("button", { type: "button", class: "ld-tool", text: "Undo" }, [el("kbd", { text: "Ctrl+Z" })]);
  const redoBtn = el("button", { type: "button", class: "ld-tool", text: "Redo" }, [el("kbd", { text: "Ctrl+Y" })]);
  undoBtn.addEventListener("click", () => run(store, "maskUndo"));
  redoBtn.addEventListener("click", () => run(store, "maskRedo"));

  column.append(clearBtn, el("div", { class: "ld-tool-gap" }), compareBtn, undoBtn, redoBtn);

  function paint() {
    const state = store.get();
    for (const [id, btn] of buttons) {
      btn.setAttribute("aria-pressed", state.tool === id ? "true" : "false");
    }
    compareBtn.setAttribute("aria-pressed", state.compare?.on ? "true" : "false");
  }

  const off = store.subscribe(() => paint());
  paint();
  root.append(column);

  return {
    destroy() { off(); },
    setHistory(h) {
      undoBtn.disabled = !h?.canUndo;
      redoBtn.disabled = !h?.canRedo;
    },
  };
}
