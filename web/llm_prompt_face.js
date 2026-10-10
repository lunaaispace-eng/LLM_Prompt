// LLM Prompt (GGUF) — the node face: quality cards, a summary line, the last
// run, and the title-bar ⓘ / gear. The native widgets stay the stored state
// (saved workflows, the API) and are hidden from the face; the gear shows them.
//
// Techniques are taken from Pixaroma's AI Prompt node (MIT, (c) 2026 pixaroma,
// js/ai_prompt + js/shared) and Peter's Luna packs (luna_help.mjs,
// luna_collapse.mjs, luna_theme.mjs). The ones that matter, with the bug each
// one avoids:
//   - ONE DOM widget in both renderers, never rebuilt on a renderer flip
//     (a rebuild leaked a widget root per flip);
//   - options.canvasOnly is a live getter (!vueNodesMode): a static true gives
//     an empty node in Nodes 2.0;
//   - the flex column sits on an inner absolute layer, because ComfyUI writes
//     display:block onto the widget root after a rebuild or collapse;
//   - the face widget is LAST and never serialized: widgets_values is positional;
//   - DOM clicks call the change tracker themselves (it snapshots on mouseup,
//     which fires before a click), or the change is not saved or undoable;
//   - title-bar icons are canvas-drawn and exist only in the classic renderer
//     (Nodes 2.0 never calls onDrawForeground), so Nodes 2.0 gets DOM buttons;
//   - the last run lives in a module Map keyed by workflow + node id, not in
//     node.properties: a tab switch rebuilds every node object (plain fields
//     are lost), and the change tracker compares properties, so writing them
//     on every run marked the workflow modified and added an undo step.

import { app } from "/scripts/app.js";
// Versioned: browsers kept an old copy of this module after Ctrl+F5 (2026-10-10). Bump on every change.
import { closePanel, isPanelOpenFor, openPanel, recentlyClosed, refreshPanel } from "./llm_prompt_panel.mjs?v=6";

const NODE = "LLMPrompt";
const FACE = "llm_face";
const MIN_H = 214;
// Widgets that stay on the face. Everything else is a setting (gear).
// custom_system_prompt stays too (Peter, 2026-10-10: "still no custom system
// prompt text input in the node"); visible, its socket is labelled again.
const KEEP = new Set(["model_name", "system_prompt", "custom_system_prompt", "user_prompt", "seed",
    "control_after_generate"]);

// Mirrors QUALITY_LEVELS in llm_prompt_node.py. Times: Qwen3.6 27B, Krea preset.
const LEVELS = [
    { key: "fast",    name: "Fast",    time: "~10 s", desc: "no thinking · MTP", thinking: false, budget: -1, mtp: 3 },
    { key: "normal",  name: "Normal",  time: "~25 s", desc: "thinks up to 1k tokens · MTP", thinking: true, budget: 1024, mtp: 3 },
    { key: "quality", name: "Quality", time: "~35 s", desc: "thinks up to 2k tokens · MTP", thinking: true, budget: 2048, mtp: 3 },
    { key: "ultra",   name: "Ultra",   time: "~50 s", desc: "unlimited thinking · MTP", thinking: true, budget: -1, mtp: 3 },
];

// Luna house palette (luna_theme.mjs).
const C = {
    accent: "#e0a458", accentSoft: "#e0a45814", bg: "#14151a", panel: "#1c1e25",
    border: "#2e313c", text: "#e7e5df", muted: "#8b8e99",
};

const isVue = () => !!window.LiteGraph?.vueNodesMode;
const widget = (node, name) => node.widgets?.find((w) => w.name === name);

/* ----------------------------------------------------------------- css */

