// LLM_Prompt_API — frontend extension for the LLM Prompt (API) node.
//
// Responsibilities:
//   - Refresh the model_name dropdown when provider / server_url change
//   - Live-query /v1/models for LM Studio, Gemini, Custom (cached 60s)
//   - Apply capability filter (text / vision / multimodal) before showing
//   - Show "Unload Now" button only when LM Studio is selected
//   - Health-check indicator (green/red dot) for LM Studio reachability

import { app } from "/scripts/app.js";

// Mirror of the Python-side PROVIDERS table — just enough for live querying.
// Keeps the JS independent of the backend for client-side decisions.
const PROVIDERS = {
    "LM Studio (local)": {
        defaultUrl: "http://localhost:1234/v1",
        liveModels: true,
        needsAuth: false,
        envVar: null,
        fallback: ["<no model loaded — load one in LM Studio>"],
    },
    "Gemini": {
        defaultUrl: "https://generativelanguage.googleapis.com/v1beta/openai",
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
        liveModels: false,
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

// Live-query a /v1/models endpoint with optional auth.
async function fetchLiveModels(baseUrl, apiKey) {
    if (!baseUrl) return [];
    try {
        const url = baseUrl.replace(/\/$/, "") + "/models";
        const headers = { "Accept": "application/json" };
        if (apiKey) headers["Authorization"] = `Bearer ${apiKey}`;
        const resp = await fetch(url, {
            method: "GET", headers,
            signal: AbortSignal.timeout(3000),
        });
        if (!resp.ok) return [];
        const json = await resp.json();
        const data = json?.data;
        if (!Array.isArray(data)) return [];
        return data
            .map((e) => e?.id || e?.name)
            .filter((s) => typeof s === "string" && s);
    } catch (e) {
        return [];
    }
}

// Replace the model_name widget's options list, keeping the current value
// selected when possible.
function updateModelDropdown(node, options) {
    const w = node.widgets?.find((widget) => widget.name === "model_name");
    if (!w) return;

    if (Array.isArray(w.options?.values)) {
        w.options.values = options;
    } else if (w.options) {
        w.options.values = options;
    } else {
        w.options = { values: options };
    }

    if (options.length > 0 && !options.includes(w.value)) {
        w.value = options[0];
    }

    node.setDirtyCanvas(true, true);
}

async function refreshModels(node) {
    const providerW = node.widgets?.find((w) => w.name === "provider");
    const urlW = node.widgets?.find((w) => w.name === "server_url");
    const filterW = node.widgets?.find((w) => w.name === "model_filter");
    if (!providerW) return;

    const provider = providerW.value;
    const cfg = PROVIDERS[provider];
    if (!cfg) return;

    const baseUrl = (urlW?.value?.trim() || cfg.defaultUrl).replace(/\/$/, "");
    const filterMode = filterW?.value || "all";

    // We never have the API key in the browser — it lives in os.environ or
    // .env on the ComfyUI server side. So for providers that require auth,
    // the browser-side live query is expected to fail; the dropdown is
    // populated from the Python-side INPUT_TYPES instead (which DOES have
    // env access). Show the fallback list as a sensible default here.
    let all = [];
    let source = "fallback";
    if (cfg.liveModels && baseUrl && !cfg.needsAuth) {
        all = await fetchLiveModels(baseUrl, null);
        if (all.length > 0) source = "live /v1/models";
    }
    if (all.length === 0) {
        all = [...cfg.fallback];
        if (cfg.needsAuth) {
            source = `fallback (browser has no key — server-side dropdown uses ${cfg.envVar} from env or .env)`;
        } else {
            source = "fallback (live query failed or returned empty)";
        }
    }

    const filtered = all.filter((m) => shouldShow(m, filterMode));
    const final = filtered.length > 0 ? filtered : all;

    updateModelDropdown(node, final);
    console.log(
        `[LLM_Prompt_API] ${provider}: ${final.length}/${all.length} models | source: ${source} | filter: ${filterMode}`
    );
}

// LM Studio health check — pings /v1/models with a tiny timeout
async function checkLMStudioHealth(baseUrl) {
    try {
        const resp = await fetch(baseUrl.replace(/\/$/, "") + "/models", {
            method: "GET",
            signal: AbortSignal.timeout(1500),
        });
        return resp.ok;
    } catch (e) {
        return false;
    }
}

// Add a small text widget that shows the LM Studio status
function addHealthIndicator(node) {
    // We use a non-interactive label-style widget — just for display
    const w = node.addCustomWidget?.({
        name: "lm_studio_status",
        type: "info",
        value: "",
        draw(ctx, node, widget_width, y, widget_height) {
            // ComfyUI doesn't need this drawn — we'll update via title overlay
        },
    });
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

// Add a JS-side "Unload Now" button, only visible when LM Studio is the provider.
function addUnloadButton(node) {
    const button = node.addWidget("button", "Unload Now (LM Studio)", null, async () => {
        const providerW = node.widgets?.find((w) => w.name === "provider");
        const urlW = node.widgets?.find((w) => w.name === "server_url");
        const modelW = node.widgets?.find((w) => w.name === "model_name");

        if (providerW?.value !== "LM Studio (local)") {
            alert("Unload only works for LM Studio.");
            return;
        }

        const baseUrl = (urlW?.value?.trim() || PROVIDERS["LM Studio (local)"].defaultUrl).replace(/\/$/, "");
        const modelName = modelW?.value || "";

        try {
            const resp = await fetch("/llm_prompt_api/lmstudio_unload", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ base_url: baseUrl, model_name: modelName }),
            });
            const result = await resp.json();
            if (result.ok) {
                console.log(`[LLM_Prompt_API] ${result.message}`);
                // Briefly flash a notification — ComfyUI's app.ui.dialog
                if (app.ui?.dialog?.show) {
                    app.ui.dialog.show(`Unloaded: ${result.message}`);
                    setTimeout(() => app.ui.dialog.close?.(), 2000);
                }
            } else {
                console.warn(`[LLM_Prompt_API] Unload failed: ${result.message}`);
                alert(`Unload failed:\n${result.message}`);
            }
        } catch (e) {
            console.error("[LLM_Prompt_API] Unload request failed:", e);
            alert(`Unload request failed: ${e.message}`);
        }
    });
    button._isUnloadButton = true;
    return button;
}

function updateProviderSpecificVisibility(node) {
    const providerW = node.widgets?.find((w) => w.name === "provider");
    const provider = providerW?.value;
    const isLM = provider === "LM Studio (local)";
    const isGemini = provider === "Gemini";

    // Show/hide the Unload Now button
    const unloadBtn = node.widgets?.find((w) => w._isUnloadButton);
    setWidgetVisible(unloadBtn, isLM);

    // Show/hide LM-Studio-only widgets
    const keepLoaded = node.widgets?.find((w) => w.name === "keep_model_loaded");
    setWidgetVisible(keepLoaded, isLM);
    const unloadAfterRun = node.widgets?.find((w) => w.name === "unload_after_run");
    setWidgetVisible(unloadAfterRun, isLM);

    // Show/hide Gemini-only widgets
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

            // Add the Unload Now button
            addUnloadButton(node);

            // Initial refresh — populate dropdown with the default provider's models
            refreshModels(node);
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
    },
});
