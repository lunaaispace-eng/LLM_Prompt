// LLM_Prompt - "Advanced" collapse toggle.
//
// The V3 schema marks advanced inputs with advanced=True, but not every ComfyUI
// frontend renders that as a collapsible section (some show them inline). This
// extension adds an explicit "Advanced" button to the LLMPrompt node that
// hides/shows the advanced widgets and resizes the node. Hidden widgets keep and
// serialize their values - this is purely visual.

import { app } from "/scripts/app.js";

// Must match the inputs tagged advanced=True in define_schema().
const ADVANCED = [
    "top_p", "top_k", "min_p", "repetition_penalty",
    "presence_penalty", "frequency_penalty", "preserve_thinking",
    "device", "n_gpu_layers",
    "image_min_tokens", "image_max_tokens", "video_fps",
    "verbose_logging",
];

const GHOST = "llmprompt_ghost_";

function hideWidget(w) {
    if (w.__llmHidden) return;
    w.__origType = w.type;
    w.__origComputeSize = w.computeSize;
    w.type = GHOST + w.name;        // unrecognized type -> not drawn
    w.computeSize = () => [0, -4];  // collapse its row height
    w.__llmHidden = true;
}

function showWidget(w) {
    if (!w.__llmHidden) return;
    w.type = w.__origType;
    w.computeSize = w.__origComputeSize;
    w.__llmHidden = false;
}

function applyAdvanced(node, show) {
    for (const name of ADVANCED) {
        const w = node.widgets?.find((x) => x.name === name);
        if (w) (show ? showWidget : hideWidget)(w);
    }
    if (node.__advBtn) {
        node.__advBtn.label = show ? "▼  Advanced (hide)" : "▶  Advanced";
    }
    // Recompute height to remove/restore the gap, preserving width.
    const newH = node.computeSize()[1];
    node.setSize([node.size[0], newH]);
    node.setDirtyCanvas(true, true);
}

function setupNode(node) {
    if (node.__advSetup) return;
    node.__advSetup = true;

    node.properties = node.properties || {};
    if (typeof node.properties.advancedShown !== "boolean") {
        node.properties.advancedShown = false; // collapsed by default
    }

    const btn = node.addWidget("button", "▶  Advanced", null, () => {
        node.properties.advancedShown = !node.properties.advancedShown;
        applyAdvanced(node, node.properties.advancedShown);
    });
    btn.serialize = false; // never write the button into widgets_values
    node.__advBtn = btn;

    // Move the button to sit just above the first advanced widget.
    const bi = node.widgets.indexOf(btn);
    if (bi > -1) {
        node.widgets.splice(bi, 1);
        let firstAdv = node.widgets.findIndex((w) => ADVANCED.includes(w.name));
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