function injectCSS() {
    if (document.getElementById("llm-face-css")) return;
    const s = document.createElement("style");
    s.id = "llm-face-css";
    s.textContent = `
.llmf-root{position:relative;width:100%;height:100%;min-height:${MIN_H}px;font-family:Inter,system-ui,sans-serif;color:${C.text}}
.llmf-in{position:absolute;inset:0;display:flex;flex-direction:column;gap:6px;padding:6px 8px 8px;box-sizing:border-box}
.llmf-row{display:flex;align-items:center;gap:6px;font-size:11px;color:${C.muted}}
.llmf-row .sp{flex:1}
.llmf-btn{display:none;width:20px;height:20px;border-radius:5px;border:1px solid ${C.border};background:${C.bg};color:${C.muted};font-size:12px;line-height:18px;text-align:center;cursor:pointer;padding:0}
.llmf-btn:hover{color:${C.accent};border-color:${C.accent}}
.llmf-root.vue .llmf-btn{display:inline-block}
.llmf-cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:5px}
.llmf-card{background:${C.bg};border:1px solid ${C.border};border-radius:7px;padding:6px 2px;text-align:center;cursor:pointer;user-select:none}
.llmf-card:hover{border-color:${C.muted}}
.llmf-card b{display:block;font-size:12px;font-weight:600;color:${C.text}}
.llmf-card span{font-size:10px;color:${C.muted}}
.llmf-card.on{border-color:${C.accent};background:${C.accentSoft}}
.llmf-card.on b{color:${C.accent}}
.llmf-sum{font-size:11px;color:${C.muted};min-height:14px}
.llmf-sum.custom{color:${C.accent}}
.llmf-model{display:flex;align-items:center;gap:4px;flex-wrap:wrap;font-size:11px;color:${C.muted}}
.llmf-bg{font-size:10px;padding:1px 6px;border-radius:4px;background:${C.border};color:#c9ccd4}
.llmf-bg.a{background:${C.accent}22;color:${C.accent}}
.llmf-bg.g{background:#7bb47b22;color:#9fd09f}
.llmf-bg.off{opacity:.45}
.llmf-bg.warn{background:#c0564a33;color:#f0a49a}
.llmf-dot{width:7px;height:7px;border-radius:4px;background:${C.border};display:inline-block}
.llmf-dot.on{background:#7bb47b}
.llmf-last{font-size:11px;color:${C.muted};background:${C.bg};border:1px solid ${C.border};border-radius:6px;padding:4px 7px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.llmf-last{display:flex;gap:8px;align-items:center}
.llmf-last .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis}
.llmf-link{color:${C.accent};cursor:pointer;flex:none}
.llmf-link:hover{text-decoration:underline}
.llmf-btn.all{display:inline-block;width:auto;padding:0 6px;font-size:11px}
.llmf-pop.think{max-width:620px;max-height:60vh;overflow:auto;white-space:pre-wrap;font:12px/1.5 ui-monospace,Consolas,monospace}
.llmf-ov{position:fixed;inset:0;z-index:1500;background:#000a;display:flex;align-items:center;justify-content:center}
.llmf-ed{width:min(900px,80vw);height:min(640px,75vh);display:flex;flex-direction:column;gap:8px;background:${C.panel};border:1px solid ${C.accent}66;border-radius:10px;padding:12px;font:12px Inter,system-ui,sans-serif;color:${C.text}}
.llmf-ed textarea{flex:1;resize:none;background:${C.bg};border:1px solid ${C.border};border-radius:6px;color:${C.text};font:14px/1.6 Inter,system-ui,sans-serif;padding:10px}
.llmf-ed textarea:focus{outline:none;border-color:${C.accent}}
.llmf-ed .bar{display:flex;gap:8px;align-items:center;color:${C.muted}}
.llmf-ed .bar .sp{flex:1}
.llmf-ed button{background:${C.bg};border:1px solid ${C.border};border-radius:6px;color:${C.text};padding:4px 12px;cursor:pointer}
.llmf-ed button.ok{border-color:${C.accent};color:${C.accent}}
.llmf-pop{position:fixed;z-index:1400;max-width:340px;background:${C.panel};border:1px solid ${C.accent}66;border-radius:8px;padding:10px 12px;font:12px/1.5 Inter,system-ui,sans-serif;color:${C.text};box-shadow:0 8px 24px #0008}
.llmf-pop b{color:${C.accent}}
`;
    document.head.appendChild(s);
}

/* ------------------------------------------------- change tracker (save) */

let _ctTimer = null;
function notifyGraphChanged() {
    clearTimeout(_ctTimer);
    _ctTimer = setTimeout(() => {
        try {
            const ct = app?.extensionManager?.workflow?.activeWorkflow?.changeTracker;
            if (typeof ct?.captureCanvasState === "function") ct.captureCanvasState();
            else ct?.checkState?.();
        } catch (_) { /* no tracker: nothing to record */ }
    }, 0);
}

/* ------------------------------------------- hide / show native widgets */

