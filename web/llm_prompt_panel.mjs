// LLM Prompt (GGUF) — the gear's floating settings panel.
//
// Every control reads and writes a hidden native widget, so the workflow, the
// API and the Python side see exactly what they saw before the face existed.
// Skeleton after Pixaroma's AI Prompt settings panel (MIT, (c) 2026 pixaroma,
// js/ai_prompt/settings.mjs + js/shared/node_panel.mjs). The bugs it avoids:
//   - the ✕ sits inside the drag handle, so the drag ignores it;
//   - outside-close listens on pointerdown (LiteGraph preventDefaults canvas
//     pointerdown, so a mousedown listener never fires there) and exempts the
//     gear, or the gear's own click reopens the panel it just closed;
//   - the listeners are added in setTimeout(0), or the opening click closes it;
//   - the focused field is blurred before the panel is removed: Chrome fires
//     no change event on a removed element, so a typed value was lost;
//   - keydown stops at the panel: ComfyUI binds single letters (b = bypass).

const SECTIONS = [
    { title: "Sampling", icon: "◐", rows: [
        ["auto_settings", "Auto for this model", "toggle"],
        ["temperature", "Creativity", "slider"],
        ["top_p", "Top p", "slider"],
        ["top_k", "Top k", "slider"],
        ["min_p", "Min p", "slider"],
        ["repetition_penalty", "Repeat penalty", "slider"],
        ["presence_penalty", "Presence penalty", "slider"],
        ["frequency_penalty", "Frequency penalty", "slider"],
    ] },
    { title: "Thinking", icon: "✦", rows: [
        ["disable_thinking", "Thinking", "toggle-inverse"],
        ["reasoning_budget", "Budget", "chips", [["off", 0], ["1k", 1024], ["2k", 2048], ["4k", 4096], ["∞", -1]]],
        ["preserve_thinking", "Keep earlier thinking", "toggle"],
    ] },
    { title: "Speed and memory", icon: "⚡", rows: [
        ["mtp_draft_tokens", "MTP draft tokens", "slider"],
        ["n_ctx", "Context", "chips", [["8k", 8192], ["16k", 16384], ["32k", 32768], ["64k", 65536]]],
        ["keep_model_loaded", "Keep model loaded", "toggle"],
        ["n_gpu_layers", "GPU layers (-1 = all)", "number"],
        ["device", "Device", "combo"],
    ] },
    { title: "Output", icon: "≡", rows: [
        ["output_format", "Format", "combo-chips"],
        ["split_output", "Split positive / negative", "toggle"],
        ["max_tokens", "Max answer length", "number"],
        ["validate", "Check output", "combo-chips"],
        ["custom_system_prompt", "Own instructions (replace the preset)", "text"],
    ] },
    { title: "Vision", icon: "◉", folded: true, rows: [
        ["load_mmproj", "Load vision projector", "combo-chips"],
        ["image_min_tokens", "Image tokens min", "number"],
        ["image_max_tokens", "Image tokens max", "number"],
        ["video_fps", "Video frames per second", "slider"],
        ["vision_mp", "Downscale images to MP (0 = off)", "slider"],
    ] },
    { title: "Debug", icon: "⋯", folded: true, rows: [
        ["verbose_logging", "Detailed console log", "toggle"],
    ] },
];

// Settings a quality level decides. Editing one switches the node to custom.
const LEVEL_OWNED = new Set(["disable_thinking", "reasoning_budget", "mtp_draft_tokens"]);
// Settings auto_settings overrides for a known model family.
const AUTO_OWNED = new Set(["temperature", "top_p", "top_k", "min_p", "repetition_penalty", "presence_penalty"]);

