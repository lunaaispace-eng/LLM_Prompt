// LLM_Prompt - "Advanced" collapse toggle for the LLMPrompt node.
//
// Diagnostic finding (Node 1.0 visual mode):
//   - top_p widget: type="number", element=undefined (canvas-drawn)
//   - top_p widget: options.advanced === true (V3 schema flag preserved here)
//   - node.showAdvanced exists as a property
//   - V2 native "Show/Hide advanced inputs" button found in DOM as a styled DIV
//
// The reliable V1-vs-V2 discriminator: a *number* widget's `.element`.
//   V1: canvas-drawn → element is undefined
//   V2: DOM-rendered → element is an HTMLElement
// (Multiline string widgets have a DOM textarea in BOTH modes, so probing them
// gives a false positive. Number/combo widgets are mode-specific.)

import { app } from "/scripts/app.js";

const ADVANCED = [
    "top_p", "top_k", "min_p", "repetition_penalty",
    "presence_penalty", "frequency_penalty", "preserve_thinking",
    "device", "n_gpu_layers",
    "image_min_tokens", "image_max_tokens", "video_fps",
    "verbose_logging",
];
const ADVANCED_SET = new Set(ADVANCED);

function isV2VisualMode(node) {
    // Diagnostic-confirmed discriminator: the modern frontend ONLY adds the
    // V2 native "Show/Hide advanced inputs" button to the DOM when V2 visual
    // mode is active. In V1 visual mode it's not in the DOM at all.
    //
    // We check existence, NOT visibility — getBoundingClientRect returns
    // positive dimensions for elements with z-index: -1, so visibility checks
    // false-positive in V1.
    //
    // Widget-element probing turned out unreliable too: the modern frontend
    // sometimes attaches .element to widgets it isn't visibly rendering.
    try {
        for (const el of document.querySelectorAll("div, button")) {
            const t = (el.textContent || "").trim();
            if (t.length < 30 && /^(show|hide)\s*advanced\s*inputs?$/i.test(t)) {
                return true;
            }
        }
    } catch (_) { /* swallow */ }
    return false;
}

function isAdvancedWidget(w) {
    if (!w) return false;
    // Diagnostic confirmed the schema flag arrives on options.advanced
    return ADVANCED_SET.has(w.name) ||
           w.advanced === true ||
           w.options?.advanced === true;
}

function hideWidget(w) {
    if (w.__llmHidden) return;
    w.__origType = w.type;
    w.__origComputeSize = w.computeSize;
    w.__origHidden = w.hidden;
    // Three-pronged hide: cover every render path the modern frontend uses,
    // so booleans (verbose_logging, preserve_thinking) don't escape like they
    // did with single-signal approaches.
    w.type = "converted-widget";    // official "hidden" type for canvas path
    w.computeSize = () => [0, -4];  // collapse the row in layout calc
    w.hidden = true;                 // the boolean / DOM render path checks this
    w.__llmHidden = true;
}

function showWidget(w) {
    if (!w.__llmHidden) return;
    w.type = w.__origType;
    w.computeSize = w.__origComputeSize;
    w.hidden = w.__origHidden;
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
}

function attachButton(node) {
    if (node.__advBtn) return;

    node.properties = node.properties || {};
    if (typeof node.properties.advancedShown !== "boolean") {
        node.properties.advancedShown = false;
    }

    const btn = node.addWidget("button", "▶  Advanced", null, () => {
        node.properties.advancedShown = !node.properties.advancedShown;
        applyAdvanced(node, node.properties.advancedShown);
    });
    btn.serialize = false;
    node.__advBtn = btn;

    // CRITICAL: do NOT splice the button into the middle of node.widgets.
    // ComfyUI's widgets_values array is positional, and an earlier version
    // that spliced the button between the basic and advanced widgets shifted
    // every advanced widget's position by one. ComfyUI's tab-switch save/load
    // path doesn't fully honour `serialize: false` on re-deserialise, so
    // values from widgets_values would land in the wrong widgets (showing up
    // as "Value not in list", "smaller than min", or NoneType conversion
    // errors). Leaving the button at the end of node.widgets keeps every
    // existing widget at its original index — values stay correctly aligned
    // through any number of tab switches. Visual trade-off: the button
    // appears at the bottom of the node instead of above the advanced
    // widgets.

    applyAdvanced(node, node.properties.advancedShown);
}