function hideWidget(w) {
    if (w.__llmfHidden) return;
    // No type change ("converted-widget"): Nodes 2.0 reads that as "converted
    // to an input" and draws an empty labelled socket row for every hidden
    // widget (seen 2026-10-10). `hidden` alone hides it in both renderers.
    w.__llmfOrig = { computeSize: w.computeSize, hidden: w.hidden, optHidden: w.options?.hidden };
    w.computeSize = () => [0, -4];
    w.hidden = true;
    if (w.options) w.options.hidden = true;
    // A multiline STRING keeps its textarea in the Nodes 2.0 body unless the
    // element itself is hidden (Pixaroma shared/utils.mjs).
    const el = w.element || w.inputEl;
    if (el) el.style.display = "none";
    w.__llmfHidden = true;
}

function showWidget(w) {
    if (!w.__llmfHidden) return;
    const o = w.__llmfOrig;
    w.computeSize = o.computeSize;
    w.hidden = o.hidden;
    if (w.options) w.options.hidden = o.optHidden;
    const el = w.element || w.inputEl;
    if (el) el.style.display = "";
    w.__llmfHidden = false;
}

// Nodes 2.0 keeps node.widgets in a shallowReactive array and snapshots the
// options, so an in-place flag change is not seen; re-assigning through an
// empty array is (Pixaroma notify/index.js). Never in the classic renderer,
// where the empty step would drop the widgets.
// A hidden widget keeps its input socket, and the classic canvas draws that
// socket where the widget used to be while a wire is dragged: the hidden
// custom_system_prompt's socket sat right above the user_prompt box, looked like
// its socket, and took Peter's idea (2026-10-10). So a hidden, unwired widget
// socket is no valid target (not drawn while dragging) and onConnectInput
// refuses it. A socket that already has a wire stays as it is, so it can be seen
// (red chip on the face) and dragged off.
function isHiddenFreeSocket(node, inp) {
    if (!inp?.widget || inp.link != null) return false;
    return !!widget(node, inp.widget.name ?? inp.name)?.__llmfHidden;
}

function guardHiddenSockets(node) {
    for (const inp of node.inputs || []) {
        if (!inp?.widget) continue;
        if (isHiddenFreeSocket(node, inp)) inp.isValidTarget = () => false;
        else if (Object.prototype.hasOwnProperty.call(inp, "isValidTarget")) delete inp.isValidTarget;
    }
}

function notifyVue(node) {
    if (!isVue() || !node.widgets?.length) return;
    const snap = [...node.widgets];
    node.widgets = [];
    node.widgets = snap;
}

function applyVisibility(node, { resize = false } = {}) {
    if (!Array.isArray(node.widgets)) return;
    const showAll = !!node.properties?.llmShowSettings;
    for (const w of node.widgets) {
        if (w.name === FACE || KEEP.has(w.name)) continue;
        (showAll ? showWidget : hideWidget)(w);
    }
    guardHiddenSockets(node);
    if (resize) node.setSize([node.size[0], node.computeSize()[1]]);
    node.setDirtyCanvas?.(true, true);
    notifyVue(node);
    node.__llmfRoot?.classList.toggle("vue", isVue());
}

/* ----------------------------------------------------------------- face */

function levelOf(node) {
    return String(widget(node, "quality")?.value ?? "fast");
}

function setValue(node, name, v) {
    const w = widget(node, name);
    if (!w || w.value === v) return;
    w.value = v;
    w.callback?.(v, app.canvas, node);
    node.setDirtyCanvas?.(true, true);
    notifyGraphChanged();
}

function setLevel(node, key) {
    if (key !== "custom") {
        node.properties = node.properties || {};
        node.properties.llmLastLevel = key;
    }
    setValue(node, "quality", key);
    renderFace(node);
    refreshPanel(node);
}

// Editing a level-owned setting in the panel: first copy the level's values
// into those widgets, so switching to custom changes nothing by itself.
function toCustom(node) {
    const lv = LEVELS.find((l) => l.key === levelOf(node));
    if (lv) {
        node.properties = node.properties || {};
        node.properties.llmLastLevel = lv.key;   // what "reset" returns to
        setValue(node, "disable_thinking", !lv.thinking);
        setValue(node, "reasoning_budget", lv.budget);
        setValue(node, "mtp_draft_tokens", lv.mtp);
    }
    setLevel(node, "custom");
}

