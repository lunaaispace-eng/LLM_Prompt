// LLM Prompt (API) — the node face: provider buttons with a status dot, the
// model card, connected inputs, four quality cards, the last run, and the
// title-bar ⓘ / ⚙. Built like the GGUF node's face (llm_prompt_face.js, accepted
// 2026-10-10) and reusing its settings panel (llm_prompt_panel.mjs); the rules
// that keep saved workflows safe are the same:
//   - the native widgets stay the stored state and are only hidden;
//   - the face is ONE DOM widget, LAST, never serialized (widgets_values is
//     positional, so a widget anywhere else shifts every saved value);
//   - DOM clicks call the change tracker themselves;
//   - title-bar icons are canvas-drawn in the classic renderer, DOM buttons in
//     Nodes 2.0;
//   - the last run lives in a module Map, not in node.properties.
// What the face shows comes from /llm_prompt_api/model_info, which uses the same
// mapping as the run (_quality_settings), so a card never promises a level the
// run would not send. Plan: plans/api-node-ui.md (mockup approved 2026-10-12).

import { app } from "/scripts/app.js";
import { closePanel, isPanelOpenFor, openPanel, recentlyClosed, refreshPanel } from "./llm_prompt_panel.mjs?v=7";

const NODE = "LLMPromptAPI";
const FACE = "llm_api_face";
const MIN_H = 276;
// Widgets that stay on the face; every other one is a setting (⚙).
const KEEP = new Set(["model_name", "system_prompt", "custom_system_prompt", "user_prompt", "seed",
    "control_after_generate"]);

const C = {
    accent: "#e0a458", accentSoft: "#e0a45814", bg: "#14151a", panel: "#1c1e25",
    border: "#2e313c", text: "#e7e5df", muted: "#8b8e99",
};

// Provider -> short button label and the kind of route behind it.
const PROVIDERS = {
    "Gemini": ["Gemini", "gemini"],
    "Grok (xAI)": ["Grok", "xai"],
    "OpenAI": ["OpenAI", "openai"],
    "LM Studio": ["LM Studio", "lmstudio"],
    "Claude (Max)": ["Claude Max", "cli"],
    "Codex (ChatGPT)": ["Codex", "cli"],
    "Grok (SuperGrok)": ["SuperGrok", "cli"],
    "Gemini (Antigravity)": ["Antigravity", "agy"],
    "Custom": ["Custom", "custom"],
};
const kindOf = (p) => (PROVIDERS[p] || [p, "custom"])[1];

const LEVELS = [
    { key: "fast", name: "Fast" }, { key: "normal", name: "Normal" },
    { key: "quality", name: "Quality" }, { key: "ultra", name: "Ultra" },
];

const isVue = () => !!window.LiteGraph?.vueNodesMode;
const widget = (node, name) => node.widgets?.find((w) => w.name === name);
const provOf = (node) => String(widget(node, "provider")?.value ?? "Gemini");
const modelOf = (node) => String(widget(node, "model_name")?.value ?? "");

/* ----------------------------------------------------------------- css */