function tearDown(node) {
    // V2 mode was detected late — undo our button + restore widgets.
    if (!node.__advBtn) return;
    const idx = node.widgets.indexOf(node.__advBtn);
    if (idx > -1) node.widgets.splice(idx, 1);
    node.__advBtn = null;
    for (const w of node.widgets) {
        if (isAdvancedWidget(w)) showWidget(w);
    }
    const newH = node.computeSize()[1];
    node.setSize([node.size[0], newH]);
    node.setDirtyCanvas(true, true);
}

// Reconcile a single node's state with the current visual mode. Idempotent —
// only acts when the actual state differs from what the current mode wants.
// V1: should have our button, advanced widgets hidden.
// V2: should NOT have our button, advanced widgets unhidden (V2 native owns it).
function syncNodeWithMode(node) {
    if (!node || !Array.isArray(node.widgets)) return;
    if (isV2VisualMode(node)) {
        if (node.__advBtn) tearDown(node);
        // Also unhide any widgets we hid in a previous V1 lifecycle
        for (const w of node.widgets) {
            if (isAdvancedWidget(w) && w.__llmHidden) showWidget(w);
        }
    } else {
        if (!node.__advBtn) attachButton(node);
    }
}

function setupNode(node) {
    if (node.__advSetup) return;
    node.__advSetup = true;
    // 250 ms initial defer so V2 has time to populate its DOM markers.
    setTimeout(() => syncNodeWithMode(node), 250);
}

// Visual-mode change handler: the user can switch Node 1.0 <-> Node 2.0 in
// Settings without reloading, which leaves per-node state stale (V1 button
// still attached when V2 takes over, or no button when V1 returns).
//
// Event-based, not polling: a MutationObserver watches document.body for DOM
// changes. When V2's "Show/Hide advanced inputs" button gets added/removed by
// a mode switch, the observer fires. Debounced 200 ms so rapid DOM churn from
// other ComfyUI work coalesces into one sync. Zero work in steady state.
let _observer = null;
function startSyncObserver() {
    if (_observer) return;
    let debounce = null;
    const tick = () => {
        try {
            const nodes = window?.app?.graph?._nodes;
            if (!Array.isArray(nodes)) return;
            for (const n of nodes) {
                if (n?.type === "LLMPrompt") syncNodeWithMode(n);
            }
        } catch (_) { /* swallow */ }
    };
    _observer = new MutationObserver(() => {
        if (debounce) clearTimeout(debounce);
        debounce = setTimeout(tick, 200);
    });
    _observer.observe(document.body, { childList: true, subtree: true });
    // One immediate tick to cover any state the observer missed before starting.
    setTimeout(tick, 300);
}

app.registerExtension({
    name: "LLM_Prompt.AdvancedToggle",
    async setup() {
        // Kick off the mode-change reconciler. Idempotent — safe to call once.
        startSyncObserver();
    },
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "LLMPrompt") return;

        const onCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onCreated?.apply(this, arguments);
            setupNode(this);
            return r;
        };

        // Re-apply after a saved workflow restores node.properties. The button
        // may not exist yet (setupNode is deferred), so retry slightly later.
        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function (info) {
            const r = onConfigure?.apply(this, arguments);
            const node = this;
            setTimeout(() => {
                if (node.__advBtn) applyAdvanced(node, !!node.properties?.advancedShown);
            }, 150);
            return r;
        };
    },
});
