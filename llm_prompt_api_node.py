"""LLM Prompt (API) — ComfyUI node for prompt generation via remote/local OpenAI-compatible endpoints.

Supported providers:
  - LM Studio (local server)
  - Gemini (Google's OpenAI-compatible endpoint)
  - Grok (xAI)
  - Custom (arbitrary OpenAI-compatible endpoint)

Design principles:
  - No GGUF loading, no chat template hell, no KV cache mgmt — the upstream
    server handles all of that.
  - Live-query /v1/models when possible (LM Studio, Gemini, Custom).
  - Grok has no /models endpoint per docs, so we keep a small curated list.
  - Per-provider parameter sanitization — strip params the provider rejects.
  - Cache-friendly prompt assembly: stable parts (style, canvas) first,
    variable parts (user_prompt) last, so prompt-caching prefix matches.
  - Forced JSON mode at the API level when output_format=json.
  - Gemini-specific features (thinking_budget, cached_content, safety) routed
    via extra_body in the OpenAI-compat layer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.request
import urllib.error
from typing import Any

# Reuse helpers from the sister GGUF node
from .llm_prompt_node import (
    load_system_prompts,
    _build_canvas_profile,
    _tensor_to_base64_png,
    _sample_video_frames,
)
from .output_cleaner import OutputCleanConfig, clean_model_output


# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------

PROVIDERS = {
    "LM Studio (local)": {
        "base_url": "http://localhost:1234/v1",
        "env_var": None,
        "needs_auth": False,
        "live_models": True,
        "fallback_models": ["<no model loaded — load one in LM Studio>"],
    },
    "Gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "env_var": "GEMINI_API_KEY",
        "needs_auth": True,
        "live_models": True,
        # Last-resort fallback when /models fails (e.g. no API key set yet)
        "fallback_models": [
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash",
        ],
    },
    "Grok (xAI)": {
        "base_url": "https://api.x.ai/v1",
        "env_var": "XAI_API_KEY",
        "needs_auth": True,
        # No /models endpoint per xAI docs — always use the curated list
        "live_models": False,
        "fallback_models": [
            "grok-4.3",
            "grok-4.20-0309-reasoning",
            "grok-4.20-0309-non-reasoning",
            "grok-build-0.1",
            "grok-3",
        ],
    },
    "Custom": {
        "base_url": "",  # user supplies via the server_url widget
        "env_var": None,
        "needs_auth": False,
        "live_models": True,
        "fallback_models": ["<set server_url and refresh>"],
    },
}


# Patterns to identify NON-chat models we should hide from the dropdown
# (embeddings, image-gen, video-gen, TTS, STT — they aren't useful here).
_NON_CHAT_PATTERNS = re.compile(
    r"(embed|whisper|tts|audio|imagen|image-gen|dall-e|dalle|gpt-image|veo|"
    r"video-gen|imagine-image|imagine-video|moderation|search|grounding|sora)",
    re.IGNORECASE,
)

# Per-provider name patterns that indicate vision capability.
# Used when the /models response doesn't include a structured modality field.
_VISION_PATTERNS = re.compile(
    r"(vision|vl|multimodal|gemini|gpt-4o|gpt-4-turbo|gpt-4\.|grok-4|grok-2-vision|"
    r"o1|o3|claude-3|llava|moondream|cogvlm|qwen.?vl|qwen2.?5.?vl|qwen3.?vl|"
    r"intern.?vl|pixtral|paligemma|gemma)",
    re.IGNORECASE,
)

# Pattern for explicit multimodal hint (video / audio input beyond just images).
_MULTIMODAL_PATTERNS = re.compile(
    r"(multimodal|gemini-2|gemini-3|gpt-4o-audio|video|audio.?input)",
    re.IGNORECASE,
)


def _classify_capability(model_id: str, modality_hint: str = "") -> str:
    """Classify a model as 'text', 'vision', or 'multimodal'.

    modality_hint can be passed by OpenRouter-style metadata (e.g.
    'text+image+video->text'). When given, that field takes priority.
    """
    if modality_hint:
        m = modality_hint.lower()
        modality_count = sum(1 for token in ("image", "video", "audio") if token in m)
        if modality_count >= 2:
            return "multimodal"
        if "image" in m or "vision" in m:
            return "vision"
        return "text"

    name = model_id.lower()
    if _MULTIMODAL_PATTERNS.search(name):
        return "multimodal"
    if _VISION_PATTERNS.search(name):
        return "vision"
    return "text"


# Cache (provider, base_url) -> list of model name strings.
_MODEL_LIST_CACHE: dict[tuple[str, str], list[str]] = {}
_MODEL_LIST_CACHE_TS: dict[tuple[str, str], float] = {}
_MODEL_CACHE_TTL_SECONDS = 60


def _fetch_models_from_server(base_url: str, api_key: str | None) -> list[str]:
    """Query /v1/models. Returns the list of model id strings.

    Silently returns [] on any failure (network down, auth missing, etc.).
    """
    if not base_url:
        return []
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, method="GET")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError):
        return []
    except Exception:
        return []

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    names: list[str] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("id") or entry.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _get_models_for_provider(
    provider: str,
    custom_url: str = "",
    api_key_override: str = "",
) -> list[str]:
    """Live-query the provider's /v1/models (or use the curated fallback).

    Filters out non-chat models (embeddings, image-gen, TTS, etc.) before
    returning. Capability-level filtering (vision/multimodal/text-only) is
    done at the dropdown rendering step by the JS extension.
    """
    cfg = PROVIDERS.get(provider)
    if not cfg:
        return ["<unknown provider>"]

    base_url = custom_url.strip() if provider == "Custom" else cfg["base_url"]
    cache_key = (provider, base_url)
    now = time.time()
    cached_ts = _MODEL_LIST_CACHE_TS.get(cache_key, 0)
    if (now - cached_ts) < _MODEL_CACHE_TTL_SECONDS:
        cached = _MODEL_LIST_CACHE.get(cache_key)
        if cached:
            return cached

    api_key = _resolve_api_key(provider, api_key_override)

    raw_models: list[str] = []
    if cfg.get("live_models") and base_url:
        raw_models = _fetch_models_from_server(base_url, api_key)

    if not raw_models:
        raw_models = list(cfg.get("fallback_models") or [])

    # Filter out non-chat models (embeddings, image-gen, TTS, etc.)
    filtered = [m for m in raw_models if not _NON_CHAT_PATTERNS.search(m)]
    if not filtered:
        # If the filter ate everything (rare), keep the raw list so user can see something
        filtered = raw_models

    # Sort: keep stable order from the API, just dedupe
    seen = set()
    deduped = []
    for m in filtered:
        if m not in seen:
            seen.add(m)
            deduped.append(m)

    _MODEL_LIST_CACHE[cache_key] = deduped
    _MODEL_LIST_CACHE_TS[cache_key] = now
    return deduped


def _resolve_api_key(provider: str, override_key: str = "") -> str | None:
    if override_key and override_key.strip():
        return override_key.strip()
    cfg = PROVIDERS.get(provider, {})
    env_var = cfg.get("env_var")
    if env_var:
        v = os.environ.get(env_var)
        if v:
            return v.strip()
    return None


def _resolve_base_url(provider: str, custom_url: str = "") -> str:
    if custom_url and custom_url.strip():
        return custom_url.strip().rstrip("/")
    cfg = PROVIDERS.get(provider, {})
    return str(cfg.get("base_url") or "").rstrip("/")


# ---------------------------------------------------------------------------
# Per-provider request sanitization
# ---------------------------------------------------------------------------

def _sanitize_payload_for_provider(provider: str, payload: dict) -> dict:
    """Strip / rename parameters the provider rejects.

    Returns a NEW dict — does not mutate input.
    """
    p = dict(payload)

    if provider == "Grok (xAI)":
        # Grok rejects llama.cpp-style sampling params
        for k in ("top_k", "min_p", "repetition_penalty", "repeat_penalty"):
            p.pop(k, None)
        # Grok uses max_completion_tokens, not max_tokens
        if "max_tokens" in p:
            p["max_completion_tokens"] = p.pop("max_tokens")

    elif provider == "Gemini":
        # Gemini's OpenAI-compat layer accepts standard params at top level,
        # but Gemini-specific config (top_k, thinking_config, safety_settings,
        # cached_content) goes through extra_body.google.
        extra_body = p.pop("extra_body", {}) or {}
        google_cfg = extra_body.get("google", {}) or {}
        # Route top_k through extra_body if set
        if "top_k" in p:
            top_k_val = p.pop("top_k")
            if top_k_val:  # only if non-zero
                google_cfg["top_k"] = top_k_val
        # Gemini doesn't honor min_p / repetition_penalty
        p.pop("min_p", None)
        p.pop("repetition_penalty", None)
        p.pop("repeat_penalty", None)
        if google_cfg:
            extra_body["google"] = google_cfg
        if extra_body:
            p["extra_body"] = extra_body

    elif provider == "LM Studio (local)":
        # LM Studio accepts everything llama.cpp-style; nothing to strip
        pass

    elif provider == "Custom":
        # Unknown server — be conservative, send everything
        pass

    # Drop zero/no-op params to keep the request clean
    for k in ("top_k", "min_p"):
        if k in p and (p[k] == 0 or p[k] == 0.0):
            p.pop(k)
    if "repetition_penalty" in p and abs(p["repetition_penalty"] - 1.0) < 1e-6:
        p.pop("repetition_penalty")
    if "repeat_penalty" in p and abs(p["repeat_penalty"] - 1.0) < 1e-6:
        p.pop("repeat_penalty")
    if "presence_penalty" in p and abs(p["presence_penalty"]) < 1e-6:
        p.pop("presence_penalty")
    if "frequency_penalty" in p and abs(p["frequency_penalty"]) < 1e-6:
        p.pop("frequency_penalty")
    if "seed" in p and p["seed"] == 0:
        p.pop("seed")

    return p


# ---------------------------------------------------------------------------
# Gemini-specific helpers
# ---------------------------------------------------------------------------

# Hardcoded safety: BLOCK_NONE for all categories. Gemini's hard server-side
# filter catches truly forbidden content regardless of this setting; everything
# else should be unblocked for creative prompt-generation work.
_GEMINI_SAFETY_BLOCK_NONE = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]


def _apply_gemini_extras(payload: dict, thinking_budget: int) -> dict:
    """Inject Gemini-specific extra_body settings: safety, thinking_budget."""
    extra_body = payload.get("extra_body") or {}
    google_cfg = extra_body.get("google") or {}

    google_cfg["safety_settings"] = list(_GEMINI_SAFETY_BLOCK_NONE)

    if thinking_budget >= 0:
        google_cfg["thinking_config"] = {"thinking_budget": int(thinking_budget)}

    extra_body["google"] = google_cfg
    payload["extra_body"] = extra_body
    return payload


# Cache: hash of stable prefix -> Gemini cached content resource name + expiry
_GEMINI_CACHE: dict[str, tuple[str, float]] = {}


def _gemini_create_cached_content(
    api_key: str,
    model: str,
    system_text: str,
    stable_user_text: str,
    ttl_seconds: int = 3600,
) -> str | None:
    """Create a Gemini cached content resource. Returns the resource name (cachedContents/...) or None.

    Uses the NATIVE Gemini REST endpoint, not the OpenAI-compat layer.
    Note: cached content must be >=1024 tokens; we estimate roughly via character count.
    """
    if not api_key:
        return None
    # Rough char->token estimate: 4 chars/token. Need ~4096 chars to clear 1024 tokens.
    if (len(system_text) + len(stable_user_text)) < 4096:
        return None  # Too small to be worth caching

    cache_key = hashlib.sha256(
        f"{model}|{system_text}|{stable_user_text}".encode("utf-8")
    ).hexdigest()
    now = time.time()
    if cache_key in _GEMINI_CACHE:
        name, expiry = _GEMINI_CACHE[cache_key]
        if expiry > now + 60:
            return name  # reuse, still valid for >1 minute

    # Gemini native cached-content endpoint
    url = f"https://generativelanguage.googleapis.com/v1beta/cachedContents?key={api_key}"
    body = {
        "model": f"models/{model}",
        "contents": [
            {"role": "user", "parts": [{"text": stable_user_text}]},
        ],
        "systemInstruction": {"parts": [{"text": system_text}]},
        "ttl": f"{int(ttl_seconds)}s",
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
    )
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[LLM_Prompt_API] Gemini cache creation failed: {e}")
        return None

    name = data.get("name")
    if not isinstance(name, str):
        return None
    expiry = now + ttl_seconds - 30  # 30s buffer
    _GEMINI_CACHE[cache_key] = (name, expiry)
    return name


# ---------------------------------------------------------------------------
# HTTP chat completion
# ---------------------------------------------------------------------------

def _send_chat_completion(
    provider: str,
    base_url: str,
    api_key: str | None,
    model: str,
    messages: list[dict],
    sampling: dict,
    output_format: str,
    stop_sequences: list[str],
    timeout: float,
    extra_body: dict | None = None,
) -> str:
    """POST to /v1/chat/completions with per-provider sanitization + retry.

    sampling dict can contain: temperature, top_p, top_k, min_p,
    repetition_penalty, presence_penalty, frequency_penalty, max_tokens, seed.
    """
    if not base_url:
        raise RuntimeError("Empty server URL. Set a provider or fill in server_url.")
    if not model or model.startswith("<"):
        raise RuntimeError(f"Invalid model: {model!r}. Pick a real model from the dropdown.")

    url = base_url.rstrip("/") + "/chat/completions"

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    payload.update(sampling)

    if stop_sequences:
        payload["stop"] = stop_sequences

    if output_format == "json":
        # Forced JSON at the API level — more reliable than instructions
        payload["response_format"] = {"type": "json_object"}

    if extra_body:
        payload["extra_body"] = extra_body

    # Per-provider sanitization
    payload = _sanitize_payload_for_provider(provider, payload)

    body = json.dumps(payload).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Retry once on transient failures (429 rate limit, 503 overloaded)
    last_err: Exception | None = None
    for attempt in range(2):
        req = urllib.request.Request(url, data=body, method="POST")
        for k, v in headers.items():
            req.add_header(k, v)

        start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
            elapsed = max(time.perf_counter() - start, 1e-6)
            break  # success
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt == 0:
                print(f"[LLM_Prompt_API] HTTP {e.code} on attempt {attempt+1}, retrying in 1.5s...")
                time.sleep(1.5)
                last_err = e
                continue
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = ""
            raise RuntimeError(
                f"HTTP {e.code} from {url}: {err_body[:500] or e.reason}"
            ) from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Cannot reach {url}: {e.reason}") from None
        except TimeoutError:
            if attempt == 0:
                print(f"[LLM_Prompt_API] Timeout on attempt {attempt+1}, retrying...")
                last_err = TimeoutError("first attempt timed out")
                continue
            raise RuntimeError(f"Timed out after {timeout}s waiting for {url}") from None
    else:
        # Loop exited without break — both attempts failed
        raise RuntimeError(f"Both attempts failed: {last_err}") from None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Non-JSON response from {url}: {raw[:500]}") from None

    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected response shape from {url}: {raw[:500]}")

    err = data.get("error")
    if isinstance(err, dict):
        raise RuntimeError(f"API error: {err.get('message', err)}")

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"No choices in response: {raw[:500]}")

    first = choices[0] or {}
    msg = first.get("message") or {}
    content = msg.get("content", "")

    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        content = "".join(parts)

    usage = data.get("usage") or {}
    pt = usage.get("prompt_tokens")
    ct = usage.get("completion_tokens")
    cached = usage.get("prompt_tokens_details", {}).get("cached_tokens") if isinstance(usage.get("prompt_tokens_details"), dict) else None
    cache_note = f", cached={cached}" if cached else ""
    print(
        f"[LLM_Prompt_API] {provider} | {model} | "
        f"prompt={pt}, completion={ct}{cache_note}, time={elapsed:.2f}s"
    )

    return str(content or "").strip()


# ---------------------------------------------------------------------------
# LM Studio unload endpoint (called from the JS button)
# ---------------------------------------------------------------------------

def _unload_lm_studio_model(base_url: str, model_name: str = "") -> tuple[bool, str]:
    """Best-effort unload via LM Studio's REST API. Returns (success, message)."""
    if not base_url:
        return False, "No base URL"

    # Strip /v1 if present, then try LM Studio's native unload endpoint
    root = base_url.replace("/v1", "").rstrip("/")
    candidates = [
        (f"{root}/api/v0/models/unload", "POST"),
    ]
    if model_name:
        candidates.append((f"{root}/api/v0/models/{model_name}/unload", "POST"))

    for url, method in candidates:
        body = json.dumps({"identifier": model_name} if model_name else {}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                if 200 <= resp.status < 300:
                    return True, f"Unloaded via {url}"
        except urllib.error.HTTPError as e:
            if e.code in (404, 405):
                continue  # try next candidate
            return False, f"HTTP {e.code}: {e.reason}"
        except Exception as e:
            return False, str(e)
    return False, "No unload endpoint responded (LM Studio's REST API may not be enabled)"


# ---------------------------------------------------------------------------
# Image / video message builders
# ---------------------------------------------------------------------------

def _build_multimodal_user_content(text: str, images_b64: list[str]) -> list[dict]:
    """Build OpenAI-style multimodal user content array (text + image_url blocks)."""
    parts: list[dict] = [{"type": "text", "text": text}]
    for img_b64 in images_b64:
        if not img_b64:
            continue
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{img_b64}"},
        })
    return parts