const PANEL_API = {
    C, widget, setValue, setLevel, toCustom, levelOf: (n) => levelOf(n), renderFace: (n) => renderFace(n),
    levelValue(n, name) {
        const lv = LEVELS.find((l) => l.key === levelOf(n));
        if (!lv) return undefined;
        return { disable_thinking: !lv.thinking, reasoning_budget: lv.budget, mtp_draft_tokens: lv.mtp }[name];
    },
    showOnNode: (n) => toggleSettings(n),
    onOpen: (n) => n.setDirtyCanvas?.(true, false),
    onClose: (n) => n.setDirtyCanvas?.(true, false),
};

function openSettings(node) {
    if (recentlyClosed(node)) return;   // this click was the panel's outside-close
    openPanel(node, PANEL_API);
}

const _lastRuns = new Map();   // "workflow path|node id" -> { stats, reasoning }
function runKey(node) {
    const wf = app?.extensionManager?.workflow?.activeWorkflow?.path ?? "";
    return `${wf}|${node.id}`;
}
const lastRunOf = (node) => _lastRuns.get(runKey(node));

function lastRunText(s) {
    if (!s) return "No run yet";
    const parts = [`${Number(s.seconds || 0).toFixed(1)} s`, `${s.completion_tokens || 0} tok`, `${s.tok_s || 0} tok/s`];
    if (s.thinking) parts.push(`thought ${s.reasoning_words || 0} words`);
    if (s.mtp) parts.push(`MTP ${s.mtp}`);
    if (s.finish && s.finish !== "stop") parts.push(`⚠ ${s.finish}`);
    return `Last run: ${parts.join(" · ")}`;
}

// Preset family, from the dropdown label (the YAML title).
const FAMILIES = [
    [/krea/i, "Krea"], [/ideogram/i, "Ideogram"], [/minimax|\bh3\b/i, "MiniMax H3"], [/chroma/i, "Chroma"],
    [/z[-_ ]?image/i, "Z-Image"], [/sdxl|pony|illustrious|juggernaut/i, "SDXL"], [/flux/i, "Flux"],
];
const familyOf = (title) => (FAMILIES.find(([re]) => re.test(title || "")) || [])[1] || "";

// Model facts come from /llm_prompt/model_info, cached per model name. The
// loaded / context part changes after a run, so a run refetches.
const _info = new Map();
async function fetchInfo(name, force = false) {
    if (!name) return null;
    if (!force && _info.has(name)) return _info.get(name);
    try {
        const r = await fetch(`/llm_prompt/model_info?name=${encodeURIComponent(name)}`);
        const j = await r.json();
        _info.set(name, j);
        return j;
    } catch (_) { return null; }
}
function refreshModel(node, force = false) {
    const name = widget(node, "model_name")?.value;
    fetchInfo(name, force).then(() => renderFace(node));
}

const INPUT_CHIPS = [["user_prompt", "idea"], ["style", "style"], ["context", "context"], ["width", "size"], ["image", "image"],
    ["reference_image", "reference"], ["video", "video"], ["audio", "audio"]];

function renderModel(node, root) {
    const info = _info.get(widget(node, "model_name")?.value);
    const fam = familyOf(widget(node, "system_prompt")?.value);
    const b = (t, cls = "") => `<span class="llmf-bg ${cls}">${t}</span>`;
    let m1 = "";
    if (info?.found) {
        if (info.quant) m1 += b(info.quant);
        m1 += info.mtp ? b("MTP", "g") : b("no MTP", "off");
        if (info.vision) m1 += b("vision");
        m1 += `<span style="margin-left:4px">${info.size_gb} GB</span>`;
        m1 += `<span class="sp" style="flex:1"></span><span class="llmf-dot ${info.loaded ? "on" : ""}"></span>`;
        m1 += info.loaded ? `<span>loaded · ${Math.round(info.n_ctx / 1024)}k</span>` : "<span>not loaded</span>";
    } else {
        m1 = "<span>model info…</span>";
    }
    let m2 = fam ? b(fam, "a") : "";
    for (const [inp, label] of INPUT_CHIPS) {
        const slot = node.inputs?.find((i) => i.name === inp);
        if (slot && slot.link != null) m2 += b(`${label} ✓`);
    }
    // Settings are hidden on the face but their sockets still take wires. A wire
    // on a hidden one is easy to make by accident and changes the result a lot:
    // say so (2026-10-10: an idea wired into custom_system_prompt replaced the
    // Krea preset and the output became a 95-token rewording of the idea).
    for (const inp of node.inputs || []) {
        if (inp.link == null || !inp.widget) continue;
        const w = widget(node, inp.name);
        if (!w?.__llmfHidden) continue;
        const why = inp.name === "custom_system_prompt" ? "replaces the preset!" : "set by a wire";
        m2 += `<span class="llmf-bg warn" title="${inp.name} is wired; it is hidden on the node (⚙ shows it)">⚠ ${inp.name} ${why}</span>`;
    }
    if (!m2.includes("✓") && !m2.includes("⚠")) m2 += `<span>no inputs connected</span>`;
    root.querySelector(".llmf-m1").innerHTML = m1;
    root.querySelector(".llmf-m2").innerHTML = m2;
}