function injectCSS() {
    if (document.getElementById("llma-css")) return;
    const s = document.createElement("style");
    s.id = "llma-css";
    s.textContent = `
.llma-root{position:relative;width:100%;height:100%;min-height:${MIN_H}px;font-family:Inter,system-ui,sans-serif;color:${C.text}}
.llma-in{position:absolute;inset:0;display:flex;flex-direction:column;gap:6px;padding:6px 8px 8px;box-sizing:border-box}
.llma-in>*{flex-shrink:0}
.llma-prov{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px}
.llma-pv{font-size:11px;text-align:center;padding:4px 2px;border:1px solid ${C.border};border-radius:6px;background:${C.bg};color:${C.muted};cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;user-select:none}
.llma-pv:hover{border-color:${C.muted};color:${C.text}}
.llma-pv.on{border-color:${C.accent};background:${C.accentSoft};color:${C.accent}}
.llma-pv i{font-style:normal;display:inline-block;width:6px;height:6px;border-radius:3px;margin-right:5px;vertical-align:1px;background:#555a66}
.llma-pv i.ok{background:#7bb47b}
.llma-row{display:flex;align-items:center;gap:6px;font-size:11px;color:${C.muted}}
.llma-row .sp{flex:1}
.llma-model{display:flex;align-items:center;gap:4px;flex-wrap:wrap;font-size:11px;color:${C.muted};min-height:16px}
.llma-bg{font-size:10px;padding:1px 6px;border-radius:4px;background:${C.border};color:#c9ccd4}
.llma-bg.a{background:${C.accent}22;color:${C.accent}}
.llma-bg.g{background:#7bb47b22;color:#9fd09f}
.llma-bg.b{background:#5a8bc422;color:#9cc0e8}
.llma-bg.off{opacity:.45}
.llma-bg.warn{background:#c0564a33;color:#f0a49a}
.llma-dot{width:7px;height:7px;border-radius:4px;background:${C.border};display:inline-block}
.llma-dot.on{background:#7bb47b}.llma-dot.bad{background:#c0564a}
.llma-btn{display:none;width:20px;height:20px;border-radius:5px;border:1px solid ${C.border};background:${C.bg};color:${C.muted};font-size:12px;line-height:18px;text-align:center;cursor:pointer;padding:0}
.llma-btn:hover{color:${C.accent};border-color:${C.accent}}
.llma-root.vue .llma-btn{display:inline-block}
.llma-btn.all{display:inline-block;width:auto;padding:0 6px;font-size:11px}
.llma-btn.busy{opacity:.5;pointer-events:none}
.llma-cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:5px}
.llma-card{background:${C.bg};border:1px solid ${C.border};border-radius:7px;padding:6px 2px;text-align:center;cursor:pointer;user-select:none}
.llma-card:hover{border-color:${C.muted}}
.llma-card b{display:block;font-size:12px;font-weight:600;color:${C.text}}
.llma-card span{font-size:10px;color:${C.muted};white-space:nowrap}
.llma-card.on{border-color:${C.accent};background:${C.accentSoft}}
.llma-card.on b{color:${C.accent}}
.llma-card.same,.llma-card.none{opacity:.45}
.llma-sum{font-size:11px;color:${C.muted};min-height:14px}
.llma-sum.custom{color:${C.accent}}
.llma-last{display:flex;gap:8px;align-items:center;font-size:11px;color:${C.muted};background:${C.bg};border:1px solid ${C.border};border-radius:6px;padding:4px 7px;white-space:nowrap;overflow:hidden}
.llma-last .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis}
.llma-link{color:${C.accent};cursor:pointer;flex:none}
.llma-link:hover{text-decoration:underline}
.llma-pop{position:fixed;z-index:1400;max-width:360px;background:${C.panel};border:1px solid ${C.accent}66;border-radius:8px;padding:10px 12px;font:12px/1.5 Inter,system-ui,sans-serif;color:${C.text};box-shadow:0 8px 24px #0008}
.llma-pop b{color:${C.accent}}
.llma-pop.think{max-width:620px;max-height:60vh;overflow:auto;white-space:pre-wrap;font:12px/1.5 ui-monospace,Consolas,monospace}
.llma-ov{position:fixed;inset:0;z-index:1500;background:#000a;display:flex;align-items:center;justify-content:center}
.llma-ed{width:min(900px,80vw);height:min(640px,75vh);display:flex;flex-direction:column;gap:8px;background:${C.panel};border:1px solid ${C.accent}66;border-radius:10px;padding:12px;font:12px Inter,system-ui,sans-serif;color:${C.text}}
.llma-ed textarea{flex:1;resize:none;background:${C.bg};border:1px solid ${C.border};border-radius:6px;color:${C.text};font:14px/1.6 Inter,system-ui,sans-serif;padding:10px}
.llma-ed textarea:focus{outline:none;border-color:${C.accent}}
.llma-ed .bar{display:flex;gap:8px;align-items:center;color:${C.muted}}
.llma-ed .bar .sp{flex:1}
.llma-ed button{background:${C.bg};border:1px solid ${C.border};border-radius:6px;color:${C.text};padding:4px 12px;cursor:pointer}
.llma-ed button.ok{border-color:${C.accent};color:${C.accent}}
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
    if (w.__llmaHidden) return;
    w.__llmaOrig = { computeSize: w.computeSize, hidden: w.hidden, optHidden: w.options?.hidden };
    w.computeSize = () => [0, -4];
    w.hidden = true;
    if (w.options) w.options.hidden = true;
    const el = w.element || w.inputEl;
    if (el) el.style.display = "none";
    w.__llmaHidden = true;
}

function showWidget(w) {
    if (!w.__llmaHidden) return;
    const o = w.__llmaOrig;
    w.computeSize = o.computeSize;
    w.hidden = o.hidden;
    if (w.options) w.options.hidden = o.optHidden;
    const el = w.element || w.inputEl;
    if (el) el.style.display = "";
    w.__llmaHidden = false;
}

// A hidden, unwired widget socket is no valid target (the GGUF node's lesson:
// a hidden socket sat above the idea box and took the idea's wire).
function isHiddenFreeSocket(node, inp) {
    if (!inp?.widget || inp.link != null) return false;
    return !!widget(node, inp.widget.name ?? inp.name)?.__llmaHidden;
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
        // The provider buttons and the quality cards replace these two.
        const own = w.name === "provider" || w.name === "quality";
        ((showAll && !own) ? showWidget : hideWidget)(w);
    }
    guardHiddenSockets(node);
    if (resize) node.setSize([node.size[0], node.computeSize()[1]]);
    node.setDirtyCanvas?.(true, true);
    notifyVue(node);
    node.__llmaRoot?.classList.toggle("vue", isVue());
}

/* ------------------------------------------------------- model facts */

const _info = new Map();   // "provider|model|url" -> info
const _pending = new Set();
let _lastProviders = {};   // provider -> ready, from the latest answer (dots stay lit meanwhile)
const infoKey = (node) => `${provOf(node)}|${modelOf(node)}|${widget(node, "server_url")?.value || ""}`;
const infoOf = (node) => _info.get(infoKey(node));

async function fetchInfo(node, force = false) {
    const key = infoKey(node);
    if (!force && _info.has(key)) return _info.get(key);
    if (_pending.has(key)) return null;
    _pending.add(key);
    const q = new URLSearchParams({ provider: provOf(node), model: modelOf(node),
        server_url: widget(node, "server_url")?.value || "" });
    try {
        const r = await fetch(`/llm_prompt_api/model_info?${q}`);
        const j = await r.json();
        _info.set(key, j);
        if (j?.providers) _lastProviders = j.providers;
        return j;
    } catch (_) {
        _info.set(key, { error: "no answer" });
        return null;
    } finally {
        _pending.delete(key);
    }
}
function refreshInfo(node, force = false) {
    fetchInfo(node, force).then(() => { renderFace(node); refreshPanel(node); });
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

// What a level puts into the two reasoning widgets, for "custom" to start from.
function levelValue(node, name) {
    const lv = LEVELS.find((l) => l.key === levelOf(node));
    const v = lv && infoOf(node)?.levels?.[lv.key];
    if (!v) return undefined;
    const k = kindOf(provOf(node));
    if (name === "gemini_thinking_level") return k === "gemini" ? (v === "minimal" ? "None" : v) : undefined;
    if (name === "reasoning_effort") {
        if (k === "gemini" || k === "custom") return undefined;
        return { off: "none", on: "medium" }[v] ?? v;
    }
    return undefined;
}

function toCustom(node) {
    const lv = LEVELS.find((l) => l.key === levelOf(node));
    if (lv) {
        node.properties = node.properties || {};
        node.properties.llmLastLevel = lv.key;
        for (const name of ["reasoning_effort", "gemini_thinking_level"]) {
            const v = levelValue(node, name);
            if (v !== undefined) setValue(node, name, v);
        }
    }
    setLevel(node, "custom");
}

function setProvider(node, p) {
    if (provOf(node) === p) return;
    const before = modelOf(node);
    setValue(node, "provider", p);   // its callback refreshes the model list (llm_prompt_api.js)
    renderFace(node);
    refreshPanel(node);
    // The list arrives later and sets model_name without a callback: watch for it.
    let n = 0;
    const t = setInterval(() => {
        if (modelOf(node) !== before || ++n > 20) { clearInterval(t); renderFace(node); refreshPanel(node); }
    }, 200);
}

const _lastRuns = new Map();   // "workflow path|node id" -> { stats, reasoning }
function runKey(node) {
    const wf = app?.extensionManager?.workflow?.activeWorkflow?.path ?? "";
    return `${wf}|${node.id}`;
}
const lastRunOf = (node) => _lastRuns.get(runKey(node));

function lastRunText(s) {
    if (!s) return "No run yet";
    const p = [`${Number(s.seconds || 0).toFixed(1)} s`];
    if (s.load_s) p.push(`load ${s.load_s} s`);
    if (s.tokens_out != null) p.push(s.tokens_in != null ? `in ${s.tokens_in} / out ${s.tokens_out} tok` : `${s.tokens_out} tok`);
    if (s.tok_s) p.push(`${s.tok_s} tok/s`);
    if (s.reasoning_tokens) p.push(`thought ${s.reasoning_tokens} tok`);
    if (s.served && s.model && s.served !== s.model && !String(s.served).startsWith(`${s.model}-`)) p.push(`served by ${s.served}`);
    if (s.model_state) p.push(s.model_state);
    const f = String(s.finish || "").toLowerCase();
    if (f && !["stop", "end_turn", "completed", "end"].includes(f)) p.push(`⚠ ${s.finish}`);
    return `Last run: ${p.join(" · ")}`;
}

const FAMILIES = [
    [/krea/i, "Krea"], [/ideogram/i, "Ideogram"], [/minimax|\bh3\b/i, "MiniMax H3"], [/chroma/i, "Chroma"],
    [/z[-_ ]?image/i, "Z-Image"], [/sdxl|pony|illustrious|juggernaut/i, "SDXL"], [/flux/i, "Flux"],
];
const familyOf = (title) => (FAMILIES.find(([re]) => re.test(title || "")) || [])[1] || "";

const INPUT_CHIPS = [["style", "style"], ["context", "context"], ["width", "size"], ["image", "image"],
    ["video", "video"], ["audio", "audio"]];

function renderProviders(node, root) {
    const info = infoOf(node);
    const cur = provOf(node);
    const names = widget(node, "provider")?.options?.values || Object.keys(PROVIDERS);
    const box = root.querySelector(".llma-prov");
    if (box.dataset.names !== names.join("|")) {
        box.dataset.names = names.join("|");
        box.innerHTML = names.map((p) =>
            `<div class="llma-pv" data-p="${p}" title="${p}"><i></i>${(PROVIDERS[p] || [p])[0]}</div>`).join("");
    }
    box.querySelectorAll(".llma-pv").forEach((el) => {
        el.classList.toggle("on", el.dataset.p === cur);
        el.querySelector("i").classList.toggle("ok", !!(info?.providers || _lastProviders)[el.dataset.p]);
    });
}

function renderModel(node, root) {
    const info = infoOf(node);
    const b = (t, cls = "") => `<span class="llma-bg ${cls}">${t}</span>`;
    let m1 = "";
    if (info && !info.error) {
        const lm = info.lmstudio || {};
        if (lm.quant) m1 += b(lm.quant);
        if (lm.params) m1 += b(lm.params);
        for (const [t, cls] of info.badges || []) m1 += b(t, cls);
        if (lm.size_gb) m1 += `<span style="margin-left:4px">${lm.size_gb} GB</span>`;
        m1 += `<span class="sp" style="flex:1"></span>`;
        const st = info.status || {};
        let state = st.text || "";
        if (kindOf(provOf(node)) === "lmstudio" && st.ok) {
            state += lm.loaded ? ` · loaded${lm.loaded_ctx ? ` ${Math.round(lm.loaded_ctx / 1024)}k` : ""}, kept`
                : " · not loaded: unloads after";
        }
        m1 += `<span style="white-space:nowrap"><span class="llma-dot ${st.ok ? "on" : "bad"}"></span> ${state}</span>`;
    } else {
        m1 = "<span>model info…</span>";
    }
    const hidden = info?.hidden_count ? ` ${info.hidden_count} retired models are hidden.` : "";
    m1 += `<button class="llma-btn all" data-act="refresh" title="${kindOf(provOf(node)) === "gemini"
        ? `Refresh the model list and re-check every Gemini model (about 20 tiny calls, under $0.01).${hidden}`
        : "Refresh the model list"}">↻</button>`;
    const fam = familyOf(widget(node, "system_prompt")?.value);
    let m2 = fam ? b(fam, "a") : "";
    for (const [inp, label] of INPUT_CHIPS) {
        const slot = node.inputs?.find((i) => i.name === inp);
        if (!slot || slot.link == null) continue;
        if (inp === "audio" && info && !info.audio) m2 += b("audio ✗ not with this provider", "warn");
        else m2 += b(`${label} ✓`);
    }
    for (const inp of node.inputs || []) {
        if (inp.link == null || !inp.widget) continue;
        const w = widget(node, inp.name);
        if (!w?.__llmaHidden || inp.name === "provider" || inp.name === "quality") continue;
        m2 += `<span class="llma-bg warn" title="${inp.name} is wired; it is hidden on the node (⚙ shows it)">⚠ ${inp.name} set by a wire</span>`;
    }
    if (!m2) m2 = `<span>no inputs connected</span>`;
    root.querySelector(".llma-m1").innerHTML = m1;
    root.querySelector(".llma-m2").innerHTML = m2;
}

