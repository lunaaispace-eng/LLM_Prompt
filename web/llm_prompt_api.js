// LLM_Prompt_API — frontend extension for the LLM Prompt (API) node.
//
// Responsibilities:
//   - Refresh the model_name dropdown when provider / server_url change
//   - Fetch the LIVE model list from the server route (the server holds the
//     API keys; the browser does not), so new provider models appear
//     automatically without editing any hardcoded list
//   - Apply capability filter (text / vision / multimodal) before showing
//   - Show/hide Gemini-only widgets (thinking budget, caching)

import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

// Mirror of the Python-side PROVIDERS table — just enough for live querying.
// Keeps the JS independent of the backend for client-side decisions.
const PROVIDERS = {
    "Gemini": {
        defaultUrl: "https://generativelanguage.googleapis.com/v1beta",
        liveModels: true,
        needsAuth: true,
        envVar: "GEMINI_API_KEY",
        // Current Gemini API models (late 2025 / early 2026) — verified at
        // https://ai.google.dev/gemini-api/docs/models. The live /v1/models
        // query returns the user's full account-accessible list once an API
        // key is set.
        fallback: [
            // Gemini 3 series (current)
            "gemini-3.1-pro-preview",
            "gemini-3.5-flash",
            "gemini-3-flash-preview",
            "gemini-3.1-flash-lite",
            "gemini-3.1-flash-lite-preview",
            // Gemini 2.5 (legacy, still supported)
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ],
    },
    "Grok (xAI)": {
        defaultUrl: "https://api.x.ai/v1",
        liveModels: true,
        needsAuth: true,
        envVar: "XAI_API_KEY",
        fallback: [
            "grok-4.3",
            "grok-4.20-0309-reasoning",
            "grok-4.20-0309-non-reasoning",
            "grok-build-0.1",
            "grok-3",
        ],
    },
    "Custom": {
        defaultUrl: "",
        liveModels: true,
        needsAuth: false,
        envVar: null,
        fallback: ["<set server_url and refresh>"],
    },
};

// Patterns mirroring the Python-side classification — keep these in sync.
const NON_CHAT_PATTERN = /(embed|whisper|tts|audio|imagen|image-gen|dall-e|dalle|gpt-image|veo|video-gen|imagine-image|imagine-video|moderation|search|grounding|sora)/i;
const VISION_PATTERN = /(vision|vl|multimodal|gemini|gpt-4o|gpt-4-turbo|gpt-4\.|grok-4|grok-2-vision|o1|o3|claude-3|llava|moondream|cogvlm|qwen.?vl|qwen2.?5.?vl|qwen3.?vl|intern.?vl|pixtral|paligemma|gemma)/i;
const MULTIMODAL_PATTERN = /(multimodal|gemini-2|gemini-3|gpt-4o-audio|video|audio.?input)/i;

function classifyCapability(modelId) {
    const n = String(modelId || "").toLowerCase();
    if (MULTIMODAL_PATTERN.test(n)) return "multimodal";
    if (VISION_PATTERN.test(n)) return "vision";
    return "text";
}

function shouldShow(modelId, filterMode) {
    if (NON_CHAT_PATTERN.test(modelId)) return false;  // always hide non-chat
    if (filterMode === "all") return true;
    const cap = classifyCapability(modelId);
    if (filterMode === "text only") return cap === "text";
    if (filterMode === "vision") return cap === "vision" || cap === "multimodal";
    if (filterMode === "multimodal") return cap === "multimodal";
    return true;
}

// Ask the SERVER for the live model list. The server route runs
// _get_models_for_provider() with the API key resolved from env / .env, so
// this returns the provider's current models (Gemini native, Grok & Custom
// via OpenAI-compatible /v1/models). Returns [] on any failure.
async function fetchServerModels(provider, serverUrl) {
    try {
        const params = new URLSearchParams({ provider: provider || "" });
        if (serverUrl) params.set("server_url", serverUrl);
        const resp = await api.fetchApi(`/llm_prompt_api/models?${params.toString()}`);
        if (!resp.ok) return [];
        const json = await resp.json();
        return Array.isArray(json?.models) ? json.models : [];
    } catch (e) {
        return [];
    }
}

// Replace the model_name widget's options list.
//
// preserveValue is the difference between a workflow LOAD and a user changing
// provider. On load the saved model must survive even if it belongs to another
// provider — the Python side validates against a superset of every provider's
// models, so keeping it is always safe, and resetting it silently corrupts the
// saved workflow. On a deliberate provider change, resetting is what the user
// expects.
function updateModelDropdown(node, options, preserveValue = false) {
    const w = node.widgets?.find((widget) => widget.name === "model_name");
    if (!w) return;

    let values = options;
    const current = w.value;

    if (preserveValue && current && !values.includes(current)) {
        values = [current, ...values];
    }

    if (w.options) {
        w.options.values = values;
    } else {
        w.options = { values: values };
    }

    if (!preserveValue && values.length > 0 && !values.includes(w.value)) {
        w.value = values[0];
    }

    node.setDirtyCanvas(true, true);
}

