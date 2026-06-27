// LLM_Prompt — "Advanced" collapse toggle for the LLMPrompt / LLMPromptAPI nodes.
//
// Frontend-compat fix (Node 2.0 / Vue):
//   The modern Vue frontend decides whether to render a widget from
//   `w.options.hidden` and needs a reactivity nudge to re-render. The legacy
//   canvas frontend honored `w.hidden` + `computeSize`. A ComfyUI frontend
//   update moved hiding to the Vue path, which is why the fold silently broke in
//   Node 2.0 while still working in Node 1.0.
//
//   This version sets BOTH paths (w.hidden + w.options.hidden) and notifies Vue
//   after a change — the same technique the working Eclipse pack uses — so the
//   fold works in Node 1.0 (canvas) AND Node 2.0 (Vue). The custom button owns
//   the fold in both modes; it no longer defers to native handling.

import { app } from "/scripts/app.js";

const ADVANCED = [
    "top_p", "top_k", "min_p", "repetition_penalty",
    "presence_penalty", "frequency_penalty", "preserve_thinking",
    "device", "n_gpu_layers",
    "image_min_tokens", "image_max_tokens", "video_fps",
    "verbose_logging",
];
const ADVANCED_SET = new Set(ADVANCED);

// Reliable Node 1.0 (canvas) vs Node 2.0 (Vue/DOM) discriminator.
function isVueMode() {
    try { return !!LiteGraph.vueNodesMode; } catch (_) { return false; }
}

// Nudge Vue's reactivity so a visibility change re-renders. Mutating the widgets
// array (pop+push the last entry) is what the frontend watches. Cheap, no-op-safe.
function notifyVue(node) {
    const ws = node.widgets;
    if (ws?.length) { const last = ws.pop(); ws.push(last); }
}

function isAdvancedWidget(w) {
    if (!w) return false;
    return ADVANCED_SET.has(w.name) ||
           w.advanced === true ||
           w.options?.advanced === true;
}

function hideWidget(w) {
    if (w.__llmHidden) return;
    w.__origType = w.type;
    w.__origComputeSize = w.computeSize;
    w.__origHidden = w.hidden;
    w.__origOptHidden = w.options ? w.options.hidden : undefined;
    w.type = "converted-widget";              // canvas (Node 1.0) hide path
    w.computeSize = () => [0, -4];
    w.hidden = true;
    if (w.options) w.options.hidden = true;   // Vue (Node 2.0) hide path  ← the fix
    w.__llmHidden = true;
}

function showWidget(w) {
    if (!w.__llmHidden) return;
    w.type = w.__origType;
    w.computeSize = w.__origComputeSize;
    w.hidden = w.__origHidden;
    if (w.options) w.options.hidden = w.__origOptHidden;
    w.__llmHidden = false;
}

function applyAdvanced(node, show) {
    if (!node.widgets) return;
    for (const w of node.widgets) {
        if (isAdvancedWidget(w)) (show ? showWidget : hideWidget)(w);
    }
    if (node.__advBtn) {
        node.__advBtn.label = show ? "▼  Advanced (hide)" : "▶  Advanced";
    }
    const newH = node.computeSize()[1];
    node.setSize([node.size[0], newH]);
    node.setDirtyCanvas(true, true);
    if (isVueMode()) notifyVue(node);   // re-render in Vue mode
}

function attachButton(node) {
    if (node.__advBtn) return;
    node.properties = node.properties || {};
    if (typeof node.properties.advancedShown !== "boolean") {
        node.properties.advancedShown = false;
    }
    // Keep the button at the END of node.widgets — never splice it between the
    // basic and advanced widgets. ComfyUI's widgets_values save/load is
    // positional and a mid-array button shifts every later widget's index, so
    // values land in the wrong widgets across tab switches.
    const btn = node.addWidget("button", "▶  Advanced", null, () => {
        node.properties.advancedShown = !node.properties.advancedShown;
        applyAdvanced(node, node.properties.advancedShown);
    });
    btn.serialize = false;
    node.__advBtn = btn;
    applyAdvanced(node, node.properties.advancedShown);
}

// Re-assert the current fold state (used on create, configure, and mode switch).
function reapply(node) {
    if (!node || !Array.isArray(node.widgets)) return;
    if (!node.__advBtn) attachButton(node);
    else applyAdvanced(node, !!node.properties?.advancedShown);
}

function setupNode(node) {
    if (node.__advSetup) return;
    node.__advSetup = true;
    setTimeout(() => reapply(node), 100);
}

// Cooperative Node 1.0 <-> 2.0 switch watcher. Shares the SAME window keys
// Eclipse uses, so the two packs reuse a single property watcher on
// LiteGraph.vueNodesMode instead of clobbering each other. Fires only on an
// actual mode toggle — no DOM polling, no widget-mutation feedback loop.
const _VMC_KEY = "__comfy_vueModeCallbacks";
const _VMC_LOCK = "__comfy_vueModeWatcherInstalled";
function installVueModeWatcher() {
    if (!window[_VMC_KEY]) window[_VMC_KEY] = new Set();
    if (window[_VMC_LOCK]) return;
    window[_VMC_LOCK] = true;
    try {
        let _value = !!LiteGraph.vueNodesMode;
        Object.defineProperty(LiteGraph, "vueNodesMode", {
            get() { return _value; },
            set(v) {
                const prev = _value; _value = !!v;
                if (prev !== _value) {
                    for (const cb of (window[_VMC_KEY] || [])) {
                        try { cb(_value, prev); } catch (e) { console.error("vueModeChange cb error", e); }
                    }
                }
            },
            configurable: true, enumerable: true,
        });
    } catch (_) { /* swallow */ }
}
function onVueModeChange(cb) {
    installVueModeWatcher();
    window[_VMC_KEY].add(cb);
}

app.registerExtension({
    name: "LLM_Prompt.AdvancedToggle",
    async setup() {
        onVueModeChange(() => {
            const nodes = window?.app?.graph?._nodes;
            if (!Array.isArray(nodes)) return;
            for (const n of nodes) {
                if (n?.type === "LLMPrompt" || n?.type === "LLMPromptAPI") {
                    setTimeout(() => reapply(n), 50);
                }
            }
        });
    },
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "LLMPrompt" && nodeData.name !== "LLMPromptAPI") return;

        const onCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onCreated?.apply(this, arguments);
            setupNode(this);
            return r;
        };

        // Re-apply after a saved workflow restores node.properties.
        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function (info) {
            const r = onConfigure?.apply(this, arguments);
            const node = this;
            setTimeout(() => reapply(node), 150);
            return r;
        };
    },
});