const CSS = (C) => `
.llmp{position:fixed;z-index:1300;width:460px;max-height:780px;transform-origin:0 0;display:flex;flex-direction:column;background:${C.panel};border:1px solid ${C.accent}66;border-radius:8px;box-shadow:0 10px 30px #000a;font:13.5px Inter,system-ui,sans-serif;color:${C.text}}
.llmp-h{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid ${C.border};cursor:move;user-select:none}
.llmp-h .t{font-weight:600;font-size:15px;flex:1}
.llmp-lv{font-size:11.5px;padding:1px 7px;border-radius:4px;background:${C.accent}22;color:${C.accent}}
.llmp-x,.llmp-reset{cursor:pointer;color:${C.muted};background:none;border:0;font-size:13.5px;padding:0 2px}
.llmp-x:hover,.llmp-reset:hover{color:${C.accent}}
.llmp-b{overflow:auto;padding:2px 14px 12px}
.llmp-s{margin-top:8px}
.llmp-sh{display:flex;align-items:center;gap:6px;color:${C.accent};font-size:13px;font-weight:600;margin:10px 0 5px;cursor:pointer;user-select:none}
.llmp-sh .n{margin-left:auto;color:${C.muted};font-size:11.5px}
.llmp-s.fold .llmp-r{display:none}
.llmp-r{display:grid;grid-template-columns:158px minmax(0,1fr) 60px;align-items:center;gap:10px;margin:5px 0;min-height:26px}
.llmp-r.wide{grid-template-columns:158px minmax(0,1fr)}
.llmp-r.col{grid-template-columns:1fr}
.llmp-r .l{color:${C.text}}
.llmp-r.dim .l{color:${C.muted}}
.llmp-r input[type=range]{width:100%;accent-color:${C.accent}}
.llmp-nb{width:100%;box-sizing:border-box;background:${C.bg};border:1px solid ${C.border};border-radius:4px;color:${C.text};font-size:12.5px;text-align:center;padding:3px 0}
.llmp-nb:focus,.llmp-ta:focus{outline:none;border-color:${C.accent}}
.llmp-ch{display:flex;gap:4px;flex-wrap:wrap}
.llmp-ch span{font-size:12.5px;padding:3px 9px;border:1px solid ${C.border};border-radius:4px;color:${C.text};cursor:pointer}
.llmp-ch span:hover{border-color:${C.muted}}
.llmp-ch span.on{border-color:${C.accent};color:${C.accent};background:${C.accent}14}
.llmp-tg{width:34px;height:18px;border-radius:9px;background:${C.border};position:relative;cursor:pointer;justify-self:start}
.llmp-tg:after{content:"";position:absolute;left:2px;top:2px;width:14px;height:14px;border-radius:7px;background:${C.muted};transition:left .12s}
.llmp-tg.on{background:${C.accent}}
.llmp-tg.on:after{left:18px;background:${C.bg}}
.llmp-ta{width:100%;box-sizing:border-box;min-height:80px;resize:vertical;background:${C.bg};border:1px solid ${C.border};border-radius:4px;color:${C.text};font:13px/1.45 Inter,system-ui,sans-serif;padding:6px 8px}
.llmp-r.wired>:not(.l){opacity:.35;pointer-events:none}
.llmp-wire{font-size:11.5px;color:#f0a49a;margin-left:6px}
.llmp-note{font-size:12px;color:${C.muted};margin:2px 0 0}
.llmp-f{border-top:1px solid ${C.border};padding:8px 14px;display:flex;justify-content:space-between;color:${C.muted};font-size:12.5px}
.llmp-f span{cursor:pointer}.llmp-f span:hover{color:${C.accent}}
`;

let _panel = null;   // { el, node, cleanup }
// The classic title-bar gear is canvas-drawn: its pointerdown reaches the
// outside-close first, so the gear's own click would reopen the panel at once.
// The face asks recentlyClosed() and treats that click as the close.
let _lastClose = { node: null, t: 0 };
export function recentlyClosed(node) { return _lastClose.node === node && Date.now() - _lastClose.t < 400; }

export function isPanelOpenFor(node) { return !!_panel && _panel.node === node; }
export function refreshPanel(node) { if (isPanelOpenFor(node)) _panel.refresh(); }

export function closePanel() {
    if (!_panel) return;
    const p = _panel;
    _panel = null;
    _lastClose = { node: p.node, t: Date.now() };
    // Commit a focused number box / text area first (see header).
    if (p.el.contains(document.activeElement)) document.activeElement.blur();
    p.cleanup();
    p.el.remove();
    p.api.onClose?.(p.node);
}

function fmt(v, step) {
    if (typeof v !== "number") return String(v ?? "");
    const d = step && step < 1 ? Math.min(3, String(step).split(".")[1]?.length || 2) : 0;
    return d ? v.toFixed(d) : String(Math.round(v));
}

