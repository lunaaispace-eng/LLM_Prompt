// LLM_Prompt presets — auto-fill sampling widgets when the model dropdown changes.
//
// Registered for BOTH the local GGUF node (LLMPrompt) and the API node
// (LLMPromptAPI). Detects the model family from the filename / cloud model ID
// and snaps the sampling widgets to that family's recommended defaults.
//
// Preset sources:
//   Qwen 3.5 / 3.6  : Unsloth official docs (https://unsloth.ai/docs/models/qwen3.5)
//                     Non-thinking general tasks: temp 0.7, top_p 0.8, top_k 20,
//                     min_p 0.0, presence_penalty 1.5, repetition_penalty 1.0
//   Qwen 3-VL       : Same as Qwen 3.5 non-thinking
//   Gemma 4 / 3     : Google + Unsloth — temp 1.0, top_p 0.95, top_k 64,
//                     min_p 0.0, repetition_penalty 1.0
//   Gemini cloud    : Treated as Gemma 4 (same architecture family)
//   Grok            : temp 0.7, top_p 0.95, top_k 0, min_p 0 (rejects them anyway)
//   SuperGemma      : Conservative (unstable fine-tune)
//   Llama 3.x       : Meta defaults

import { app } from "/scripts/app.js";

const PRESETS = {
    qwen_instruct: {
        label: "Qwen 3.5/3.6 (instruct)",
        values: {
            temperature: 0.7,
            top_p: 0.8,
            top_k: 20,
            min_p: 0.0,
            presence_penalty: 1.5,
            repetition_penalty: 1.0,
        },
    },
    qwen_thinking: {
        label: "Qwen 3.5/3.6 (thinking)",
        values: {
            temperature: 1.0,
            top_p: 0.95,
            top_k: 20,
            min_p: 0.0,
            presence_penalty: 1.5,
            repetition_penalty: 1.0,
        },
    },
    qwen_vl: {
        label: "Qwen 3-VL",
        values: {
            temperature: 0.7,
            top_p: 0.8,
            top_k: 20,
            min_p: 0.0,
            presence_penalty: 1.5,
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
            presence_penalty: 0.0,
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
            presence_penalty: 0.0,
            repetition_penalty: 1.0,
        },
    },
    gemini_cloud: {
        // Same architecture as Gemma 4. Cloud API rejects top_k/min_p directly
        // (those go via extra_body) but we keep them set so the widget shows
        // sensible values — backend sanitization handles routing.
        label: "Gemini (cloud)",
        values: {
            temperature: 1.0,
            top_p: 0.95,
            top_k: 64,
            min_p: 0.0,
            presence_penalty: 0.0,
            repetition_penalty: 1.0,
        },
    },
    grok: {
        label: "Grok (xAI)",
        values: {
            temperature: 0.7,
            top_p: 0.95,
            top_k: 0,   // Grok rejects this — backend strips
            min_p: 0.0,
            presence_penalty: 0.0,
            repetition_penalty: 1.0,
        },
    },
    supergemma: {
        label: "SuperGemma",
        values: {
            temperature: 0.8,
            top_p: 0.9,
            top_k: 30,
            min_p: 0.0,
            presence_penalty: 0.0,
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
            presence_penalty: 0.0,
            repetition_penalty: 1.0,
        },
    },
};