function cardLabel(k, v) {
    if (!v) return "";
    if (k === "lmstudio") return `reasoning ${v}`;
    if (k === "gemini") return `thinking ${v}`;
    return v === "none" ? "no reasoning" : `effort ${v}`;
}

function renderFace(node) {
    const root = node.__llmaRoot;
    if (!root) return;
    // The model list sets model_name in code (no callback) after a provider
    // switch, so a missing answer for the current pair is fetched from here.
    if (!infoOf(node) && !_pending.has(infoKey(node))) refreshInfo(node);
    renderProviders(node, root);
    renderModel(node, root);
    const info = infoOf(node);
    const k = kindOf(provOf(node));
    const key = levelOf(node);
    let prev = null;
    root.querySelectorAll(".llma-card").forEach((c, i) => {
        const v = info?.levels?.[c.dataset.key] || "";
        const same = !!v && prev !== null && v === prev.v;
        c.classList.toggle("on", c.dataset.key === key);
        c.classList.toggle("none", !v && !!info);
        c.classList.toggle("same", same);
        c.querySelector("span").textContent = !info ? "…" : !v ? "no control" : same ? `= ${prev.name}` : cardLabel(k, v);
        if (!same) prev = { v, name: LEVELS[i].name };
    });
    const lv = LEVELS.find((l) => l.key === key);
    const sum = root.querySelector(".llma-sum");
    sum.classList.toggle("custom", !lv);
    const v = lv && info?.levels?.[lv.key];
    const short = (PROVIDERS[provOf(node)] || [provOf(node)])[0];
    sum.textContent = !lv ? "Custom: reasoning as set in ⚙"
        : !info ? "" : v ? `${short}: ${cardLabel(k, v)}` : `${short}: this model has no reasoning control`;
    const last = lastRunOf(node);
    root.querySelector(".llma-last .t").textContent = lastRunText(last?.stats);
    root.querySelector(".llma-link").style.display = last?.reasoning ? "" : "none";
    root.classList.toggle("vue", isVue());
}