export function openPanel(node, api) {
    // api: { C, widget, setValue(node, name, v), renderFace, levelOf, setLevel, levels, showOnNode }
    if (isPanelOpenFor(node)) { closePanel(); return; }
    closePanel();
    const { C } = api;
    if (!document.getElementById("llmp-css")) {
        const s = document.createElement("style");
        s.id = "llmp-css";
        s.textContent = CSS(C);
        document.head.appendChild(s);
    }
    node.properties = node.properties || {};
    const folded = node.properties.llmPanelFolded || {};

    const el = document.createElement("div");
    el.className = "llmp";
    el.innerHTML = `<div class="llmp-h"><span>⚙</span><span class="t">Settings</span>
<span class="llmp-lv"></span><button class="llmp-reset" title="Back to the quality level">reset</button>
<button class="llmp-x" title="Close">✕</button></div><div class="llmp-b"></div>
<div class="llmp-f"><span data-act="shownode">Show all settings on the node</span><span data-act="close">Done</span></div>`;
    const body = el.querySelector(".llmp-b");
    const refreshers = [];

    // What the run will use: a quality level overrides the settings it owns.
    const val = (name) => {
        const lv = LEVEL_OWNED.has(name) ? api.levelValue(node, name) : undefined;
        return lv !== undefined ? lv : api.widget(node, name)?.value;
    };
    const set = (name, v) => {
        if (LEVEL_OWNED.has(name) && api.levelOf(node) !== "custom") api.toCustom(node);
        api.setValue(node, name, v);
        refreshAll();
    };

    for (const sec of SECTIONS) {
        const rows = sec.rows.filter(([name]) => api.widget(node, name));
        if (!rows.length) continue;
        const box = document.createElement("div");
        box.className = "llmp-s" + ((folded[sec.title] ?? sec.folded) ? " fold" : "");
        const head = document.createElement("div");
        head.className = "llmp-sh";
        head.innerHTML = `<span>${sec.icon}</span><span>${sec.title}</span><span class="n"></span>`;
        head.addEventListener("click", () => {
            box.classList.toggle("fold");
            folded[sec.title] = box.classList.contains("fold");
            node.properties.llmPanelFolded = folded;
        });
        box.appendChild(head);
        if (sec.title === "Sampling") {
            const note = document.createElement("div");
            note.className = "llmp-r col llmp-note";
            refreshers.push(() => {
                note.textContent = val("auto_settings")
                    ? "Auto is on: the model's official values replace the dimmed sliders."
                    : "Auto is off: these values are used as set.";
            });
            box.appendChild(note);
        }
        if (sec.title === "Thinking") {
            const note = document.createElement("div");
            note.className = "llmp-r col llmp-note";
            refreshers.push(() => {
                const lv = api.levelOf(node);
                note.textContent = lv === "custom"
                    ? "Custom: thinking and MTP are used as set here."
                    : `The ${lv} level sets thinking and MTP; changing them switches to custom.`;
            });
            box.appendChild(note);
        }
        for (const row of rows) box.appendChild(buildRow(node, row, val, set, refreshers, api));
        body.appendChild(box);
    }

    const lvEl = el.querySelector(".llmp-lv");
    const resetEl = el.querySelector(".llmp-reset");
    function refreshAll() {
        const lv = api.levelOf(node);
        lvEl.textContent = lv;
        resetEl.style.display = lv === "custom" && node.properties.llmLastLevel ? "" : "none";
        for (const f of refreshers) f();
    }
    resetEl.addEventListener("click", (e) => {
        e.stopPropagation();
        api.setLevel(node, node.properties.llmLastLevel || "fast");
        refreshAll();
    });
    el.querySelector(".llmp-x").addEventListener("click", (e) => { e.stopPropagation(); closePanel(); });
    el.querySelector(".llmp-f").addEventListener("click", (e) => {
        const act = e.target.dataset?.act;
        if (act === "close") closePanel();
        if (act === "shownode") { closePanel(); api.showOnNode(node); }
    });
    el.addEventListener("keydown", (e) => { if (e.key !== "Escape") e.stopPropagation(); });
    el.addEventListener("pointerdown", (e) => e.stopPropagation());
    el.addEventListener("wheel", (e) => e.stopPropagation(), { passive: true });

    document.body.appendChild(el);
    refreshAll();
    // Attached to the node (Peter, 2026-10-10: "it is independent, it is not
    // connected to the node and zoom in and out keeps it the same"): placed in
    // canvas units next to the node and redrawn every frame at the canvas zoom,
    // so it pans and zooms with the node. LiteGraph emits nothing on a
    // pan / zoom, hence the frame loop (Pixaroma node_panel.mjs followNode).
    const anchor = anchorFor(node);
    const stopFollow = follow(el, node, anchor);
    const stopDrag = makeDraggable(el, el.querySelector(".llmp-h"), anchor);

    const onOutside = (e) => {
        if (el.contains(e.target) || e.target.closest?.("[data-llm-gear]")) return;
        closePanel();
    };
    const onEsc = (e) => { if (e.key === "Escape") { e.stopPropagation(); closePanel(); } };
    const t = setTimeout(() => {
        document.addEventListener("pointerdown", onOutside, true);
        document.addEventListener("keydown", onEsc, true);
    }, 0);
    _panel = {
        el, node, api, refresh: refreshAll,
        cleanup() {
            clearTimeout(t);
            stopFollow();
            stopDrag();
            document.removeEventListener("pointerdown", onOutside, true);
            document.removeEventListener("keydown", onEsc, true);
        },
    };
    api.onOpen?.(node);
}