function renderFace(node) {
    const root = node.__llmfRoot;
    if (!root) return;
    renderModel(node, root);
    const key = levelOf(node);
    root.querySelectorAll(".llmf-card").forEach((c) => c.classList.toggle("on", c.dataset.key === key));
    const lv = LEVELS.find((l) => l.key === key);
    const sum = root.querySelector(".llmf-sum");
    sum.classList.toggle("custom", !lv);
    sum.textContent = lv ? lv.desc : "Custom: your own thinking and MTP settings (⚙)";
    const last = lastRunOf(node);
    root.querySelector(".llmf-last .t").textContent = lastRunText(last?.stats);
    root.querySelector(".llmf-link").style.display = last?.reasoning ? "" : "none";
    root.classList.toggle("vue", isVue());
}

function buildFace(node) {
    injectCSS();
    const root = document.createElement("div");
    root.className = "llmf-root";
    const inner = document.createElement("div");
    inner.className = "llmf-in";
    inner.innerHTML = `
<div class="llmf-model llmf-m1"></div>
<div class="llmf-model llmf-m2"></div>
<div class="llmf-row"><span>Prompt quality</span><span class="sp"></span>
<button class="llmf-btn all" data-act="edit" title="Edit your idea in a large editor">✎ idea</button>
<button class="llmf-btn" data-act="help" title="Help">i</button>
<button class="llmf-btn" data-act="gear" data-llm-gear="1" title="Settings">⚙</button></div>
<div class="llmf-cards">${LEVELS.map((l) =>
        `<div class="llmf-card" data-key="${l.key}"><b>${l.name}</b><span>${l.time}</span></div>`).join("")}</div>
<div class="llmf-sum"></div>
<div class="llmf-last"><span class="t"></span><span class="llmf-link" data-act="think">view thinking</span></div>`;
    root.appendChild(inner);
    inner.addEventListener("pointerdown", (e) => e.stopPropagation());
    inner.addEventListener("click", (e) => {
        const card = e.target.closest(".llmf-card");
        if (card) { e.stopPropagation(); setLevel(node, card.dataset.key); return; }
        if (e.target.closest(".llmf-link")) {
            e.stopPropagation();
            const r = e.target.getBoundingClientRect();
            openThinking(node, r.left, r.bottom + 6);
            return;
        }
        const btn = e.target.closest(".llmf-btn");
        if (!btn) return;
        if (btn.dataset.act === "edit") { e.stopPropagation(); openEditor(node); return; }
        e.stopPropagation();
        const r = btn.getBoundingClientRect();
        if (btn.dataset.act === "help") openHelp(r.left, r.bottom + 6);
        else openSettings(node);
    });

    const w = node.addDOMWidget(FACE, FACE, root, { serialize: false, hideOnZoom: false, getMinHeight: () => MIN_H });
    w.serialize = false;
    w.computeLayoutSize = () => ({ minHeight: MIN_H, minWidth: 1 });
    try {
        Object.defineProperty(w.options, "canvasOnly", {
            configurable: true, enumerable: true, get: () => !window.LiteGraph?.vueNodesMode,
        });
    } catch (_) { w.options.canvasOnly = !isVue(); }
    node.__llmfRoot = root;
    renderFace(node);
}

/* ------------------------------------------------------ help + settings */

let _pop = null;
function closeHelp() {
    _pop?.remove();
    _pop = null;
    document.removeEventListener("pointerdown", onOutside, true);
    document.removeEventListener("keydown", onEsc, true);
}
function onOutside(e) { if (_pop && !_pop.contains(e.target)) closeHelp(); }
function onEsc(e) { if (e.key === "Escape") { e.stopPropagation(); closeHelp(); } }