async function refreshModels(node, preserveValue = false) {
    const providerW = node.widgets?.find((w) => w.name === "provider");
    const urlW = node.widgets?.find((w) => w.name === "server_url");
    const filterW = node.widgets?.find((w) => w.name === "model_filter");
    if (!providerW) return;

    const provider = providerW.value;
    const cfg = PROVIDERS[provider];
    if (!cfg) return;

    const serverUrl = urlW?.value?.trim() || "";
    const filterMode = filterW?.value || "all";

    // Generation token — the fix for the "model resets to another provider's
    // model when switching windows/tabs" bug.
    //
    // Several refreshes can be in flight at once (onNodeCreated fires one before
    // saved values are restored, onConfigure fires another after). They await a
    // network fetch, so the LAST one to RESOLVE wins — not the last one started.
    // Backgrounded tabs throttle fetches, which is why switching windows made a
    // stale refresh land last and overwrite the loaded model with values[0] of
    // the DEFAULT provider. Stamp each call and drop any result that has been
    // superseded.
    node.__llmRefreshGen = (node.__llmRefreshGen || 0) + 1;
    const gen = node.__llmRefreshGen;

    // The browser has no API keys (env / .env live on the server), so we ask
    // the server route for the live list. It runs _get_models_for_provider()
    // with credentials and returns the provider's current models — that's what
    // makes new Gemini / Grok models show up automatically.
    let source = "server route (live)";
    let all = await fetchServerModels(provider, serverUrl);
    if (gen !== node.__llmRefreshGen) return;   // superseded by a newer refresh
    if (all.length === 0) {
        // Route unreachable (server not up, no key, offline) — show the
        // hardcoded snapshot so the dropdown is never empty.
        all = [...cfg.fallback];
        source = `fallback (server route unavailable — server resolves ${cfg.envVar || "server_url"} from env / .env)`;
    }

    const filtered = all.filter((m) => shouldShow(m, filterMode));
    const final = filtered.length > 0 ? filtered : all;

    updateModelDropdown(node, final, preserveValue);
    console.log(
        `[LLM_Prompt_API] ${provider}: ${final.length}/${all.length} models | source: ${source} | filter: ${filterMode}`
    );
}

// Show/hide a widget by manipulating its computeSize / type. ComfyUI doesn't
// have a first-class "hide" API for built-in widgets, but setting computeSize
// to return [0, -4] effectively collapses the widget row.
//
// Critical: only stash/restore when state actually changes. A naive
// "always restore on show" would set computeSize = null for widgets that
// were never hidden, which silently breaks their rendering.
function setWidgetVisible(widget, visible) {
    if (!widget) return;
    const isHidden = widget._origComputeSize !== undefined;
    if (visible && isHidden) {
        widget.computeSize = widget._origComputeSize;
        widget.type = widget._origType;
        delete widget._origComputeSize;
        delete widget._origType;
    } else if (!visible && !isHidden) {
        widget._origComputeSize = widget.computeSize;
        widget._origType = widget.type;
        widget.computeSize = () => [0, -4];
        widget.type = "hidden";
    }
    // Otherwise: already in the desired state, do nothing
}

function updateProviderSpecificVisibility(node) {
    const providerW = node.widgets?.find((w) => w.name === "provider");
    const isGemini = providerW?.value === "Gemini";

    // Gemini-only widgets: visible only when Gemini is the provider.
    const thinkingBudget = node.widgets?.find((w) => w.name === "gemini_thinking_budget");
    setWidgetVisible(thinkingBudget, isGemini);
    const enableCaching = node.widgets?.find((w) => w.name === "enable_caching");
    setWidgetVisible(enableCaching, isGemini);

    node.setDirtyCanvas(true, true);
}

app.registerExtension({
    name: "LLM_Prompt.APIRefresh",

    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        if (nodeData.name !== "LLMPromptAPI") return;

        const onCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onCreated?.apply(this, arguments);
            const node = this;

            // Initial refresh — populate dropdown with the default provider's
            // models. preserveValue=true because on a workflow LOAD this fires
            // BEFORE the saved values are restored: if it were allowed to reset,
            // a late-resolving fetch would clobber the loaded model. For a
            // genuinely new node there is nothing to preserve, so it costs
            // nothing. Belt-and-braces with the generation token in
            // refreshModels().
            refreshModels(node, true);
            updateProviderSpecificVisibility(node);

            // Wire up callbacks on provider / server_url / model_filter
            const watch = (name, callback) => {
                const w = node.widgets?.find((widget) => widget.name === name);
                if (!w) return;
                const orig = w.callback;
                w.callback = function (value) {
                    if (typeof orig === "function") {
                        try { orig.apply(this, arguments); } catch (e) { /* ignore */ }
                    }
                    callback(value);
                };
            };

            watch("provider", () => {
                refreshModels(node);
                updateProviderSpecificVisibility(node);
            });
            watch("server_url", () => refreshModels(node));
            watch("model_filter", () => refreshModels(node));

            return r;
        };

        // onNodeCreated fires BEFORE ComfyUI restores saved widget values, so
        // the refresh it kicks off reads the DEFAULT provider and resolves
        // after the restore — overwriting the saved model with one from the
        // wrong provider. onConfigure runs after the values are in place, so
        // refresh again here, preserving what was loaded.
        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            const r = onConfigure?.apply(this, arguments);
            const node = this;
            refreshModels(node, true);
            updateProviderSpecificVisibility(node);
            return r;
        };
    },
});