function buildRow(node, [name, label, kind, chips], val, set, refreshers, api) {
    const w = api.widget(node, name);
    const opt = w.options || {};
    const r = document.createElement("div");
    r.className = "llmp-r";
    const l = document.createElement("span");
    l.className = "l";
    l.textContent = label;
    l.title = w.tooltip || opt.tooltip || name;
    r.appendChild(l);
    const dimIfAuto = () => r.classList.toggle("dim", AUTO_OWNED.has(name) && !!val("auto_settings"));
    // A setting fed by a wire takes the wire's value; the control would mislead.
    const wireTag = document.createElement("span");
    wireTag.className = "llmp-wire";
    wireTag.textContent = "set by a wire";
    l.appendChild(wireTag);
    refreshers.push(() => {
        const inp = node.inputs?.find((i) => i.name === name);
        const wired = inp?.link != null;
        r.classList.toggle("wired", wired);
        wireTag.style.display = wired ? "" : "none";
    });

    if (kind === "slider") {
        const step = opt.step2 ?? opt.round ?? opt.step ?? 1;
        const range = document.createElement("input");
        range.type = "range";
        range.min = opt.min ?? 0; range.max = opt.max ?? 1; range.step = step;
        const nb = document.createElement("input");
        nb.className = "llmp-nb"; nb.type = "text"; nb.size = 1;
        const commit = (raw) => {
            let v = Number(raw);
            if (!Number.isFinite(v)) { nb.value = fmt(val(name), step); return; }
            v = Math.min(Number(range.max), Math.max(Number(range.min), v));
            set(name, v);
        };
        range.addEventListener("input", () => { nb.value = fmt(Number(range.value), step); });
        range.addEventListener("change", () => commit(range.value));
        nb.addEventListener("change", () => commit(nb.value));
        r.append(range, nb);
        refreshers.push(() => { const v = val(name); range.value = v; nb.value = fmt(v, step); dimIfAuto(); });
    } else if (kind === "number") {
        r.classList.add("wide");
        const nb = document.createElement("input");
        nb.className = "llmp-nb"; nb.type = "text"; nb.size = 1; nb.style.width = "90px";
        nb.addEventListener("change", () => {
            let v = Math.round(Number(nb.value));
            if (!Number.isFinite(v)) { nb.value = val(name); return; }
            if (opt.min != null) v = Math.max(opt.min, v);
            if (opt.max != null) v = Math.min(opt.max, v);
            set(name, v);
        });
        r.appendChild(nb);
        refreshers.push(() => { nb.value = val(name); });
    } else if (kind === "toggle" || kind === "toggle-inverse") {
        r.classList.add("wide");
        const tg = document.createElement("div");
        tg.className = "llmp-tg";
        const inv = kind === "toggle-inverse";
        tg.addEventListener("click", () => set(name, inv ? !!tg.classList.contains("on") : !tg.classList.contains("on")));
        r.appendChild(tg);
        refreshers.push(() => tg.classList.toggle("on", inv ? !val(name) : !!val(name)));
    } else if (kind === "chips" || kind === "combo-chips") {
        r.classList.add("wide");
        const ch = document.createElement("div");
        ch.className = "llmp-ch";
        const list = kind === "chips" ? chips : (opt.values || []).map((v) => [v, v]);
        for (const [text, v] of list) {
            const s = document.createElement("span");
            s.textContent = text;
            s.addEventListener("click", () => set(name, v));
            ch.appendChild(s);
            refreshers.push(() => s.classList.toggle("on", val(name) === v));
        }
        r.appendChild(ch);
    } else if (kind === "combo") {
        r.classList.add("wide");
        const ch = document.createElement("div");
        ch.className = "llmp-ch";
        for (const v of opt.values || []) {
            const s = document.createElement("span");
            s.textContent = v;
            s.addEventListener("click", () => set(name, v));
            ch.appendChild(s);
            refreshers.push(() => s.classList.toggle("on", val(name) === v));
        }
        r.appendChild(ch);
    } else if (kind === "text") {
        r.classList.add("col");
        const ta = document.createElement("textarea");
        ta.className = "llmp-ta";
        ta.placeholder = "Empty = use the preset";
        ta.addEventListener("change", () => set(name, ta.value));
        r.appendChild(ta);
        refreshers.push(() => { if (document.activeElement !== ta) ta.value = val(name) ?? ""; });
    }
    return r;
}