function openPop(x, y, cls, fill) {
    closeHelp();
    injectCSS();
    _pop = document.createElement("div");
    _pop.className = `llmf-pop ${cls}`;
    fill(_pop);
    document.body.appendChild(_pop);
    const r = _pop.getBoundingClientRect();
    _pop.style.left = `${Math.max(8, Math.min(x, innerWidth - r.width - 8))}px`;
    _pop.style.top = `${Math.max(8, Math.min(y, innerHeight - r.height - 8))}px`;
    _pop.addEventListener("wheel", (e) => e.stopPropagation(), { passive: true });
    setTimeout(() => {
        document.addEventListener("pointerdown", onOutside, true);
        document.addEventListener("keydown", onEsc, true);
    }, 0);
}

// The reasoning arrives with the run (ui llm_reasoning). Kept in memory only:
// it can be long, and the `log` output already carries it.
function openThinking(node, x, y) {
    const text = lastRunOf(node)?.reasoning || "";
    openPop(x, y, "think", (el) => { el.textContent = text; });
}

function openHelp(x, y) {
    openPop(x, y, "", (el) => { el.innerHTML = HELP_HTML; });
}

const HELP_HTML = `<b>LLM Prompt (GGUF)</b><br>Turns your idea into a finished prompt with a local model.<br><br>
<b>Fast</b> writes straight away (~10 s). <b>Normal</b>, <b>Quality</b> and <b>Ultra</b> let the model think
first, up to 1k, 2k or unlimited tokens. All of them use MTP when the model has MTP heads (same text, about
twice as fast). Times are for a 27B model with the Krea preset.<br><br>
<b>⚙</b> opens every setting. Changing thinking or MTP there switches the level to <b>custom</b>;
<b>reset</b> in the panel goes back. <b>✎ idea</b> opens a large editor for your idea
(Ctrl+Enter saves, Esc cancels).`;

// Full-screen editor for the idea (user_prompt). While it is open, ComfyUI's
// own Ctrl+Z must not undo the graph: its undo handler sits on window keydown
// capture from startup, so stopping the event later does not help. The
// sanctioned switch is ComfyApp.maskeditor_is_opended (Pixaroma
// shared/graph_undo_guard.mjs); it is handed back only if it is still ours.
function openEditor(node) {
    const w = widget(node, "user_prompt");
    if (!w) return;
    injectCSS();
    const ov = document.createElement("div");
    ov.className = "llmf-ov";
    ov.innerHTML = `<div class="llmf-ed"><div class="bar"><b style="color:${C.accent}">Your idea</b><span class="sp"></span>
<span>Ctrl+Enter saves · Esc cancels</span></div><textarea spellcheck="true"></textarea>
<div class="bar"><span class="cnt"></span><span class="sp"></span><button data-act="cancel">Cancel</button>
<button class="ok" data-act="save">Save</button></div></div>`;
    const ta = ov.querySelector("textarea");
    const cnt = ov.querySelector(".cnt");
    ta.value = w.value ?? "";
    const count = () => { cnt.textContent = `${ta.value.trim() ? ta.value.trim().split(/\s+/).length : 0} words`; };
    count();
    const C0 = app.constructor;
    const prevHook = C0?.maskeditor_is_opended;
    const hook = () => true;
    if (C0) C0.maskeditor_is_opended = hook;
    const close = (save) => {
        if (save) { setValue(node, "user_prompt", ta.value); renderFace(node); }
        if (C0 && C0.maskeditor_is_opended === hook) C0.maskeditor_is_opended = prevHook;
        ov.remove();
    };
    ta.addEventListener("input", count);
    ov.addEventListener("keydown", (e) => {
        e.stopPropagation();
        if (e.key === "Escape") close(false);
        else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); close(true); }
    });
    ov.addEventListener("pointerdown", (e) => { e.stopPropagation(); if (e.target === ov) close(false); });
    ov.addEventListener("wheel", (e) => e.stopPropagation(), { passive: true });
    ov.querySelector('[data-act="save"]').addEventListener("click", () => close(true));
    ov.querySelector('[data-act="cancel"]').addEventListener("click", () => close(false));
    document.body.appendChild(ov);
    ta.focus();
}