# ---------------------------------------------------------------------------
# The Node
# ---------------------------------------------------------------------------

class LLMPromptAPINode:
    """Prompt generation via OpenAI-compatible API (LM Studio, Gemini, Grok, Custom).

    Zero llama-cpp-python dependency. Just HTTP.
    """

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("PROMPT",)
    FUNCTION = "generate"
    CATEGORY = "Luna/LLM"

    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(cls):
        prompts = load_system_prompts()
        prompt_names = ["None"] + sorted(prompts.keys())
        providers = list(PROVIDERS.keys())
        default_provider = providers[0]
        model_options = _get_models_for_provider(default_provider) or ["<unavailable>"]

        return {
            "required": {
                "provider": (providers, {
                    "default": default_provider,
                    "tooltip": "Which backend to call. LM Studio = local server on port 1234. Gemini/Grok = cloud APIs (need API key in env var or override field). Custom = arbitrary OpenAI-compatible endpoint.",
                }),
                "model_name": (model_options, {
                    "tooltip": "Available models. LM Studio shows what you have loaded. Gemini fetches your account's accessible models. Grok shows the curated list (no /models endpoint). Refreshes when provider changes.",
                }),
                "server_url": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "Override the provider's default URL. Required for Custom provider. Leave empty to use the provider default (e.g. http://localhost:1234/v1 for LM Studio).",
                }),
                "api_key": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "API key override. Leave empty to use the provider's env var (GEMINI_API_KEY, XAI_API_KEY).",
                }),
                "model_filter": (["all", "text only", "vision", "multimodal"], {
                    "default": "all",
                    "tooltip": "Filter the model dropdown by capability. 'vision' shows models that accept images. 'multimodal' shows models accepting multiple input types (image+video+audio).",
                }),
                "system_prompt": (prompt_names, {
                    "default": prompt_names[0],
                    "tooltip": "System prompt preset loaded from prompts/*.md. Choose None to use only custom_system_prompt.",
                }),
                "custom_system_prompt": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "tooltip": "Override the preset. If non-empty, replaces whatever the preset would have been.",
                }),
                "user_prompt": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "tooltip": "Your scene concept, subject, idea.",
                    "forceInput": False,
                }),
                "output_format": (["text", "json", "list"], {
                    "default": "text",
                    "tooltip": "text = plain prompt paragraph. json = forced JSON output via response_format (server-side enforced). list = numbered multi-scene list (for LTX video tracks).",
                }),
                "max_tokens": ("INT", {
                    "default": 4096,
                    "min": 64,
                    "max": 32000,
                    "tooltip": "Maximum tokens to generate. 4096 handles single prompts and short multi-scene lists. Raise for long LTX video tracks.",
                }),
                "temperature": ("FLOAT", {
                    "default": 0.7,
                    "min": 0.0,
                    "max": 2.0,
                    "step": 0.05,
                    "tooltip": "Sampling randomness. Lower = more deterministic, higher = more creative. Auto-fills when you pick a model from a known family.",
                }),
                "top_p": ("FLOAT", {
                    "default": 0.9,
                    "min": 0.0,
                    "max": 1.0,
                    "step": 0.05,
                    "tooltip": "Nucleus sampling. Common values: 0.8 (Qwen instruct), 0.95 (Gemma 4 / thinking models). Auto-fills with the model preset.",
                }),
                "top_k": ("INT", {
                    "default": 0,
                    "min": 0,
                    "max": 200,
                    "tooltip": "Top-K sampling. 0 = disabled. Qwen wants 20, Gemma 4 wants 64. Grok and OpenAI reject this parameter — it gets stripped automatically.",
                }),
                "min_p": ("FLOAT", {
                    "default": 0.0,
                    "min": 0.0,
                    "max": 1.0,
                    "step": 0.01,
                    "tooltip": "Min-P sampling. 0.0 = disabled (the Unsloth default for both Qwen and Gemma). Some cloud APIs reject this — gets stripped automatically.",
                }),
                "presence_penalty": ("FLOAT", {
                    "default": 0.0,
                    "min": -2.0,
                    "max": 2.0,
                    "step": 0.1,
                    "tooltip": "Discourages reusing tokens (encourages diversity). Unsloth recommends 1.5 for Qwen instruct mode. 0.0 for Gemma 4.",
                }),
                "frequency_penalty": ("FLOAT", {
                    "default": 0.0,
                    "min": -2.0,
                    "max": 2.0,
                    "step": 0.1,
                    "tooltip": "Discourages repeating frequent tokens. 0.0 = disabled.",
                }),
                "repetition_penalty": ("FLOAT", {
                    "default": 1.0,
                    "min": 0.5,
                    "max": 2.0,
                    "step": 0.05,
                    "tooltip": "Repetition penalty. 1.0 = disabled (Google recommendation). Mostly meaningful for LM Studio; cloud APIs typically ignore or reject it.",
                }),
                "seed": ("INT", {
                    "default": 0,
                    "min": 0,
                    "max": 2**32 - 1,
                    "tooltip": "Random seed. 0 = non-deterministic (the provider picks). Same seed + same prompt + same params = same output (cloud APIs treat this as best-effort).",
                }),
                "stop_sequences": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "Comma-separated strings that stop generation when emitted. Example: 'END,---,EOF'. Leave empty for default behavior.",
                }),
                "gemini_thinking_budget": ("INT", {
                    "default": 0,
                    "min": 0,
                    "max": 24576,
                    "tooltip": "Gemini only. Tokens the model spends on internal reasoning before answering. 0 = no thinking (fastest, cheapest, recommended for prompt gen). Max 24576 for flash models. Ignored for non-Gemini providers.",
                }),
                "enable_caching": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "Gemini only. Caches the stable prefix (system + style + canvas) to cut cost when generating multiple variations of the same character/scene. Requires the prefix to be at least ~1024 tokens.",
                }),
                "unload_after_run": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "LM Studio only. POST an unload request after generation to free VRAM. Silently ignored for other providers.",
                }),
                "timeout_seconds": ("INT", {
                    "default": 120,
                    "min": 5,
                    "max": 600,
                    "tooltip": "HTTP timeout for the chat call. Cloud APIs can be slow at peak times — raise to 180+ for big reasoning models like grok-4.20-reasoning.",
                }),
            },
            "optional": {
                "style": ("STRING", {"forceInput": True}),
                "width": ("INT", {"forceInput": True}),
                "height": ("INT", {"forceInput": True}),
                "image": ("IMAGE", {"forceInput": True}),
                "video": ("VIDEO", {"forceInput": True}),
            },
        }

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def generate(
        self,
        provider: str,
        model_name: str,
        server_url: str,
        api_key: str,
        model_filter: str,
        system_prompt: str,
        custom_system_prompt: str,
        user_prompt: str,
        output_format: str,
        max_tokens: int,
        temperature: float,
        top_p: float,
        top_k: int,
        min_p: float,
        presence_penalty: float,
        frequency_penalty: float,
        repetition_penalty: float,
        seed: int,
        stop_sequences: str,
        gemini_thinking_budget: int,
        enable_caching: bool,
        unload_after_run: bool,
        timeout_seconds: int,
        style: str = "",
        width: int = 0,
        height: int = 0,
        image=None,
        video=None,
    ):
        cfg = PROVIDERS.get(provider)
        if not cfg:
            raise RuntimeError(f"Unknown provider: {provider!r}")

        base_url = _resolve_base_url(provider, server_url)
        resolved_key = _resolve_api_key(provider, api_key)
        if cfg.get("needs_auth") and not resolved_key:
            env_var = cfg.get("env_var") or "<none>"
            raise RuntimeError(
                f"{provider} requires an API key. Set the api_key field on the "
                f"node, or export the {env_var} environment variable before "
                f"starting ComfyUI."
            )

        # Resolve system prompt
        if custom_system_prompt and custom_system_prompt.strip():
            sys_prompt = custom_system_prompt.strip()
        elif system_prompt and system_prompt != "None":
            prompts = load_system_prompts()
            sys_prompt = prompts.get(system_prompt, "")
        else:
            sys_prompt = ""

        # Output-format directive (light hint; JSON is also forced at API level via response_format)
        if output_format == "json":
            sys_prompt += (
                "\n\nReturn only the final prompt as a valid JSON object. "
                "No preface, no explanations, no markdown fences."
            )
        elif output_format == "list":
            sys_prompt += (
                "\n\nOutput only the numbered list as specified. "
                "No preface, no explanations, no markdown fences. "
                "Keep every item on its own numbered line."
            )
        elif sys_prompt:
            sys_prompt += (
                "\n\nReturn only the final prompt text. "
                "No preface, no explanations, no JSON, no markdown fences."
            )

        # Build user prompt — STABLE FIRST, VARIABLE LAST for cache friendliness.
        # Order: STYLE (rarely changes) -> CANVAS (rarely changes) -> USER REQUEST (varies)
        stable_parts: list[str] = []
        if style and style.strip():
            stable_parts.append(f"STYLE:\n{style.strip()}")
        if width > 0 and height > 0:
            canvas_block = _build_canvas_profile(width, height)
            if canvas_block:
                stable_parts.append(canvas_block)

        variable_parts: list[str] = []
        if user_prompt and user_prompt.strip():
            variable_parts.append(f"USER REQUEST:\n{user_prompt.strip()}")

        all_parts = stable_parts + variable_parts
        final_user_prompt = "\n\n".join(all_parts) if all_parts else "Describe a scene vividly."

        # Stable text for Gemini caching purposes (system + stable user portion)
        stable_user_text = "\n\n".join(stable_parts)

        # Vision content
        images_b64: list[str] = []
        if image is not None:
            for i in range(image.shape[0]):
                img = _tensor_to_base64_png(image[i])
                if img:
                    images_b64.append(img)
        if video is not None:
            for frame in _sample_video_frames(video, 16):
                img = _tensor_to_base64_png(frame)
                if img:
                    images_b64.append(img)

        # Build messages array
        messages: list[dict] = []
        if sys_prompt:
            messages.append({"role": "system", "content": sys_prompt})

        if images_b64:
            user_content = _build_multimodal_user_content(final_user_prompt, images_b64)
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": final_user_prompt})

        # Build sampling dict
        sampling = {
            "temperature": float(temperature),
            "top_p": float(top_p),
            "top_k": int(top_k),
            "min_p": float(min_p),
            "presence_penalty": float(presence_penalty),
            "frequency_penalty": float(frequency_penalty),
            "repetition_penalty": float(repetition_penalty),
            "max_tokens": int(max_tokens),
            "seed": int(seed),
        }

        # Parse stop sequences
        stops: list[str] = []
        if stop_sequences and stop_sequences.strip():
            stops = [s.strip() for s in stop_sequences.split(",") if s.strip()]

        # Gemini-specific extra_body (safety + thinking)
        extra_body: dict | None = None
        if provider == "Gemini":
            extra_body = {}
            _apply_gemini_extras(extra_body, gemini_thinking_budget)

            # Caching: create or reuse a cached content resource for the stable prefix
            if enable_caching and resolved_key and sys_prompt and stable_user_text:
                cache_name = _gemini_create_cached_content(
                    api_key=resolved_key,
                    model=model_name,
                    system_text=sys_prompt,
                    stable_user_text=stable_user_text,
                )
                if cache_name:
                    extra_body.setdefault("google", {})["cached_content"] = cache_name
                    print(f"[LLM_Prompt_API] Using Gemini cached content: {cache_name}")

        # Send the request
        try:
            raw = _send_chat_completion(
                provider=provider,
                base_url=base_url,
                api_key=resolved_key,
                model=model_name,
                messages=messages,
                sampling=sampling,
                output_format=output_format,
                stop_sequences=stops,
                timeout=float(timeout_seconds),
                extra_body=extra_body,
            )
        finally:
            if unload_after_run and provider == "LM Studio (local)":
                ok, msg = _unload_lm_studio_model(base_url, model_name)
                if ok:
                    print(f"[LLM_Prompt_API] {msg}")
                else:
                    print(f"[LLM_Prompt_API] Unload-after-run failed: {msg}")

        if output_format == "text":
            cleaned = clean_model_output(raw, OutputCleanConfig(mode="prompt")) or raw
        else:
            cleaned = raw

        return (cleaned,)


# ---------------------------------------------------------------------------
# HTTP route for the LM Studio "Unload Now" JS button
# ---------------------------------------------------------------------------

def _register_routes():
    """Register a server-side route the JS button calls to trigger an unload.

    Doing this server-side (instead of having JS hit LM Studio directly) avoids
    CORS issues when ComfyUI runs on a different origin.
    """
    try:
        from server import PromptServer  # type: ignore
    except Exception:
        return

    try:
        from aiohttp import web  # type: ignore
    except Exception:
        return

    @PromptServer.instance.routes.post("/llm_prompt_api/lmstudio_unload")
    async def _route_unload(request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        base_url = (data.get("base_url") or "http://localhost:1234/v1").strip()
        model_name = (data.get("model_name") or "").strip()
        ok, msg = _unload_lm_studio_model(base_url, model_name)
        return web.json_response({"ok": bool(ok), "message": str(msg)})


_register_routes()


# ---------------------------------------------------------------------------
# ComfyUI registration
# ---------------------------------------------------------------------------

NODE_CLASS_MAPPINGS = {
    "LLMPromptAPI": LLMPromptAPINode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LLMPromptAPI": "LLM Prompt (API)",
}
