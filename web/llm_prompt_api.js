// LLM_Prompt_API — refresh the model dropdown when the provider changes,
// and live-query the chosen endpoint for available models.
//
// ComfyUI's built-in INPUT_TYPES dropdown is static once the node is created,
// so this extension dynamically updates the model_name widget's options list
// based on the selected provider.

import { app } from "/scripts/app.js";

// Mirror of the Python-side PROVIDERS table. Frontend uses this for the URL
// hint and to decide whether to live-query the endpoint.
const PROVIDERS = {
    "LM Studio (local)": {
        defaultUrl: "http://localhost:1234/v1",
        liveModels: true,
        needsAuth: false,
        fallback: ["<no model loaded — load one in LM Studio>"],
    },
    "Gemini": {
        defaultUrl: "https://generativelanguage.googleapis.com/v1beta/openai",
        liveModels: false,
        needsAuth: true,
        fallback: [
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
        ],
    },
    "Grok (xAI)": {
        defaultUrl: "https://api.x.ai/v1",
        liveModels: false,
        needsAuth: true,
        fallback: ["grok-4", "grok-4-fast", "grok-3", "grok-3-mini", "grok-2-vision", "grok-2"],
    },
    "OpenAI": {
        defaultUrl: "https://api.openai.com/v1",
        liveModels: false,
        needsAuth: true,
        fallback: ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "o1", "o1-mini", "o3-mini"],
    },
    "OpenRouter": {
        defaultUrl: "https://openrouter.ai/api/v1",
        liveModels: false,
        needsAuth: true,
        fallback: [
            "anthropic/claude-3.5-sonnet",
            "openai/gpt-4o",
            "google/gemini-2.5-pro",
            "meta-llama/llama-3.3-70b-instruct",
            "qwen/qwen-2.5-72b-instruct",
        ],
    },
    "Custom": {
        defaultUrl: "",
        liveModels: true,
        needsAuth: false,
        fallback: ["<set server_url and refresh>"],
    },
};

// Live-query /v1/models from the browser. Same shape as the Python helper.
async function fetchLiveModels(baseUrl, apiKey) {
    if (!baseUrl) return [];
    try {
        const url = baseUrl.replace(/\/$/, "") + "/models";
        const headers = { "Accept": "application/json" };
        if (apiKey) headers["Authorization"] = `Bearer ${apiKey}`;
        const resp = await fetch(url, { method: "GET", headers, signal: AbortSignal.timeout(2500) });
        if (!resp.ok) return [];
        const json = await resp.json();
        const data = json?.data;
        if (!Array.isArray(data)) return [];
        return data.map((e) => e?.id || e?.name).filter((s) => typeof s === "string" && s);
    } catch (e) {
        return [];
    }
}

// Replace the model_name widget's options list and select a default.
function updateModelDropdown(node, newOptions, selectIfPossible) {
    const w = node.widgets?.find((widget) => widget.name === "model_name");
    if (!w) return;

    // The combo widget stores its options in different places depending on
    // ComfyUI version. Cover the common shapes.
    if (Array.isArray(w.options?.values)) {
        w.options.values = newOptions;
    } else if (w.options) {
        w.options.values = newOptions;
    } else {
        w.options = { values: newOptions };
    }

    // Pick a sensible value: prefer the current value if still valid, else first.
    if (selectIfPossible && newOptions.includes(selectIfPossible)) {
        w.value = selectIfPossible;
    } else if (newOptions.length > 0) {
        w.value = newOptions[0];
    }

    node.setDirtyCanvas(true, true);
}

async function refreshModelsForProvider(node) {
    const providerWidget = node.widgets?.find((w) => w.name === "provider");
    const urlWidget = node.widgets?.find((w) => w.name === "server_url");
    const keyWidget = node.widgets?.find((w) => w.name === "api_key");
    if (!providerWidget) return;

    const provider = providerWidget.value;
    const cfg = PROVIDERS[provider];
    if (!cfg) return;

    const baseUrl = (urlWidget?.value?.trim() || cfg.defaultUrl).replace(/\/$/, "");
    const apiKey = keyWidget?.value?.trim() || "";

    let options = [];
    if (cfg.liveModels && baseUrl) {
        const live = await fetchLiveModels(baseUrl, apiKey);
        if (live.length > 0) {
            options = live;
        }
    }
    if (options.length === 0) {
        options = [...cfg.fallback];
    }

    const currentModel = node.widgets?.find((w) => w.name === "model_name")?.value;
    updateModelDropdown(node, options, currentModel);
    console.log(
        `[LLM_Prompt_API] ${provider}: ${options.length} model(s) available`
    );
}

app.registerExtension({
    name: "LLM_Prompt.APIProviderRefresh",

    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        if (nodeData.name !== "LLMPromptAPI") return;

        const onCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onCreated?.apply(this, arguments);
            const node = this;

            // Refresh once at node-create so the dropdown is populated with
            // the live LM Studio list (when the default provider is LM Studio).
            refreshModelsForProvider(node);

            // Hook the provider widget: when it changes, refresh.
            const providerWidget = node.widgets?.find((w) => w.name === "provider");
            if (providerWidget) {
                const orig = providerWidget.callback;
                providerWidget.callback = function (value) {
                    if (typeof orig === "function") {
                        try { orig.apply(this, arguments); } catch (e) { /* ignore */ }
                    }
                    refreshModelsForProvider(node);
                };
            }

            // Hook the server_url widget too: refresh after the URL changes
            // (useful for Custom provider).
            const urlWidget = node.widgets?.find((w) => w.name === "server_url");
            if (urlWidget) {
                const orig = urlWidget.callback;
                urlWidget.callback = function (value) {
                    if (typeof orig === "function") {
                        try { orig.apply(this, arguments); } catch (e) { /* ignore */ }
                    }
                    refreshModelsForProvider(node);
                };
            }

            return r;
        };
    },
});