// Detect model family from filename or cloud model ID.
// Order matters — most specific patterns must come first.
function detectFamily(modelName) {
    if (!modelName) return null;
    let n = String(modelName).toLowerCase().trim();

    // OpenRouter-style "provider/model" prefix — strip the prefix and recurse
    if (n.includes("/") && !n.startsWith("http")) {
        const parts = n.split("/");
        const tail = parts[parts.length - 1];
        return detectFamily(tail);
    }

    // Specific fine-tunes before family names
    if (n.includes("supergemma")) return "supergemma";

    // Qwen variants — most specific first
    if (n.includes("qwen")) {
        if (n.match(/qwen.?3.?vl/) || n.includes("3-vl") || n.includes("3_vl")) return "qwen_vl";
        if (n.includes("think") && !n.includes("no.?think")) return "qwen_thinking";
        // Default Qwen (3.5, 3.6) to instruct mode
        return "qwen_instruct";
    }

    // Cloud Gemini — same architecture as Gemma 4 but distinct preset
    // (mainly so the user can identify what triggered the auto-fill)
    if (n.startsWith("gemini") || n.includes("models/gemini")) return "gemini_cloud";

    // Gemma family
    if (n.includes("gemma")) {
        if (n.includes("gemma-4") || n.includes("gemma4") || n.includes("gemma_4")) return "gemma_4";
        if (n.includes("gemma-3") || n.includes("gemma3") || n.includes("gemma_3")) return "gemma_3";
        // Default newer Gemma to Gemma 4 profile
        return "gemma_4";
    }

    // Grok
    if (n.startsWith("grok") || n.includes("grok-")) return "grok";

    // Llama 3.x / 4.x
    if (n.includes("llama-3") || n.includes("llama3") || n.includes("llama_3") ||
        n.includes("llama-4") || n.includes("llama4") || n.includes("llama_4")) {
        return "llama_3";
    }

    return null;
}

function applyPreset(node, presetKey) {
    const preset = PRESETS[presetKey];
    if (!preset) return;

    const applied = [];
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

function hookModelWidget(node) {
    const w = node.widgets?.find((widget) => widget.name === "model_name");
    if (!w) return;
    // Self-healing: re-wrap whenever OUR wrapper isn't the active callback.
    // The API node replaces the model widget's callback when the provider /
    // model options rebuild (provider switch, live /models refresh), which
    // silently dropped our preset hook. The old one-time `_presetHooked` flag
    // never re-fired in that case (you had to switch category and back). An
    // identity check makes re-hooking reliable.
    if (w.callback && w.callback.__llmPresetWrapper) return;

    const orig = w.callback;
    const wrapper = function (value) {
        if (typeof orig === "function") {
            try { orig.apply(this, arguments); } catch (e) { /* ignore */ }
        }
        const family = detectFamily(value);
        if (family) {
            applyPreset(node, family);
        }
    };
    wrapper.__llmPresetWrapper = true;
    w.callback = wrapper;
}

// API node only: when the provider changes the model options rebuild, so
// re-hook the model widget AND immediately apply the preset for whatever model
// is now selected — no more "pick another category and come back" dance.
function hookProviderWidget(node) {
    const pw = node.widgets?.find((widget) => widget.name === "provider");
    if (!pw || pw.__llmProviderHooked) return;
    pw.__llmProviderHooked = true;

    const orig = pw.callback;
    pw.callback = function (value) {
        if (typeof orig === "function") {
            try { orig.apply(this, arguments); } catch (e) { /* ignore */ }
        }
        // Model options rebuild asynchronously after a provider switch; re-hook
        // and apply on the next tick once the new model value has settled.
        setTimeout(() => {
            hookModelWidget(node);
            const mw = node.widgets?.find((widget) => widget.name === "model_name");
            if (mw) {
                const family = detectFamily(mw.value);
                if (family) applyPreset(node, family);
            }
        }, 200);
    };
}

// Register for both node types — same logic applies.
app.registerExtension({
    name: "LLM_Prompt.ModelPresets",

    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        if (nodeData.name !== "LLMPrompt" && nodeData.name !== "LLMPromptAPI") return;

        const onCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onCreated?.apply(this, arguments);
            const node = this;
            hookModelWidget(node);
            hookProviderWidget(node);

            // Backstop: re-assert both hooks shortly after creation (widgets may
            // still be building) and periodically (cheap identity checks bail
            // immediately when already hooked). Self-healing if anything
            // replaces a callback later. The interval is tracked per-node and
            // cleared in onRemoved so deleted nodes don't leak timers.
            setTimeout(() => { hookModelWidget(node); hookProviderWidget(node); }, 150);
            node.__llmPresetInterval = setInterval(() => {
                hookModelWidget(node);
                hookProviderWidget(node);
            }, 1500);

            return r;
        };

        const onRemoved = nodeType.prototype.onRemoved;
        nodeType.prototype.onRemoved = function () {
            if (this.__llmPresetInterval) {
                clearInterval(this.__llmPresetInterval);
                this.__llmPresetInterval = null;
            }
            return onRemoved?.apply(this, arguments);
        };
    },
});
