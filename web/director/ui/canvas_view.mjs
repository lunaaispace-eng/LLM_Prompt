// Image canvas: zoom, pan, mask tools, outpaint handles, crop frame. Maths stays in core/.
import { el, ensureCss } from "./dom.mjs";
import {
  makeView, zoomAt, pan, toImage, normBox, outpaintMargins, anchorFromFrame,
} from "../core/geometry.mjs";
import { create, stampLine, fillBox, invert, clear, isEmpty } from "../core/maskops.mjs";
import { createUndo } from "../core/undo.mjs";
import {
  assetIdentity, assetSize, assetUrl, buildToolRegistry, commandForKey, cropFrameOf,
  isTypingTarget, maskPngBlob, maskTintRgba, mountTools, readMargins,
} from "./tools_bar.mjs";

const SIDE = { left: 0, top: 1, right: 2, bottom: 3 };

function px(n) { return n + "px"; }

export function mountCanvas(host, store, client) {
  ensureCss(new URL("./canvas_view.css", import.meta.url));
  const photo = el("img", { class: "ld-photo", alt: "", draggable: "false" });
  const maskCanvas = el("canvas", { class: "ld-mask", "aria-hidden": "true" });
  const boxEl = el("div", { class: "ld-box", hidden: true });
  const cropEl = el("div", { class: "ld-crop", "data-crop": "frame" });
  const outEl = el("div", { class: "ld-outpaint", hidden: true });
  const handles = {};
  for (const side of Object.keys(SIDE)) {
    outEl.append(handles[side] = el("button", {
      type: "button", class: "ld-handle", "data-side": side, "aria-label": side + " outpaint handle",
    }));
  }
  const world = el("div", { class: "ld-world" }, [photo, maskCanvas, boxEl, cropEl, outEl]);
  const ring = el("div", { class: "ld-ring", hidden: true });
  const hint = el("p", { class: "ld-pan-hint", text: "Space + drag to pan" });
  const empty = el("p", { class: "ld-empty", text: "Drop an image or pick one from Assets" });
  const stage = el("div", { class: "ld-stage", tabindex: "0", "aria-label": "Image canvas" }, [world, ring, hint, empty]);
  const sizeEl = el("span", { class: "ld-readout", text: "—" });
  const zoomBtn = el("button", { type: "button", class: "ld-btn ld-zoom", text: "Fit", title: "Fit" });
  const maskBtn = el("button", { type: "button", class: "ld-btn", "aria-pressed": "true", text: "Mask" });
  const brush = el("input", { type: "range", class: "ld-brush", min: "1", max: "400", step: "1", "aria-label": "Brush size" });
  const brushRead = el("span", { class: "ld-readout", text: "Brush" });
  const marginEl = el("span", { class: "ld-readout ld-margin-read" });
  const status = el("div", { class: "ld-canvas-status" }, [sizeEl, zoomBtn, maskBtn, brushRead, brush, marginEl]);
  const toolsHost = el("div", { class: "ld-tools-host" });
  const column = el("div", { class: "ld-canvas-col" }, [stage, status]);
  const root = el("div", { class: "ld-canvas" }, [el("div", { class: "ld-canvas-main" }, [toolsHost, column])]);

  let view = { imgW: 1, imgH: 1, viewW: 1, viewH: 1, zoom: 1, panX: 0, panY: 0 };
  let mask = null, history = createUndo(), shown = "", fitted = false, maskOn = true, space = false;
  let stroke = null, box0 = null, op0 = null, pan0 = null, cropDrag = null, cropPos = null, cropKey = "";
  let live = false, raf = 0, last = null, bar = null;
  const maskCtx = maskCanvas.getContext("2d");

  const radius = () => (Number(store.get().brush) || 1) / 2;
  const snap = () => new Uint8ClampedArray(mask.data);
  const syncHist = () => bar?.setHistory({ canUndo: history.canUndo, canRedo: history.canRedo });

  function commit() {
    if (!mask) return;
    history.push(snap());
    live = false;
    syncHist();
  }
  function restore(data) {
    if (!mask || !data || data.length !== mask.data.length) return;
    mask.data.set(data);
    paintNow();
    syncHist();
  }

  function paintNow() {
    if (!mask) return;
    if (maskCanvas.width !== mask.w || maskCanvas.height !== mask.h) {
      maskCanvas.width = mask.w;
      maskCanvas.height = mask.h;
    }
    const px = maskTintRgba(mask);
    const img = maskCtx.createImageData(mask.w, mask.h);
    img.data.set(px);
    maskCtx.putImageData(img, 0, 0);
  }

  const dirty = () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; paintNow(); }); };

  function writeMargins(next) {
    const cur = readMargins(store.get());
    if (cur.every((v, i) => v === next[i])) return;
    const engine = store.get().engine;
    store.set({ engine: { ...engine, params: { ...engine.params, outpaint: next } } });
  }

  function place() {
    const state = store.get();
    const z = view.zoom || 1;
    const m = readMargins(state);
    const showOut = !!mask && (state.tool === "outpaint" || m.some((v) => v > 0));
    const frame = mask ? cropFrameOf(state.resize?.plan, mask.w, mask.h) : null;
    world.hidden = !mask;
    empty.hidden = !!mask;
    hint.hidden = !mask;
    const w = mask?.w || 0;
    const h = mask?.h || 0;
    world.style.width = photo.style.width = px(w);
    world.style.height = photo.style.height = px(h);
    world.style.transform = "translate(" + view.panX + "px," + view.panY + "px) scale(" + z + ")";
    maskCanvas.style.visibility = maskOn ? "visible" : "hidden";
    const hs = px(10 / z);
    for (const node of Object.values(handles)) {
      node.style.width = node.style.height = hs;
      node.hidden = state.tool !== "outpaint";
    }
    const put = (node, x, y, w, h) => {
      node.style.left = px(x); node.style.top = px(y); node.style.width = px(w); node.style.height = px(h);
    };
    outEl.hidden = !showOut;
    if (showOut && mask) put(outEl, -m[0], -m[1], mask.w + m[0] + m[2], mask.h + m[1] + m[3]);
    cropEl.hidden = !frame;
    if (frame) {
      const origin = cropPos && cropKey === frame.key ? cropPos : { left: frame.x0, top: frame.y0 };
      put(cropEl, origin.left, origin.top, frame.fw, frame.fh);
      cropEl.style.borderWidth = px(2 / z);
    }
    sizeEl.textContent = mask ? mask.w + " × " + mask.h : "—";
    zoomBtn.textContent = mask && fitted ? Math.round(z * 100) + "%" : "Fit";
    marginEl.textContent = showOut
      ? "left " + m[0] + " · top " + m[1] + " · right " + m[2] + " · bottom " + m[3] + " px" : "";
    stage.dataset.tool = state.tool || "";
    if (document.activeElement !== brush) brush.value = String(state.brush ?? 40);
    brushRead.textContent = "Brush " + (state.brush ?? 40);
    maskBtn.setAttribute("aria-pressed", maskOn ? "true" : "false");
    const showRing = last && mask && (state.tool === "brush" || state.tool === "eraser");
    ring.hidden = !showRing;
    if (showRing) {
      put(ring, last[0], last[1], (Number(state.brush) || 1) * z, (Number(state.brush) || 1) * z);
      ring.style.transform = "translate(-50%, -50%)";
      ring.dataset.tool = state.tool;
    }
  }

  function fit() {
    if (!mask || stage.clientWidth < 2 || stage.clientHeight < 2) return;
    view = makeView({ imgW: mask.w, imgH: mask.h, viewW: stage.clientWidth, viewH: stage.clientHeight });
    fitted = true;
    place();
  }

  function adopt(w, h) {
    if (mask && mask.w === w && mask.h === h) { place(); return; }
    mask = create(w, h);
    history = createUndo();
    history.push(snap());
    fitted = false; cropPos = null; cropKey = ""; live = false; stroke = null; box0 = null;
    paintNow(); syncHist(); fit();
  }

  function dropImage() {
    mask = null;
    history = createUndo();
    fitted = false;
    photo.removeAttribute("src");
    syncHist();
    place();
  }
  function syncAsset() {
    const asset = store.get().asset;
    const key = assetIdentity(asset);
    if (key === shown) return;
    shown = key;
    const size = assetSize(asset);
    const url = assetUrl(asset, client);
    if (!size && !url) { dropImage(); return; }
    if (size) adopt(size[0], size[1]);
    if (url) {
      photo.dataset.key = key;
      if (photo.getAttribute("src") !== url) photo.src = url;
    } else photo.removeAttribute("src");
  }

  function at(ev) {
    const r = stage.getBoundingClientRect();
    const p = [ev.clientX - r.left, ev.clientY - r.top], [x, y] = toImage(view, p[0], p[1]);
    return { p, x, y };
  }

  function onPaint(info, value) {
    if (!mask) return;
    if (info.phase === "down") {
      stroke = { x: info.x, y: info.y, value };
      stampLine(mask, info.x, info.y, info.x, info.y, radius(), value);
      live = true;
      dirty();
    } else if (info.phase === "move" && stroke && stroke.value === value) {
      stampLine(mask, stroke.x, stroke.y, info.x, info.y, radius(), value);
      stroke.x = info.x;
      stroke.y = info.y;
      live = true;
      dirty();
    } else if (info.phase === "up" && stroke && stroke.value === value) {
      stroke = null;
      commit();
      paintNow();
    }
  }

  function onBox(info) {
    if (!mask) return;
    if (info.phase === "down") box0 = { x: info.x, y: info.y };
    else if (info.phase === "move" && box0) {
      const [x, y, w, h] = normBox(box0.x, box0.y, info.x, info.y, mask.w, mask.h);
      boxEl.hidden = !(w > 0 && h > 0);
      boxEl.style.left = px(x);
      boxEl.style.top = px(y);
      boxEl.style.width = px(w);
      boxEl.style.height = px(h);
    } else if (info.phase === "up" && box0) {
      const [x, y, w, h] = normBox(box0.x, box0.y, info.x, info.y, mask.w, mask.h);
      box0 = null;
      boxEl.hidden = true;
      if (!(w > 0 && h > 0)) return;
      fillBox(mask, [x, y, x + w, y + h], 255);
      commit();
      paintNow();
    }
  }

  function onOutpaint(info) {
    if (!info.side) return;
    if (info.phase === "down") {
      op0 = { side: info.side, sx: info.sx, sy: info.sy, base: readMargins(store.get()) };
      return;
    }
    if (!op0) return;
    const got = outpaintMargins(view, { side: op0.side, dx: info.sx - op0.sx, dy: info.sy - op0.sy });
    const next = op0.base.slice();
    next[SIDE[op0.side]] = got[SIDE[op0.side]];
    writeMargins(next);
    if (info.phase === "up") op0 = null;
  }

  function dropPreview() {
    if (live) commit();
    stroke = null;
    box0 = null;
    boxEl.hidden = true;
  }
  function onInvert() {
    if (!mask) return;
    dropPreview();
    invert(mask);
    commit();
    paintNow();
  }
  function onClear() {
    if (!mask) return;
    dropPreview();
    if (isEmpty(mask)) return;
    clear(mask);
    commit();
    paintNow();
  }
  function step(dir) {
    if (box0 && !live) { box0 = null; boxEl.hidden = true; return; }
    dropPreview();
    const data = dir < 0 ? history.undo() : history.redo();
    if (data) restore(data);
  }

  const registry = buildToolRegistry({ paint: onPaint, box: onBox, outpaint: onOutpaint, invert: onInvert });

  function moveCrop(left, top, frame) {
    const slackW = mask.w - frame.fw;
    const slackH = mask.h - frame.fh;
    const l = Math.min(Math.max(0, left), Math.max(0, slackW));
    const t = Math.min(Math.max(0, top), Math.max(0, slackH));
    cropPos = { left: l, top: t };
    cropKey = frame.key;
    place();
    const anchor = anchorFromFrame(l, t, slackW, slackH);
    const resize = store.get().resize || { state: {}, plan: null };
    const prev = resize.state && typeof resize.state === "object" ? resize.state : {};
    if (prev.crop_anchor && prev.crop_anchor.x === anchor.x && prev.crop_anchor.y === anchor.y) return;
    store.set({ resize: { ...resize, state: { ...prev, crop_anchor: anchor } } });
  }

  function onPointer(ev) {
    ev.stopPropagation();
    if (!mask) return;
    const hit = at(ev);
    last = hit.p;
    const side = ev.target?.closest?.("[data-side]")?.dataset?.side;
    const onCrop = !!ev.target?.closest?.("[data-crop]");
    if (ev.type === "pointerdown") {
      if (ev.button !== 0) return;
      stage.focus({ preventScroll: true });
      stage.setPointerCapture?.(ev.pointerId);
      if (side && store.get().tool === "outpaint") onOutpaint({ phase: "down", side, sx: hit.p[0], sy: hit.p[1] });
      else if (onCrop) {
        const frame = cropFrameOf(store.get().resize?.plan, mask.w, mask.h);
        if (!frame) return;
        const origin = cropPos && cropKey === frame.key ? cropPos : { left: frame.x0, top: frame.y0 };
        cropDrag = { x: hit.x, y: hit.y, left: origin.left, top: origin.top, frame };
      } else if (space) pan0 = { x: hit.p[0], y: hit.p[1], view: { ...view } };
      else registry.find((tool) => tool.id === store.get().tool)?.onPointer?.({ phase: "down", x: hit.x, y: hit.y });
      return;
    }
    if (ev.type === "pointermove") {
      if (op0) onOutpaint({ phase: "move", side: op0.side, sx: hit.p[0], sy: hit.p[1] });
      else if (cropDrag) moveCrop(cropDrag.left + (hit.x - cropDrag.x), cropDrag.top + (hit.y - cropDrag.y), cropDrag.frame);
      else if (pan0) { view = pan(pan0.view, hit.p[0] - pan0.x, hit.p[1] - pan0.y); place(); }
      else if (stroke || box0) registry.find((tool) => tool.id === store.get().tool)?.onPointer?.({ phase: "move", x: hit.x, y: hit.y });
      place();
      return;
    }
    if (op0) onOutpaint({ phase: "up", side: op0.side, sx: hit.p[0], sy: hit.p[1] });
    if (cropDrag) {
      moveCrop(cropDrag.left + (hit.x - cropDrag.x), cropDrag.top + (hit.y - cropDrag.y), cropDrag.frame);
      cropDrag = null;
    }
    pan0 = null;
    if (stroke || box0) registry.find((tool) => tool.id === store.get().tool)?.onPointer?.({ phase: "up", x: hit.x, y: hit.y });
  }

  function onKey(ev) {
    if (!root.contains(ev.target) || isTypingTarget(ev.target)) return;
    if (ev.code === "Space") {
      ev.preventDefault(); ev.stopPropagation();
      space = true; stage.classList.add("ld-space");
      return;
    }
    const cmd = commandForKey(ev);
    if (!cmd) return;
    ev.preventDefault(); ev.stopPropagation();
    if (ev.repeat && cmd.tool) return;
    if (cmd.tool === "invert") onInvert();
    else if (cmd.tool) store.set({ tool: cmd.tool });
    else if (cmd.command) step(cmd.command === "undo" ? -1 : 1);
  }

  store.register("maskInvert", () => onInvert());
  store.register("maskClear", () => onClear());
  store.register("maskUndo", () => step(-1));
  store.register("maskRedo", () => step(1));
  bar = mountTools(toolsHost, store, registry);
  syncHist();

  photo.addEventListener("load", () => {
    if (!root.isConnected || photo.dataset.key !== shown) return;
    if (photo.naturalWidth > 0 && photo.naturalHeight > 0) adopt(photo.naturalWidth, photo.naturalHeight);
  });
  zoomBtn.addEventListener("click", () => fit());
  maskBtn.addEventListener("click", () => { maskOn = !maskOn; place(); });
  brush.addEventListener("input", () => {
    const n = Number(brush.value);
    if (n !== Number(store.get().brush)) store.set({ brush: n });
  });
  for (const type of ["pointerdown", "pointermove", "pointerup", "pointercancel"]) {
    stage.addEventListener(type, onPointer);
    root.addEventListener(type, (ev) => ev.stopPropagation());
  }
  root.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    if (!mask || ev.target.closest(".ld-tools")) return;
    const p = at(ev).p;
    view = zoomAt(view, ev.deltaY < 0 ? 1.1 : 1 / 1.1, p[0], p[1]);
    fitted = true;
    place();
  }, { passive: false });
  stage.addEventListener("pointerleave", () => { last = null; ring.hidden = true; });
  root.addEventListener("keydown", onKey, true);
  const onKeyUp = (ev) => {
    if (ev.code !== "Space") return;
    space = false;
    stage.classList.remove("ld-space");
    if (root.contains(ev.target)) ev.stopPropagation();
  };
  document.addEventListener("keyup", onKeyUp, true);

  const off = store.subscribe(() => { syncAsset(); place(); });
  const ro = new ResizeObserver(() => {
    if (!mask) return;
    if (!fitted) fit();
    else { view = { ...view, viewW: stage.clientWidth, viewH: stage.clientHeight }; place(); }
  });
  ro.observe(stage);
  host.append(root);
  syncAsset();
  place();

  return {
    destroy() {
      off();
      bar.destroy();
      ro.disconnect();
      if (raf) cancelAnimationFrame(raf);
      document.removeEventListener("keyup", onKeyUp, true);
      root.remove();
    },
    exportMaskPng: () => maskPngBlob(mask),
  };
}
