// LLM Prompt (GGUF) — load old saved workflows with their values in the right widgets.
//
// ComfyUI restores widgets_values BY POSITION. The node's widget list has changed
// several times (settings inserted in the middle, others removed), so a workflow
// saved with an older list loads its values into the wrong widgets: e.g. a
// 30-value workflow from before 2026-10-10 got n_gpu_layers = "auto",
// device = -1 and verbose_logging = "off" once reasoning_budget and
// mtp_draft_tokens were inserted (measured on Qwen2.1_T2I.json).
//
// Each historic layout below is the widget order of that version (from git;
// control_after_generate is the seed's companion widget). On configure, a saved
// list whose length matches an old layout is re-applied by name, and widgets
// the old version did not have go back to their defaults. Names that no longer
// exist (mirostat_*, bbox_min_score) are dropped.

import { app } from "/scripts/app.js";

const NODE = "LLMPrompt";
const COMMON = ["model_name", "system_prompt", "custom_system_prompt", "user_prompt", "output_format"];
const AUG = [...COMMON, "split_output", "auto_settings", "disable_thinking", "temperature", "max_tokens", "n_ctx",
    "seed", "control_after_generate", "keep_model_loaded", "top_p", "top_k", "min_p", "repetition_penalty",
    "presence_penalty", "frequency_penalty", "preserve_thinking", "device", "n_gpu_layers"];

const LAYOUTS = {
    // b2eed4c 2026-05-21
    13: [...COMMON, "max_tokens", "temperature", "top_p", "repetition_penalty", "device", "keep_model_loaded",
        "seed", "control_after_generate"],
    // 987f3af 2026-05-24
    15: [...COMMON, "max_tokens", "temperature", "top_p", "repetition_penalty", "device", "keep_model_loaded",
        "seed", "control_after_generate", "n_ctx", "n_gpu_layers"],
    // ab63335 2026-05-24
    17: [...COMMON, "max_tokens", "temperature", "top_p", "top_k", "min_p", "repetition_penalty", "device",
        "keep_model_loaded", "seed", "control_after_generate", "n_ctx", "n_gpu_layers"],
    // f93dec7 2026-05-31
    18: [...COMMON, "max_tokens", "temperature", "top_p", "top_k", "min_p", "repetition_penalty", "device",
        "disable_thinking", "keep_model_loaded", "seed", "control_after_generate", "n_ctx", "n_gpu_layers"],
    // 31885bf 2026-06-06
    19: [...COMMON, "max_tokens", "temperature", "top_p", "top_k", "min_p", "repetition_penalty", "device",
        "auto_settings", "disable_thinking", "keep_model_loaded", "seed", "control_after_generate", "n_ctx", "n_gpu_layers"],
    // b361bec 2026-06-07
    20: [...COMMON, "split_output", "max_tokens", "temperature", "top_p", "top_k", "min_p", "repetition_penalty",
        "device", "auto_settings", "disable_thinking", "keep_model_loaded", "seed", "control_after_generate", "n_ctx",
        "n_gpu_layers"],
    // 9cd0762 2026-08-05
    27: [...AUG, "image_min_tokens", "image_max_tokens", "video_fps", "verbose_logging"],
    // f6dc3c8 2026-06-18
    28: [...AUG, "image_min_tokens", "image_max_tokens", "video_fps", "bbox_min_score", "verbose_logging"],
    // b44941f 2026-10-07 (and every version since 2026-08 with load_mmproj)
    30: [...AUG, "load_mmproj", "image_min_tokens", "image_max_tokens", "video_fps", "verbose_logging",
        "vision_mp", "validate"],
};
// Two different 29-value layouts: June (V3 migration) and August (vision_mp + validate).
const LAYOUT_29_JUNE = [...COMMON, "split_output", "disable_thinking", "auto_settings", "max_tokens", "temperature",
    "top_p", "top_k", "min_p", "repetition_penalty", "seed", "control_after_generate", "keep_model_loaded", "device",
    "presence_penalty", "frequency_penalty", "mirostat_mode", "mirostat_tau", "mirostat_eta", "preserve_thinking",
    "image_min_tokens", "image_max_tokens", "video_fps", "n_ctx", "n_gpu_layers"];
const LAYOUT_29_AUG = [...AUG, "image_min_tokens", "image_max_tokens", "video_fps", "verbose_logging", "vision_mp",
    "validate"];

// A 17-value layout that is in no commit (saved by a build between 987f3af and
// ab63335): the 15-value list plus an extra INT + control pair after the seed.
// Seen in Pony_Illu_Workflow.json and LTX2.3_compact_long.json.
const LAYOUT_17_EXTRA = [...COMMON, "max_tokens", "temperature", "top_p", "repetition_penalty", "device",
    "keep_model_loaded", "seed", "control_after_generate", "__unknown", "__unknown_control", "n_ctx", "n_gpu_layers"];

export function layoutFor(values) {
    if (!Array.isArray(values)) return null;
    if (values.length === 17 && typeof values[9] === "string") return LAYOUT_17_EXTRA;  // device at 9
    if (values.length === 29) {
        // August ends with validate ("off" / "h3"); June ends with n_gpu_layers (a number).
        return typeof values[28] === "string" ? LAYOUT_29_AUG : LAYOUT_29_JUNE;
    }
    return LAYOUTS[values.length] || null;
}

function defaultOf(node, name) {
    const spec = node.constructor?.nodeData?.input?.required?.[name]
        ?? node.constructor?.nodeData?.input?.optional?.[name];
    const opts = Array.isArray(spec) ? spec[1] : undefined;
    if (opts && "default" in opts) return opts.default;
    if (Array.isArray(spec?.[0])) return spec[0][0];          // legacy combo list
    if (Array.isArray(opts?.options)) return opts.options[0]; // V3 combo
    return undefined;
}

export function remapOldValues(node, values) {
    const layout = layoutFor(values);
    if (!layout || !Array.isArray(node.widgets)) return false;
    const saved = new Map(layout.map((name, i) => [name, values[i]]));
    // Booleans saved as 0 / 1 by some old builds (Ideogram_Master.json).
    for (const w of node.widgets) {
        if (saved.has(w.name) && w.type === "toggle" && typeof saved.get(w.name) === "number") {
            saved.set(w.name, !!saved.get(w.name));
        }
    }
    for (const w of node.widgets) {
        if (w.serialize === false || w.options?.serialize === false) continue;
        if (saved.has(w.name)) {
            w.value = saved.get(w.name);
        } else if (w.name !== "control_after_generate") {
            const d = defaultOf(node, w.name);
            if (d !== undefined) w.value = d;
        }
    }
    console.info(`[LLM_Prompt] node ${node.id}: old ${values.length}-value layout re-applied by name.`);
    return true;
}

app.registerExtension({
    name: "LLM_Prompt.Compat",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE || nodeType.prototype.__llmCompatWrapped) return;
        nodeType.prototype.__llmCompatWrapped = true;
        const onConfigure = nodeType.prototype.onConfigure;
        // Synchronous, inside configure: the change tracker takes its baseline
        // after the load, so an untouched old workflow is not flagged modified.
        nodeType.prototype.onConfigure = function (info) {
            try { remapOldValues(this, info?.widgets_values); } catch (e) { console.error("[LLM_Prompt] compat", e); }
            return onConfigure?.apply(this, arguments);
        };
    },
});