function nodeScreenRect(node) {
    const vueEl = document.querySelector(`[data-node-id="${node.id}"]`);
    if (window.LiteGraph?.vueNodesMode && vueEl) return vueEl.getBoundingClientRect();
    const c = window.app?.canvas ?? globalThis.comfyAPI?.app?.app?.canvas;
    const cr = c.canvas.getBoundingClientRect();
    const { scale, offset } = c.ds;
    const th = window.LiteGraph?.NODE_TITLE_HEIGHT ?? 30;
    return {
        left: cr.left + (node.pos[0] + offset[0]) * scale,
        top: cr.top + (node.pos[1] - th + offset[1]) * scale,
        width: node.size[0] * scale,
        height: (node.size[1] + th) * scale,
    };
}

const canvasScale = () => (window.app?.canvas ?? globalThis.comfyAPI?.app?.app?.canvas)?.ds?.scale || 1;

// Where the panel sits relative to the node, in canvas units (so it scales with
// the zoom). Remembered per node for this page session; a drag changes it.
const _anchors = new Map();
function anchorFor(node) {
    if (!_anchors.has(node.id)) _anchors.set(node.id, { dx: node.size[0] + 14, dy: 0 });
    return _anchors.get(node.id);
}

function follow(el, node, anchor) {
    let raf = 0, last = "";
    const tick = () => {
        raf = requestAnimationFrame(tick);
        if (!(node.graph ?? null)) { closePanel(); return; }   // node deleted
        const r = nodeScreenRect(node);
        const sc = canvasScale();
        const left = r.left + anchor.dx * sc, top = r.top + anchor.dy * sc;
        const key = `${left.toFixed(1)}|${top.toFixed(1)}|${sc}`;
        if (key === last) return;
        last = key;
        el.style.left = `${left}px`;
        el.style.top = `${top}px`;
        el.style.transform = `scale(${sc})`;
    };
    tick();
    return () => cancelAnimationFrame(raf);
}

function makeDraggable(el, handle, anchor) {
    let sx = 0, sy = 0, ox = 0, oy = 0, pid = null;
    const move = (e) => {
        if (pid === null) return;
        if (!(e.buttons & 1)) { up(); return; }   // missed release
        const sc = canvasScale();
        anchor.dx = ox + (e.clientX - sx) / sc;   // stays attached: only the offset to the node moves
        anchor.dy = oy + (e.clientY - sy) / sc;
    };
    const up = () => {
        if (pid === null) return;
        try { handle.releasePointerCapture(pid); } catch (_) { /* gone */ }
        pid = null;
        window.removeEventListener("pointermove", move, true);
        window.removeEventListener("pointerup", up, true);
        window.removeEventListener("pointercancel", up, true);
    };
    const down = (e) => {
        if (e.button !== 0 || e.target.closest("button")) return;   // ✕ and reset stay clickable
        e.preventDefault();
        pid = e.pointerId;
        sx = e.clientX; sy = e.clientY;
        ox = anchor.dx; oy = anchor.dy;
        try { handle.setPointerCapture(pid); } catch (_) { /* fine */ }
        window.addEventListener("pointermove", move, true);
        window.addEventListener("pointerup", up, true);
        window.addEventListener("pointercancel", up, true);
    };
    handle.addEventListener("pointerdown", down);
    return () => { up(); handle.removeEventListener("pointerdown", down); };
}