// A saved size from before the face (every setting visible, ~1,200 px) or from
// "show all settings" leaves the node far taller than its content, and the idea
// box stretches to fill it (Peter, 2026-10-10: "after every restart the LLM node
// is so extended"). On load, a node more than 35% taller than it needs shrinks
// to fit; a node made a little taller on purpose keeps its size. Classic only:
// Nodes 2.0 keeps sizes in its own layout store.
function shrinkIfOversized(node) {
    if (isVue() || node.properties?.llmShowSettings || node.flags?.collapsed) return;
    const need = node.computeSize()[1];
    if (need > 0 && node.size[1] > need * 1.35) {
        node.setSize([node.size[0], need]);
        node.setDirtyCanvas?.(true, true);
    }
}

function toggleSettings(node) {
    node.properties = node.properties || {};
    node.properties.llmShowSettings = !node.properties.llmShowSettings;
    applyVisibility(node, { resize: true });
    notifyGraphChanged();
}

/* ------------------------------------------- title-bar icons (classic) */

const ICON_R = 7;
const ICONS = [{ act: "help", inset: 16 }, { act: "gear", inset: 36 }];

function iconCentre(node, inset) {
    const h = window.LiteGraph?.NODE_TITLE_HEIGHT ?? 30;
    return [node.size[0] - inset, -h / 2];
}

function hitIcon(node, pos) {
    if (isVue() || node.flags?.collapsed) return null;
    for (const ic of ICONS) {
        const [cx, cy] = iconCentre(node, ic.inset);
        if (Math.hypot(pos[0] - cx, pos[1] - cy) <= ICON_R + 3) return ic.act;
    }
    return null;
}

function drawIcons(node, ctx) {
    if (isVue() || node.flags?.collapsed) return;
    for (const ic of ICONS) {
        const [cx, cy] = iconCentre(node, ic.inset);
        const on = node.__llmfHover === ic.act || (ic.act === "gear" && (isPanelOpenFor(node) || node.properties?.llmShowSettings));
        ctx.save();
        ctx.strokeStyle = ctx.fillStyle = on ? C.accent : C.muted;
        ctx.lineWidth = 1.4;
        if (ic.act === "help") {
            ctx.beginPath(); ctx.arc(cx, cy, ICON_R, 0, Math.PI * 2); ctx.stroke();
            ctx.font = "bold 10px sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
            ctx.fillText("i", cx, cy + 0.5);
        } else {
            // gear: ring + 8 teeth
            ctx.beginPath(); ctx.arc(cx, cy, 4.2, 0, Math.PI * 2); ctx.stroke();
            ctx.beginPath(); ctx.arc(cx, cy, 1.6, 0, Math.PI * 2); ctx.fill();
            for (let k = 0; k < 8; k++) {
                const a = (k * Math.PI) / 4;
                ctx.beginPath();
                ctx.moveTo(cx + Math.cos(a) * 4.2, cy + Math.sin(a) * 4.2);
                ctx.lineTo(cx + Math.cos(a) * 6.6, cy + Math.sin(a) * 6.6);
                ctx.stroke();
            }
        }
        ctx.restore();
    }
}

/* ------------------------------------------------- renderer flip watch */

// Shares the window keys llm_prompt_advanced.js (and the Eclipse pack) use, so
// one property watcher on LiteGraph.vueNodesMode serves everyone.
function onVueModeChange(cb) {
    const KEY = "__comfy_vueModeCallbacks", LOCK = "__comfy_vueModeWatcherInstalled";
    if (!window[KEY]) window[KEY] = new Set();
    window[KEY].add(cb);
    if (window[LOCK]) return;
    window[LOCK] = true;
    try {
        let v = !!LiteGraph.vueNodesMode;
        Object.defineProperty(LiteGraph, "vueNodesMode", {
            get() { return v; },
            set(nv) {
                const prev = v; v = !!nv;
                if (prev !== v) for (const f of window[KEY]) { try { f(v, prev); } catch (e) { console.error(e); } }
            },
            configurable: true, enumerable: true,
        });
    } catch (_) { /* swallow */ }
}

/* ---------------------------------------------------------- extension */

