// LLM_Prompt — auto-fill sampling presets when the model dropdown changes.
//
// Different model families need different sampling values for clean output.
// When you select a model from the dropdown, this extension detects the
// family from the filename and snaps the sampling widgets to that family's
// recommended defaults. You can still tweak any widget after auto-fill.
//
// Sources for the presets:
//   Qwen 3.5    : Qwen official inference defaults
//   Qwen 3-VL   : Qwen 3 VL official defaults
//   Gemma 4     : Google / Unsloth model card recommendation
//   Gemma 3     : Same as Gemma 4 (shared sampling profile)
//   SuperGemma  : Conservative, the fine-tune is unstable at higher temps
//   Llama 3.x   : Meta default
//
// Add or tweak entries in MODEL_PRESETS below to change behavior.

import { app } from "/scripts/app.js";

const MODEL_PRESETS = {
    // Each preset: which sampling values to apply.
    // Only listed widgets are updated. Anything not listed is left alone.
    qwen_3_5: {
        label: "Qwen 3.5",
        values: {
            temperature: 0.7,
            top_p: 0.8,
            top_k: 20,
            min_p: 0.05,
            repetition_penalty: 1.0,
        },
    },
    qwen_3_vl: {
        label: "Qwen 3-VL",
        values: {
            temperature: 0.7,
            top_p: 0.8,
            top_k: 20,
            min_p: 0.05,
            repetition_penalty: 1.0,
        },
    },
    qwen_other: {
        label: "Qwen (generic)",
        values: {
            temperature: 0.7,
            top_p: 0.8,
            top_k: 20,
            min_p: 0.05,
            repetition_penalty: 1.0,
        },
    },
    gemma_4: {
        label: "Gemma 4",
        values: {
            temperature: 1.0,
            top_p: 0.95,
            top_k: 64,
            min_p: 0.0,
            repetition_penalty: 1.0,
        },
    },
    gemma_3: {
        label: "Gemma 3",
        values: {
            temperature: 1.0,
            top_p: 0.95,
            top_k: 64,
            min_p: 0.0,
            repetition_penalty: 1.0,
        },
    },
    supergemma: {
        label: "SuperGemma",
        values: {
            temperature: 0.8,
            top_p: 0.9,
            top_k: 30,
            min_p: 0.05,
            repetition_penalty: 1.0,
        },
    },
    llama_3: {
        label: "Llama 3.x",
        values: {
            temperature: 0.6,
            top_p: 0.9,
            top_k: 50,
            min_p: 0.05,
            repetition_penalty: 1.0,
        },
    },
};

// Detect model family from filename. Order matters — first match wins.
// Most-specific patterns (supergemma) must precede less-specific ones (gemma).
function detectFamily(modelName) {
    if (!modelName) return null;
    const n = String(modelName).toLowerCase();

    // SuperGemma must come before generic gemma checks
    if (n.includes("supergemma")) return "supergemma";

    // Qwen family — order matters: most specific first
    if (n.includes("qwen")) {
        if (n.includes("3.5") || n.includes("3_5") || n.includes("3-5")) return "qwen_3_5";
        if (n.match(/qwen.?3.?vl/) || n.includes("3-vl") || n.includes("3_vl")) return "qwen_3_vl";
        return "qwen_other";
    }

    // Gemma family
    if (n.includes("gemma")) {
        if (n.includes("gemma-4") || n.includes("gemma4") || n.includes("gemma_4")) return "gemma_4";
        if (n.includes("gemma-3") || n.includes("gemma3") || n.includes("gemma_3")) return "gemma_3";
        // Default new Gemma to Gemma 4 profile (more common modern variant)
        return "gemma_4";
    }

    // Llama
    if (n.includes("llama-3") || n.includes("llama3") || n.includes("llama_3")) return "llama_3";
    if (n.includes("llama-4") || n.includes("llama4") || n.includes("llama_4")) return "llama_3";

    return null;
}

// Apply a preset by setting widget values. Triggers ComfyUI's redraw.
function applyPreset(node, presetKey) {
    const preset = MODEL_PRESETS[presetKey];
    if (!preset) return;

    let applied = [];
    for (const [name, value] of Object.entries(preset.values)) {
        const w = node.widgets?.find((widget) => widget.name === name);
        if (w) {
            w.value = value;
            applied.push(`${name}=${value}`);
            if (typeof w.callback === "function") {
                try { w.callback(value); } catch (e) { /* ignore */ }
            }
        }
    }

    if (applied.length > 0) {
        console.log(
            `[LLM_Prompt] Applied "${preset.label}" preset: ${applied.join(", ")}`
        );
    }
    node.setDirtyCanvas(true, true);
}

app.registerExtension({
    name: "LLM_Prompt.ModelPresets",

    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        if (nodeData.name !== "LLMPrompt") return;

        const onCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onCreated?.apply(this, arguments);

            // Capture the node in a closure so the callback always has the
            // right reference, regardless of how ComfyUI invokes it.
            const node = this;
            const modelWidget = node.widgets?.find((w) => w.name === "model_name");
            if (!modelWidget) return r;

            // Wrap the existing callback so we don't clobber whatever ComfyUI set
            const origCallback = modelWidget.callback;
            modelWidget.callback = function (value) {
                if (typeof origCallback === "function") {
                    try { origCallback.apply(this, arguments); } catch (e) { /* ignore */ }
                }
                const family = detectFamily(value);
                if (family) {
                    applyPreset(node, family);
                }
            };

            return r;
        };
    },
});