async function refreshList(node, btn) {
    btn?.classList.add("busy");
    try {
        const q = new URLSearchParams({ provider: provOf(node), server_url: widget(node, "server_url")?.value || "",
            refresh: "1" });
        await fetch(`/llm_prompt_api/models?${q}`);
    } catch (_) { /* the list below still refreshes from what the server has */ }
    // The provider widget's callback (llm_prompt_api.js) reloads the dropdown.
    const pw = widget(node, "provider");
    pw?.callback?.(pw.value, app.canvas, node);
    await fetchInfo(node, true);
    btn?.classList.remove("busy");
    renderFace(node);
}

function buildFace(node) {
    injectCSS();
    const root = document.createElement("div");
    root.className = "llma-root";
    const inner = document.createElement("div");
    inner.className = "llma-in";
    inner.innerHTML = `
<div class="llma-prov"></div>
<div class="llma-model llma-m1"></div>
<div class="llma-model llma-m2"></div>
<div class="llma-row"><span>Prompt quality</span><span class="sp"></span>
<button class="llma-btn all" data-act="edit" title="Edit your idea in a large editor">✎ idea</button>
<button class="llma-btn" data-act="help" title="Help">i</button>
<button class="llma-btn" data-act="gear" title="Settings">⚙</button></div>
<div class="llma-cards">${LEVELS.map((l) =>
        `<div class="llma-card" data-key="${l.key}"><b>${l.name}</b><span>…</span></div>`).join("")}</div>
<div class="llma-sum"></div>
<div class="llma-last"><span class="t"></span><span class="llma-link" data-act="think">view thinking</span></div>`;
    root.appendChild(inner);
    inner.addEventListener("pointerdown", (e) => e.stopPropagation());
    inner.addEventListener("click", (e) => {
        const pv = e.target.closest(".llma-pv");
        if (pv) { e.stopPropagation(); setProvider(node, pv.dataset.p); return; }
        const card = e.target.closest(".llma-card");
        if (card) { e.stopPropagation(); setLevel(node, card.dataset.key); return; }
        if (e.target.closest(".llma-link")) {
            e.stopPropagation();
            const r = e.target.getBoundingClientRect();
            openThinking(node, r.left, r.bottom + 6);
            return;
        }
        const btn = e.target.closest(".llma-btn");
        if (!btn) return;
        e.stopPropagation();
        const act = btn.dataset.act;
        if (act === "edit") openEditor(node);
        else if (act === "refresh") refreshList(node, btn);
        else if (act === "help") { const r = btn.getBoundingClientRect(); openHelp(r.left, r.bottom + 6); }
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
    node.__llmaRoot = root;
    renderFace(node);
}

/* -------------------------------------------------------- the ⚙ panel */

const SECTIONS = [
    { title: "Sampling", icon: "◐", rows: [
        ["auto_settings", "Auto for this model", "toggle"],
        ["temperature", "Creativity", "slider"],
        ["top_p", "Top p", "slider"],
        ["top_k", "Top k", "slider"],
        ["min_p", "Min p", "slider"],
        ["repeat_penalty", "Repeat penalty", "slider"],
        ["presence_penalty", "Presence penalty", "slider"],
        ["frequency_penalty", "Frequency penalty", "slider"],
    ] },
    { title: "Reasoning", icon: "✦", rows: [
        ["reasoning_effort", "Reasoning effort", "combo"],
        ["gemini_thinking_level", "Thinking level", "combo"],
    ] },
    { title: "LM Studio", icon: "⚡", rows: [
        ["context_length", "Context (when this node loads)", "chips",
            [["model default", 0], ["4k", 4096], ["8k", 8192], ["16k", 16384], ["32k", 32768]]],
        ["unload_after_run", "Unload after the run (only if this node loaded it)", "toggle"],
    ] },
    { title: "Output", icon: "≡", rows: [
        ["output_format", "Format", "combo-chips"],
        ["split_output", "Split positive / negative", "toggle"],
        ["max_tokens", "Max answer length", "number"],
        ["validate", "Check output", "combo-chips"],
        ["stop_sequences", "Stop at (comma-separated)", "line", "empty = none"],
    ] },
    { title: "Connection", icon: "⇄", rows: [
        ["server_url", "Server URL", "line", "empty = the provider's default"],
        ["timeout_seconds", "Timeout (seconds)", "number"],
        ["enable_caching", "Cache the preset (Gemini)", "toggle"],
    ] },
    { title: "Vision · video", icon: "◉", folded: true, rows: [
        ["model_filter", "Model list shows", "combo-chips"],
        ["vision_mp", "Downscale images to MP (0 = off)", "slider"],
        ["video_input_mode", "Video", "combo-chips"],
        ["video_sample_frames", "Sampled frames", "number"],
        ["gemini_video_fps", "Gemini video fps", "slider"],
    ] },
    { title: "Debug", icon: "⋯", folded: true, rows: [
        ["disable_thinking", "Strip <think> from the output", "toggle"],
    ] },
];

// Which settings each kind of route uses. A row not listed for a kind is hidden.
const USES = {
    auto_settings: ["gemini", "xai", "openai", "lmstudio", "custom"],
    temperature: ["gemini", "xai", "openai", "lmstudio", "custom"],
    top_p: ["gemini", "xai", "openai", "lmstudio", "custom"],
    top_k: ["gemini", "lmstudio", "custom"],
    min_p: ["lmstudio"],
    repeat_penalty: ["lmstudio"],
    presence_penalty: ["xai", "openai", "custom"],
    frequency_penalty: ["xai", "openai", "custom"],
    reasoning_effort: ["xai", "openai", "lmstudio", "cli", "agy"],
    gemini_thinking_level: ["gemini"],
    context_length: ["lmstudio"],
    unload_after_run: ["lmstudio"],
    stop_sequences: ["gemini", "xai", "openai", "custom"],
    server_url: ["lmstudio", "custom", "xai", "openai"],
    enable_caching: ["gemini"],
    gemini_video_fps: ["gemini"],
};

const PANEL_API = {
    C, widget, setValue, setLevel, toCustom, levelValue,
    levelOf: (n) => levelOf(n), renderFace: (n) => renderFace(n),
    title: "Settings",
    sections: SECTIONS,
    levelOwned: new Set(["reasoning_effort", "gemini_thinking_level"]),
    autoOwned: new Set(["temperature", "top_p", "top_k", "min_p", "repeat_penalty", "presence_penalty"]),
    rowVisible(n, name) {
        const uses = USES[name];
        if (!uses) return true;
        const k = kindOf(provOf(n));
        if (name === "gemini_thinking_level" && /gemma/i.test(modelOf(n))) return false;
        return uses.includes(k);
    },
    sectionNote(n, title, val) {
        const k = kindOf(provOf(n));
        if (title === "Sampling") {
            return val("auto_settings")
                ? "Auto is on: the model's official values replace the dimmed sliders (known families only)."
                : "Auto is off: these values are used as set.";
        }
        if (title === "Reasoning") {
            const lv = levelOf(n);
            return lv === "custom" ? "Custom: the reasoning level is used as set here."
                : `The ${lv} level sets this; changing it switches to custom.`;
        }
        if (title === "LM Studio") return "A model you or Luna Bridge already loaded is used as it is.";
        if (title === "Connection" && k === "custom") return "Custom needs the server URL (OpenAI-compatible, …/v1).";
        return undefined;
    },
    showOnNode: (n) => toggleSettings(n),
    onOpen: (n) => n.setDirtyCanvas?.(true, false),
    onClose: (n) => n.setDirtyCanvas?.(true, false),
};

function openSettings(node) {
    if (recentlyClosed(node)) return;
    openPanel(node, PANEL_API);
}

function toggleSettings(node) {
    node.properties = node.properties || {};
    node.properties.llmShowSettings = !node.properties.llmShowSettings;
    applyVisibility(node, { resize: true });
    notifyGraphChanged();
}

/* ------------------------------------------------------ help + popups */

let _pop = null;
function closePop() {
    _pop?.remove();
    _pop = null;
    document.removeEventListener("pointerdown", onOutside, true);
    document.removeEventListener("keydown", onEsc, true);
}
function onOutside(e) { if (_pop && !_pop.contains(e.target)) closePop(); }
function onEsc(e) { if (e.key === "Escape") { e.stopPropagation(); closePop(); } }

function openPop(x, y, cls, fill) {
    closePop();
    injectCSS();
    _pop = document.createElement("div");
    _pop.className = `llma-pop ${cls}`;
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

function openThinking(node, x, y) {
    const text = lastRunOf(node)?.reasoning || "";
    openPop(x, y, "think", (el) => { el.textContent = text; });
}

function openHelp(x, y) {
    openPop(x, y, "", (el) => { el.innerHTML = HELP_HTML; });
}

const HELP_HTML = `<b>LLM Prompt (API)</b><br>Turns your idea into a finished prompt with a cloud model, your
subscriptions, or LM Studio.<br><br>
<b>Providers</b>: a green dot means ready (key found, server up, CLI found).<br>
<b>Fast</b> asks for the least reasoning the provider allows; <b>Normal</b>, <b>Quality</b> and <b>Ultra</b> ask for
more. Each card shows what it sends for the chosen model; a dimmed card is the same as the one before it.<br><br>
<b>↻</b> refreshes the model list. For Gemini it also re-checks every model and hides retired ones.<br>
<b>LM Studio</b>: a model this node loads is unloaded after the run; one you or Luna Bridge loaded stays.<br>
<b>Audio</b> works with Gemini and Gemini (Antigravity).<br><br>
<b>⚙</b> opens every setting; changing the reasoning there switches to <b>custom</b>, <b>reset</b> goes back.
<b>✎ idea</b> opens a large editor (Ctrl+Enter saves, Esc cancels).`;

function openEditor(node) {
    const w = widget(node, "user_prompt");
    if (!w) return;
    injectCSS();
    const ov = document.createElement("div");
    ov.className = "llma-ov";
    ov.innerHTML = `<div class="llma-ed"><div class="bar"><b style="color:${C.accent}">Your idea</b><span class="sp"></span>
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

// A size saved before the face (every widget visible) leaves the node far too
// tall; on load it shrinks to fit (the GGUF node's fix, 2026-10-10).
function shrinkIfOversized(node) {
    if (isVue() || node.properties?.llmShowSettings || node.flags?.collapsed) return;
    const need = node.computeSize()[1];
    if (need > 0 && node.size[1] > need * 1.35) {
        node.setSize([node.size[0], need]);
        node.setDirtyCanvas?.(true, true);
    }
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
        const on = node.__llmaHover === ic.act || (ic.act === "gear" && (isPanelOpenFor(node) || node.properties?.llmShowSettings));
        ctx.save();
        ctx.strokeStyle = ctx.fillStyle = on ? C.accent : C.muted;
        ctx.lineWidth = 1.4;
        if (ic.act === "help") {
            ctx.beginPath(); ctx.arc(cx, cy, ICON_R, 0, Math.PI * 2); ctx.stroke();
            ctx.font = "bold 10px sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
            ctx.fillText("i", cx, cy + 0.5);
        } else {
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
    name: "LLM_Prompt.APIFace",
    async setup() {
        onVueModeChange(() => {
            for (const n of app.graph?._nodes || []) {
                if (n?.type === NODE) setTimeout(() => { applyVisibility(n); renderFace(n); }, 50);
            }
        });
    },
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE || nodeType.prototype.__llmaWrapped) return;
        const P = nodeType.prototype;
        P.__llmaWrapped = true;
        P.__llmaFace = true;   // llm_prompt_api.js leaves widget visibility to this face

        const onCreated = P.onNodeCreated;
        P.onNodeCreated = function () {
            const r = onCreated?.apply(this, arguments);
            this.properties = this.properties || {};
            buildFace(this);
            for (const name of ["model_name", "system_prompt", "server_url"]) {
                const mw = widget(this, name);
                if (!mw) continue;
                const cb = mw.callback;
                mw.callback = (...a) => { const rr = cb?.apply(mw, a); refreshInfo(this); return rr; };
            }
            const qw = widget(this, "quality");
            if (qw) {
                const cb = qw.callback;
                qw.callback = (...a) => { const rr = cb?.apply(qw, a); renderFace(this); return rr; };
            }
            applyVisibility(this);
            this.__llmaFresh = true;
            setTimeout(() => refreshInfo(this), 150);
            setTimeout(() => applyVisibility(this, { resize: !!this.__llmaFresh }), 100);
            return r;
        };

        const onConfigure = P.onConfigure;
        P.onConfigure = function () {
            const r = onConfigure?.apply(this, arguments);
            this.__llmaFresh = false;
            renderFace(this);
            setTimeout(() => refreshInfo(this), 200);
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
                refreshInfo(this, true);   // LM Studio load state changed
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
            if (act && canvas?.pointer) delete canvas.pointer.onDoubleClick;
            if (act === "help") { openHelp(e.clientX, e.clientY + 12); return true; }
            if (act === "gear") { openSettings(this); return true; }
            return onDown?.apply(this, arguments);
        };

        const onMove = P.onMouseMove;
        P.onMouseMove = function (e, pos) {
            const act = hitIcon(this, pos);
            if (act !== this.__llmaHover) { this.__llmaHover = act; this.setDirtyCanvas(true, false); }
            return onMove?.apply(this, arguments);
        };

        const onLeave = P.onMouseLeave;
        P.onMouseLeave = function () {
            if (this.__llmaHover) { this.__llmaHover = null; this.setDirtyCanvas(true, false); }
            return onLeave?.apply(this, arguments);
        };

        const onConn = P.onConnectionsChange;
        P.onConnectionsChange = function () {
            const r = onConn?.apply(this, arguments);
            guardHiddenSockets(this);
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