app.registerExtension({
    name: "LLM_Prompt.Face",
    async setup() {
        onVueModeChange(() => {
            for (const n of app.graph?._nodes || []) {
                if (n?.type === NODE) setTimeout(() => { applyVisibility(n); renderFace(n); }, 50);
            }
        });
    },
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE || nodeType.prototype.__llmfWrapped) return;
        nodeType.prototype.__llmfWrapped = true;
        const P = nodeType.prototype;

        const onCreated = P.onNodeCreated;
        P.onNodeCreated = function () {
            const r = onCreated?.apply(this, arguments);
            this.properties = this.properties || {};
            buildFace(this);
            for (const name of ["model_name", "system_prompt"]) {
                const mw = widget(this, name);
                if (!mw) continue;
                const cb = mw.callback;
                mw.callback = (...a) => { const rr = cb?.apply(mw, a); refreshModel(this); return rr; };
            }
            const qw = widget(this, "quality");
            if (qw) {
                const cb = qw.callback;
                qw.callback = (...a) => { const rr = cb?.apply(qw, a); renderFace(this); return rr; };
            }
            // Hide the settings NOW, before any size is computed. Done only in a
            // timer, a load first sized the node for every widget visible
            // (1,264 px) and overwrote the saved size on every restart
            // (Peter, 2026-10-10: "after every restart the LLM node is so extended").
            applyVisibility(this);
            // A fresh node shrinks to its face; a loaded one keeps its saved size
            // (onConfigure clears the flag before this timer fires).
            this.__llmfFresh = true;
            setTimeout(() => refreshModel(this), 120);
            setTimeout(() => applyVisibility(this, { resize: !!this.__llmfFresh }), 100);
            return r;
        };

        const onConfigure = P.onConfigure;
        P.onConfigure = function () {
            const r = onConfigure?.apply(this, arguments);
            this.__llmfFresh = false;
            renderFace(this);
            setTimeout(() => refreshModel(this), 160);
            setTimeout(() => {
                applyVisibility(this);
                renderFace(this);
                shrinkIfOversized(this);
            }, 150);
            return r;
        };

        const onExecuted = P.onExecuted;
        P.onExecuted = function (out) {
            const r = onExecuted?.apply(this, arguments);
            const s = out?.llm_stats?.[0];
            if (s) {
                _lastRuns.set(runKey(this), { stats: s, reasoning: out?.llm_reasoning?.[0] || "" });
                renderFace(this);
                refreshModel(this, true);   // loaded / context changed
            }
            return r;
        };

        const onDraw = P.onDrawForeground;
        P.onDrawForeground = function (ctx) {
            const r = onDraw?.apply(this, arguments);
            drawIcons(this, ctx);
            return r;
        };

        const onDown = P.onMouseDown;
        P.onMouseDown = function (e, pos, canvas) {
            const act = hitIcon(this, pos);
            // LiteGraph arms the title rename on double click before asking the
            // node, and ignores onDblClick's answer: disarm it on the icons.
            if (act && canvas?.pointer) delete canvas.pointer.onDoubleClick;
            if (act === "help") { openHelp(e.clientX, e.clientY + 12); return true; }
            if (act === "gear") { openSettings(this); return true; }
            return onDown?.apply(this, arguments);
        };

        const onMove = P.onMouseMove;
        P.onMouseMove = function (e, pos) {
            const act = hitIcon(this, pos);
            if (act !== this.__llmfHover) { this.__llmfHover = act; this.setDirtyCanvas(true, false); }
            return onMove?.apply(this, arguments);
        };

        const onLeave = P.onMouseLeave;
        P.onMouseLeave = function () {
            if (this.__llmfHover) { this.__llmfHover = null; this.setDirtyCanvas(true, false); }
            return onLeave?.apply(this, arguments);
        };

        const onConn = P.onConnectionsChange;
        P.onConnectionsChange = function () {
            const r = onConn?.apply(this, arguments);
            guardHiddenSockets(this);   // a wire dragged off a hidden socket: guard it again
            renderFace(this);
            return r;
        };

        const onConnectInput = P.onConnectInput;
        P.onConnectInput = function (slot) {
            if (isHiddenFreeSocket(this, this.inputs?.[slot])) return false;
            return onConnectInput ? onConnectInput.apply(this, arguments) : true;
        };

        const onRemoved = P.onRemoved;
        P.onRemoved = function () {
            if (isPanelOpenFor(this)) closePanel();
            return onRemoved?.apply(this, arguments);
        };

        const onDbl = P.onDblClick;
        P.onDblClick = function (e, pos) {
            if (hitIcon(this, pos)) return true;
            return onDbl?.apply(this, arguments);
        };
    },
});
