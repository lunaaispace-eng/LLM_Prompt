// LLM_Prompt - "Advanced" collapse toggle for the LLMPrompt node.
//
// Two ComfyUI frontends exist in the wild and they need different handling:
//
//   Node 2.0 (Vue-based "ComfyUI Frontend") — natively respects the V3
//     advanced=True flag and shows a built-in "Show advanced inputs" toggle.
//     We MUST NOT add our own button here; doing so duplicates the control
//     and corrupts the V2 layout pass (it was shrinking user_prompt). So in
//     V2 this extension does nothing and lets the native collapse work.
//
//   Node 1.0 (legacy LiteGraph-only) — ignores advanced=True, so widgets
//     render inline with no collapse. We add a "▶ Advanced" button just
//     above the advanced widgets; clicking it hides/shows them and resizes
//     the node. Hidden widgets keep their values (purely visual).
//
// The advanced list is checked TWO ways so nothing slips through:
//   1) by widget name (canonical),
//   2) by widget.advanced flag (set by V3 schema advanced=True).
// Belt and suspenders: even if a future widget gets renamed or a frontend
// puts the flag in a different spot, one of the two paths still hides it.

import { app } from "/scripts/app.js";

const ADVANCED = [
    "top_p", "top_k", "min_p", "repetition_penalty",
    "presence_penalty", "frequency_penalty", "preserve_thinking",
    "device", "n_gpu_layers",
    "image_min_tokens", "image_max_tokens", "video_fps",
    "verbose_logging",
];
const ADVANCED_SET = new Set(ADVANCED);
const GHOST = "llmprompt_ghost_";

// V2 Vue frontend exposes app.extensionManager; legacy LiteGraph does not.
function isV2Frontend() {
    return typeof app?.extensionManager !== "undefined";
}

function isAdvancedWidget(w) {
    return ADVANCED_SET.has(w.name)
        || w.advanced === true
        || w.options?.advanced === true;
}

function hideWidget(w) {
    if (w.__llmHidden) return;
    w.__origType = w.type;
    w.__origComputeSize = w.computeSize;
    w.type = GHOST + w.name;        // unrecognized type → not drawn
    w.computeSize = () => [0, -4];  // collapse the row
    w.__llmHidden = true;
}

function showWidget(w) {
    if (!w.__llmHidden) return;
    w.type = w.__origType;
    w.computeSize = w.__origComputeSize;
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

function setupNode(node) {
    if (node.__advSetup) return;
    node.__advSetup = true;

    // V2 Vue frontend has its own native advanced collapse — leave it alone.
    if (isV2Frontend()) return;

    node.properties = node.properties || {};
    if (typeof node.properties.advancedShown !== "boolean") {
        node.properties.advancedShown = false; // collapsed by default
    }

    const btn = node.addWidget("button", "▶  Advanced", null, () => {
        node.properties.advancedShown = !node.properties.advancedShown;
        applyAdvanced(node, node.properties.advancedShown);
    });
    btn.serialize = false; // don't pollute widgets_values
    node.__advBtn = btn;

    // Move the button to sit just above the first advanced widget.
    const bi = node.widgets.indexOf(btn);
    if (bi > -1) {
        node.widgets.splice(bi, 1);
        let firstAdv = node.widgets.findIndex((w) => isAdvancedWidget(w));
        if (firstAdv < 0) firstAdv = node.widgets.length;
        node.widgets.splice(firstAdv, 0, btn);
    }

    applyAdvanced(node, node.properties.advancedShown);
}

app.registerExtension({
    name: "LLM_Prompt.AdvancedToggle",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "LLMPrompt") return;

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
            if (this.__advBtn) applyAdvanced(this, !!this.properties?.advancedShown);
            return r;
        };
    },
});
