"""LLM Prompt (API) — ComfyUI node for prompt generation via remote/local OpenAI-compatible endpoints.

Supported providers:
  - Gemini (Google) — native API, default provider
  - Grok (xAI)
  - Custom (arbitrary OpenAI-compatible endpoint)

Design principles:
  - No GGUF loading, no chat template hell, no KV cache mgmt — the upstream
    server handles all of that.
  - Live-query /v1/models when possible (Gemini, Custom).
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
from pathlib import Path
from typing import Any

# Reuse helpers from the sister GGUF node
from .llm_prompt_node import (
    load_system_prompts,
    _build_canvas_profile,
    _tensor_to_base64_png,
    _sample_video_frames,
)
from .output_cleaner import OutputCleanConfig, clean_model_output, normalize_prompt_separator, split_positive_negative
from comfy_api.latest import io


# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------

PROVIDERS = {
    "Gemini": {
        # NATIVE Gemini REST API — NOT the OpenAI-compat layer. The compat layer
        # silently drops Gemini-specific params (thinking_budget, safety, top_k),
        # and rejects `seed` and `extra_body` at the HTTP level. Going native
        # gives us full feature support and matches Luna Core's approach.
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "env_var": ["GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_API_KEY"],
        "needs_auth": True,
        "live_models": True,
        "native_protocol": "gemini",  # routes to _send_gemini_native()
        # Last-resort fallback when /models fails (e.g. no API key set yet).
        # Current Gemini API models as of late 2025 / early 2026 — verified
        # against https://ai.google.dev/gemini-api/docs/models. Once an API key
        # is set, the live query returns the user's full account-accessible list.
        "fallback_models": [
            # Gemini 3 series (current)
            "gemini-3.1-pro-preview",
            "gemini-3.5-flash",
            "gemini-3-flash-preview",
            "gemini-3.1-flash-lite",
            "gemini-3.1-flash-lite-preview",
            # Gemini 2.5 (legacy, still supported)
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ],
    },
    "Grok (xAI)": {
        "base_url": "https://api.x.ai/v1",
        "env_var": ["XAI_API_KEY", "GROK_API_KEY"],
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


def _fetch_models_from_server(base_url: str, api_key: str | None, native: str = "") -> list[str]:
    """Query the models endpoint. Returns the list of model id strings.

    For most providers (OpenAI-compatible), hits `<base>/models` with optional
    Bearer auth and expects `{"data": [{"id": ...}]}`.

    For Gemini native, hits `<base>/models?key=<api_key>` and expects
    `{"models": [{"name": "models/gemini-..."}]}`.

    Silently returns [] on any failure.
    """
    if not base_url:
        return []
    try:
        if native == "gemini":
            # Native Gemini models endpoint
            if not api_key:
                return []
            url = f"{base_url.rstrip('/')}/models?key={api_key}"
            req = urllib.request.Request(url, method="GET")
        else:
            url = base_url.rstrip("/") + "/models"
            req = urllib.request.Request(url, method="GET")
            if api_key:
                req.add_header("Authorization", f"Bearer {api_key}")

        with urllib.request.urlopen(req, timeout=3.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError):
        return []
    except Exception:
        return []

    if not isinstance(payload, dict):
        return []

    names: list[str] = []
    if native == "gemini":
        # Gemini's native REST returns: {"models": [{"name": "models/gemini-2.5-flash", "supportedGenerationMethods": [...]}, ...]}
        for entry in payload.get("models") or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or ""
            if name.startswith("models/"):
                name = name[len("models/"):]
            if not name.startswith("gemini-"):
                continue
            # Skip non-chat methods (embeddings, tuning, etc.)
            methods = entry.get("supportedGenerationMethods") or []
            if methods and "generateContent" not in methods:
                continue
            # Skip image-gen / video-gen variants — they're not for chat
            if "-image" in name or name.startswith("imagen") or name.startswith("veo"):
                continue
            names.append(name)
    else:
        # OpenAI-compatible format
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return []
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
        raw_models = _fetch_models_from_server(base_url, api_key, cfg.get("native_protocol", ""))

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


def _comfyui_root() -> Path:
    """Walk up from this file to find ComfyUI root.

    Expected layout: <ComfyUI>/custom_nodes/LLM_Prompt/llm_prompt_api_node.py
    So __file__.parent (node) .parent (custom_nodes) .parent (ComfyUI) = root.
    """
    return Path(__file__).resolve().parent.parent.parent


def _load_env_file_keys() -> dict[str, str]:
    """Load API keys from .env files. Standard KEY=VALUE per line, # for comments.

    Search order (first match wins per key):
      1. <ComfyUI root>/.env          (PRIMARY — keys live with the install, not the git repo)
      2. <LLM_Prompt node folder>/.env (fallback — only use if you must)

    The node folder is INSIDE the git repo, so don't put your keys there unless
    you've gitignored it. The ComfyUI root is the right location — it's outside
    every custom node's repo, easy to back up, and standard practice.
    """
    keys: dict[str, str] = {}
    candidates = [
        _comfyui_root() / ".env",
        Path(__file__).resolve().parent / ".env",
    ]
    for path in candidates:
        try:
            if not path.is_file():
                continue
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and v and k not in keys:
                    keys[k] = v
        except Exception:
            continue
    return keys


def _resolve_api_key(provider: str, override_key: str = "") -> str | None:
    """Resolve the API key, in priority order:
       1. Process environment variable (os.environ) — checks all accepted names
       2. .env file in the ComfyUI root
       3. .env file in the LLM_Prompt node folder

    The `override_key` parameter exists for backward compatibility but is no
    longer wired to any node widget. Keys are NEVER stored in workflow JSON.
    """
    if override_key and override_key.strip():
        return override_key.strip()
    cfg = PROVIDERS.get(provider, {})
    env_var_spec = cfg.get("env_var")
    if not env_var_spec:
        return None
    # Normalize to list (each provider can accept multiple env var names)
    env_var_names = env_var_spec if isinstance(env_var_spec, list) else [env_var_spec]

    # 2. Process env — try every accepted name
    for name in env_var_names:
        v = os.environ.get(name)
        if v and v.strip():
            return v.strip()

    # 3 + 4. .env file fallback — try every accepted name
    file_keys = _load_env_file_keys()
    for name in env_var_names:
        if name in file_keys and file_keys[name].strip():
            return file_keys[name].strip()
    return None


def _diagnose_env_keys() -> None:
    """Print a one-time diagnostic showing which API key env vars ComfyUI sees.

    Helps users debug why their system env var isn't being picked up — most
    commonly because Easy-Install / embedded python doesn't inherit user-level
    env vars, or the variable was set after Comfy started.
    """
    print("[LLM_Prompt_API] API key environment check:")
    for prov_name, prov_cfg in PROVIDERS.items():
        spec = prov_cfg.get("env_var")
        if not spec:
            continue
        names = spec if isinstance(spec, list) else [spec]
        found = []
        for name in names:
            v = os.environ.get(name)
            if v:
                # Show only first 8 chars + length — don't leak the full key
                preview = f"{v[:8]}…({len(v)} chars)"
                found.append(f"{name}={preview}")
        # Check .env file too
        file_keys = _load_env_file_keys()
        for name in names:
            if name in file_keys and name not in [f.split("=")[0] for f in found]:
                preview = f"{file_keys[name][:8]}…(.env)"
                found.append(f"{name}={preview}")
        if found:
            print(f"  {prov_name}: {', '.join(found)}")
        else:
            print(f"  {prov_name}: NOT FOUND in env or .env (tried: {names})")


# Run the diagnostic once at module load
_diagnose_env_keys()


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
        # Grok requires seed to fit in a signed 32-bit int (max 2,147,483,647).
        # ComfyUI's randomize widget uses unsigned 32-bit range (up to 2^32-1),
        # so clamp via modulo to stay within Grok's bounds without losing entropy.
        if "seed" in p:
            seed_val = int(p["seed"])
            if seed_val > 2147483647:
                p["seed"] = seed_val % 2147483647

    elif provider == "Gemini":
        # Gemini is handled by the NATIVE API path (_send_gemini_native), not
        # this OpenAI-compat path. If we ever end up here for Gemini, strip
        # everything the compat endpoint rejects.
        p.pop("seed", None)
        p.pop("extra_body", None)
        p.pop("top_k", None)
        p.pop("min_p", None)
        p.pop("repetition_penalty", None)
        p.pop("repeat_penalty", None)

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


# _apply_gemini_extras() removed — was for the broken OpenAI-compat extra_body
# approach. Gemini now uses the native API path directly via _send_gemini_native.


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
# Native Gemini chat completion
# ---------------------------------------------------------------------------
# The OpenAI-compat layer rejects `seed`, `extra_body`, and silently drops
# Gemini-specific config. The native API supports everything we want.

def _convert_messages_to_gemini(messages: list[dict]) -> tuple[dict | None, list[dict]]:
    """Convert OpenAI-style messages to Gemini native format.

    Returns (system_instruction, contents) where:
      - system_instruction is {"parts": [{"text": "..."}]} or None
      - contents is list of {"role": "user"|"model", "parts": [...]}.

    Handles multimodal user content (text + image_url) by converting image_url
    data URIs to Gemini's inline_data blocks.
    """
    import base64

    system_text = None
    contents = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            if isinstance(content, str) and content:
                system_text = content
            continue
        # User / assistant messages
        gemini_role = "model" if role == "assistant" else "user"
        parts: list[dict] = []
        if isinstance(content, str):
            if content:
                parts.append({"text": content})
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                t = block.get("type")
                if t == "text":
                    text = block.get("text", "")
                    if text:
                        parts.append({"text": text})
                elif t == "image_url":
                    url = block.get("image_url", {}).get("url", "")
                    # Expect data URI: data:image/png;base64,XXXX
                    if url.startswith("data:"):
                        try:
                            header, b64 = url.split(",", 1)
                            mime = header.split(";")[0].replace("data:", "")
                            parts.append({
                                "inline_data": {
                                    "mime_type": mime or "image/png",
                                    "data": b64,
                                }
                            })
                        except Exception:
                            pass
        if parts:
            contents.append({"role": gemini_role, "parts": parts})

    system_instruction = {"parts": [{"text": system_text}]} if system_text else None
    return system_instruction, contents


def _send_gemini_native(
    base_url: str,  # unused — SDK handles endpoints
    api_key: str,
    model: str,
    messages: list[dict],
    sampling: dict,
    output_format: str,
    stop_sequences: list[str],
    thinking_budget: int,
    thinking_level: str,
    cached_content_name: str | None,
    timeout: float,  # unused — SDK manages timeouts
) -> str:
    """Send a chat request via google-genai SDK.

    Mirrors Luna Core / Comfy Pilot's gemini.py pattern. The SDK handles
    endpoints, message format conversion, retries, and protocol versioning.
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise RuntimeError(
            "google-genai SDK not installed. Run:\n"
            "    pip install google-genai\n"
            f"(import error: {e})"
        ) from None

    if not api_key:
        raise RuntimeError("Gemini requires an API key — set GEMINI_API_KEY in env or .env")
    if not model or model.startswith("<"):
        raise RuntimeError(f"Invalid model: {model!r}.")

    client = genai.Client(api_key=api_key)

    # Convert messages: extract system, convert user content (including images)
    system_text = None
    contents = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            if isinstance(content, str) and content:
                system_text = content
            continue
        gemini_role = "model" if role == "assistant" else "user"
        parts: list = []
        if isinstance(content, str):
            if content:
                parts.append(types.Part(text=content))
        elif isinstance(content, list):
            import base64
            for block in content:
                if not isinstance(block, dict):
                    continue
                t = block.get("type")
                if t == "text":
                    text = block.get("text", "")
                    if text:
                        parts.append(types.Part(text=text))
                elif t == "image_url":
                    url = block.get("image_url", {}).get("url", "")
                    if url.startswith("data:"):
                        try:
                            header, b64 = url.split(",", 1)
                            mime = header.split(";")[0].replace("data:", "") or "image/png"
                            parts.append(types.Part(inline_data=types.Blob(
                                mime_type=mime,
                                data=base64.b64decode(b64),
                            )))
                        except Exception:
                            pass
        if parts:
            contents.append(types.Content(role=gemini_role, parts=parts))

    # Build the SDK config — match Luna Core's simple pattern
    config_kwargs: dict[str, Any] = {}
    if system_text:
        config_kwargs["system_instruction"] = system_text
    if "temperature" in sampling:
        config_kwargs["temperature"] = float(sampling["temperature"])
    if "top_p" in sampling:
        config_kwargs["top_p"] = float(sampling["top_p"])
    if sampling.get("top_k", 0):
        config_kwargs["top_k"] = int(sampling["top_k"])
    if "max_tokens" in sampling:
        config_kwargs["max_output_tokens"] = int(sampling["max_tokens"])
    if stop_sequences:
        config_kwargs["stop_sequences"] = list(stop_sequences)
    if output_format == "json":
        config_kwargs["response_mime_type"] = "application/json"
    # Thinking control. On Gemini 3 Pro the API uses thinking_level (low/medium/
    # high) instead of a token budget; the two are mutually exclusive. When a
    # level is set AND the model is a Gen-3 Pro, send the level; otherwise fall
    # back to the token budget (0 = disabled, recommended for prompt gen).
    _is_gen3_pro = "gemini-3" in model.lower() and "pro" in model.lower()
    if thinking_level and thinking_level != "None" and _is_gen3_pro:
        try:
            config_kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_level=thinking_level,
            )
        except Exception:
            # SDK too old for thinking_level — fall back to the token budget.
            if thinking_budget >= 0:
                config_kwargs["thinking_config"] = types.ThinkingConfig(
                    thinking_budget=int(thinking_budget),
                )
    elif thinking_budget >= 0:
        config_kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_budget=int(thinking_budget),
        )
    # Hardcoded BLOCK_NONE for all four configurable safety categories
    config_kwargs["safety_settings"] = [
        types.SafetySetting(
            category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"
        ),
        types.SafetySetting(
            category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"
        ),
        types.SafetySetting(
            category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"
        ),
        types.SafetySetting(
            category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"
        ),
    ]
    if cached_content_name:
        config_kwargs["cached_content"] = cached_content_name

    gen_config = types.GenerateContentConfig(**config_kwargs)

    start = time.perf_counter()
    try:
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=gen_config,
        )
    except Exception as e:
        # SDK exception messages are usually descriptive enough
        raise RuntimeError(f"Gemini SDK error: {e}") from None
    elapsed = max(time.perf_counter() - start, 1e-6)

    # Extract text from response
    result_text = ""
    if response.text:
        result_text = response.text
    elif response.candidates:
        for cand in response.candidates:
            if cand.content and cand.content.parts:
                for p in cand.content.parts:
                    if hasattr(p, "text") and p.text:
                        result_text += p.text

    # Log usage like Luna Core does
    try:
        usage = response.usage_metadata
        pt = getattr(usage, "prompt_token_count", None)
        ct = getattr(usage, "candidates_token_count", None)
        cached = getattr(usage, "cached_content_token_count", None) or 0
        tt = getattr(usage, "thoughts_token_count", None) or 0
        cache_note = f", cached={cached}" if cached else ""
        think_note = f", thoughts={tt}" if tt else ""
        print(
            f"[LLM_Prompt_API] Gemini | {model} | "
            f"prompt={pt}, completion={ct}{cache_note}{think_note}, "
            f"time={elapsed:.2f}s"
        )
    except Exception:
        print(f"[LLM_Prompt_API] Gemini | {model} | time={elapsed:.2f}s")

    return result_text.strip()


# ---------------------------------------------------------------------------
# HTTP chat completion (OpenAI-compatible — LM Studio, Grok, Custom)
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
    response_format_override: dict | None = None,
) -> str:
    """POST to /v1/chat/completions with per-provider sanitization + retry.

    sampling dict can contain: temperature, top_p, top_k, min_p,
    repetition_penalty, presence_penalty, frequency_penalty, max_tokens, seed.

    response_format_override: if set, used as-is for response_format (e.g. a
    json_schema dict). Takes priority over the output_format-derived default.
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

    if response_format_override:
        payload["response_format"] = response_format_override
    elif output_format == "json":
        # Forced JSON at the API level — more reliable than instructions
        payload["response_format"] = {"type": "json_object"}

    if extra_body:
        # The OpenAI SDK flattens extra_body into the top-level request body; it
        # does NOT send a literal "extra_body" field. Since this node POSTs raw
        # JSON (not via the SDK), we replicate that flattening here so params like
        # chat_template_kwargs land at the top level where LM Studio reads them.
        for k, v in extra_body.items():
            payload[k] = v

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

class LLMPromptAPINode(io.ComfyNode):
    """Prompt generation via OpenAI-compatible API (Gemini, Grok, Custom).

    Zero llama-cpp-python dependency. Just HTTP.
    """

    @classmethod
    def define_schema(cls) -> io.Schema:
        prompts = load_system_prompts()
        prompt_names = ["None"] + sorted(prompts.keys())
        providers = list(PROVIDERS.keys())
        default_provider = providers[0]

        # CRITICAL: ComfyUI validates the model_name value against this list
        # server-side. The JS extension changes the *visible* options when the
        # user picks a provider, but the validator uses what's returned here.
        # So we must return a SUPERSET of all possible model names across all
        # providers — otherwise picking e.g. a Grok model fails validation
        # because it isn't in Gemini's list.
        all_models: list[str] = []
        for prov_name, prov_cfg in PROVIDERS.items():
            all_models.extend(prov_cfg.get("fallback_models") or [])
        # Live-query each provider that supports it AND has credentials
        # available at INPUT_TYPES time (env var or no auth needed). This is
        # critical: the JS extension's browser-side live query may show models
        # to the user that aren't in our hardcoded fallback, and those would
        # fail server-side validation if we don't include them here too.
        for prov_name, prov_cfg in PROVIDERS.items():
            if not prov_cfg.get("live_models"):
                continue
            base = prov_cfg.get("base_url") or ""
            if not base:
                continue  # Custom — no URL until user fills in widget
            # Use the full key resolver so .env files are honored at scan time too
            key = _resolve_api_key(prov_name, "")
            if prov_cfg.get("needs_auth") and not key:
                continue  # Skip auth-required providers without a key
            try:
                live = _fetch_models_from_server(base, key, prov_cfg.get("native_protocol", ""))
                all_models.extend(live)
            except Exception:
                pass
        # Dedupe while preserving order
        seen: set[str] = set()
        model_options: list[str] = []
        for m in all_models:
            if m and m not in seen:
                seen.add(m)
                model_options.append(m)
        if not model_options:
            model_options = ["<no models available>"]

        return io.Schema(
            node_id="LLMPromptAPI",
            display_name="LLM Prompt (API)",
            category="Luna/LLM",
            description=(
                "Cloud prompt generation via OpenAI-compatible / native APIs. "
                "Gemini (default, via google-genai SDK), Grok, or any Custom endpoint. "
                "Outputs positive / negative for image-gen workflows."
            ),
            inputs=[
                # ===== BASIC =====
                io.Combo.Input("provider", options=providers, default=default_provider,
                               tooltip="Which backend to call. Gemini / Grok = cloud APIs (need an API key in an env var or a .env at the ComfyUI root). Custom = any OpenAI-compatible endpoint (set server_url)."),
                io.Combo.Input("model_name", options=model_options,
                               tooltip="Available models. Gemini fetches your account's accessible models. Grok shows the curated list (no /models endpoint). Refreshes when the provider changes."),
                io.String.Input("server_url", default="",
                                tooltip="Override the provider's default URL. Required for the Custom provider. Leave empty to use the provider default."),
                io.Combo.Input("model_filter", options=["all", "text only", "vision", "multimodal"], default="all",
                               tooltip="Filter the model dropdown by capability. 'vision' shows models that accept images. 'multimodal' shows models accepting multiple input types."),
                io.Combo.Input("system_prompt", options=prompt_names, default=prompt_names[0],
                               tooltip="System prompt preset loaded from prompts/*.md. Choose None to use only custom_system_prompt."),
                io.String.Input("custom_system_prompt", multiline=True, default="",
                                tooltip="Override the preset. If non-empty, replaces whatever the preset would have been."),
                io.String.Input("user_prompt", multiline=True, default="",
                                tooltip="Your scene concept, subject, idea."),
                io.Combo.Input("output_format", options=["text", "json", "list"], default="text",
                               tooltip="text = plain prompt paragraph. json = forced JSON output. list = numbered multi-scene list (for LTX video tracks)."),
                io.Boolean.Input("split_output", default=True,
                                 tooltip="ON splits a 'positive | negative' result into the two outputs. OFF = full text on positive, negative empty. Uses the same bulletproof marker contract as the GGUF node."),
                io.Int.Input("max_tokens", default=4096, min=64, max=32000,
                             tooltip="Maximum tokens to generate. 4096 handles single prompts and short lists. Raise for long LTX video tracks."),
                io.Int.Input("seed", default=0, min=0, max=2**32 - 1,
                             tooltip="Random seed. 0 = non-deterministic (the provider picks). Cloud APIs treat this as best-effort."),

                # ===== ADVANCED (collapsed by default) =====
                io.Float.Input("temperature", default=0.7, min=0.0, max=2.0, step=0.05, advanced=True,
                               tooltip="Sampling randomness. Lower = focused, higher = creative. Auto-fills when you pick a model from a known family."),
                io.Float.Input("top_p", default=0.9, min=0.0, max=1.0, step=0.05, advanced=True,
                               tooltip="Nucleus sampling. Ref: Qwen ~0.8, Gemma ~0.95. Auto-fills with the model preset."),
                io.Int.Input("top_k", default=0, min=0, max=200, advanced=True,
                             tooltip="Top-K. 0 = disabled. Qwen 20, Gemma 64. Grok / OpenAI reject it — stripped automatically."),
                io.Float.Input("min_p", default=0.0, min=0.0, max=1.0, step=0.01, advanced=True,
                               tooltip="Min-P. 0.0 = disabled (Unsloth default). Some cloud APIs reject it — stripped automatically."),
                io.Float.Input("presence_penalty", default=0.0, min=-2.0, max=2.0, step=0.1, advanced=True,
                               tooltip="Discourages reusing tokens. Unsloth recommends 1.5 for Qwen instruct, 0.0 for Gemma 4."),
                io.Float.Input("frequency_penalty", default=0.0, min=-2.0, max=2.0, step=0.1, advanced=True,
                               tooltip="Discourages repeating frequent tokens. 0.0 = disabled."),
                io.Float.Input("repetition_penalty", default=1.0, min=0.5, max=2.0, step=0.05, advanced=True,
                               tooltip="Repetition penalty. 1.0 = disabled. Many cloud APIs ignore or reject it."),
                io.String.Input("stop_sequences", default="", advanced=True,
                                tooltip="Comma-separated strings that stop generation. Example: 'END,---,EOF'. Empty = default."),
                io.Int.Input("gemini_thinking_budget", default=0, min=0, max=24576, advanced=True,
                             tooltip="Gemini flash/2.5 only. Reasoning tokens before answering. 0 = no thinking (recommended for prompts). Ignored on Gemini 3 Pro (use gemini_thinking_level)."),
                io.Combo.Input("gemini_thinking_level", options=["None", "low", "medium", "high"], default="None", advanced=True,
                               tooltip="Gemini 3 Pro only. Reasoning depth — the Gen-3 Pro replacement for thinking_budget. When set (not None) on a Gemini 3 Pro model it overrides the budget. Leave None for prompts."),
                io.Boolean.Input("enable_caching", default=False, advanced=True,
                                 tooltip="Gemini only. Caches the stable prefix (system + style + canvas) to cut cost on repeated variations. Needs a ~1024+ token prefix."),
                io.Boolean.Input("disable_thinking", default=True, advanced=True,
                                 tooltip="Strips any <think> reasoning from the OUTPUT as a safety net. For Gemini, set the thinking budget/level to 0/None to disable at the source."),
                io.Int.Input("timeout_seconds", default=120, min=5, max=600, advanced=True,
                             tooltip="HTTP timeout for the chat call. Raise to 180+ for big reasoning models."),
                io.Float.Input("bbox_min_score", default=0.0, min=0.0, max=1.0, step=0.05, advanced=True,
                               tooltip="Filter pre-detected bboxes by detection score before injecting. 0 = keep all."),

                # ===== Optional connections =====
                io.String.Input("style", optional=True, force_input=True,
                                tooltip="Style description from an external node."),
                io.Int.Input("width", optional=True, force_input=True,
                             tooltip="Image width - drives an aspect-ratio composition profile."),
                io.Int.Input("height", optional=True, force_input=True,
                             tooltip="Image height - drives an aspect-ratio composition profile."),
                io.Image.Input("image", optional=True, tooltip="Input image for vision-capable models."),
                io.Video.Input("video", optional=True, tooltip="Input video for vision-capable models."),
                io.BoundingBox.Input("bboxes", optional=True, force_input=True,
                                     tooltip="Optional pre-detected regions (e.g. SAM 3.1), pixel-space "
                                             "[[{x,y,width,height,score}]]. When present, they're injected into the "
                                             "prompt as FIXED regions the model must describe in context and reuse "
                                             "verbatim (image-to-image layout). Needs the image dims — wire width/height "
                                             "or connect the reference image."),
            ],
            outputs=[
                io.String.Output("positive", display_name="positive"),
                io.String.Output("negative", display_name="negative"),
            ],
        )

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return float("nan")

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    @classmethod
    def execute(
        cls,
        provider: str,
        model_name: str,
        server_url: str,
        model_filter: str,
        system_prompt: str,
        custom_system_prompt: str,
        user_prompt: str,
        output_format: str,
        split_output: bool,
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
        gemini_thinking_level: str,
        enable_caching: bool,
        disable_thinking: bool,
        timeout_seconds: int,
        style: str = "",
        width: int = 0,
        height: int = 0,
        image=None,
        video=None,
        bboxes=None,
        bbox_min_score: float = 0.0,
    ):
        cfg = PROVIDERS.get(provider)
        if not cfg:
            raise RuntimeError(f"Unknown provider: {provider!r}")

        base_url = _resolve_base_url(provider, server_url)
        # Keys are NEVER passed via node widgets — read only from env / .env
        resolved_key = _resolve_api_key(provider)
        if cfg.get("needs_auth") and not resolved_key:
            spec = cfg.get("env_var")
            names = spec if isinstance(spec, list) else [spec or "<none>"]
            primary = names[0]
            alt_text = f" (or any of: {', '.join(names[1:])})" if len(names) > 1 else ""
            env_path = _comfyui_root() / ".env"
            raise RuntimeError(
                f"{provider} requires an API key. Two ways to set it:\n"
                f"  1. Set the {primary} environment variable{alt_text} BEFORE launching ComfyUI "
                f"(env vars set after ComfyUI starts are NOT picked up by Easy-Install builds), OR\n"
                f"  2. Create a .env file at {env_path} with the line:\n"
                f"     {primary}=your_key_here\n\n"
                f"The node intentionally has NO api_key widget — widget values are saved to "
                f"workflow JSON and would leak the key if you shared the workflow.\n\n"
                f"Check the ComfyUI startup log for '[LLM_Prompt_API] API key environment check:' "
                f"to see exactly which keys this Python process sees."
            )

        # Resolve system prompt
        if custom_system_prompt and custom_system_prompt.strip():
            sys_prompt = custom_system_prompt.strip()
        elif system_prompt and system_prompt != "None":
            prompts = load_system_prompts()
            sys_prompt = prompts.get(system_prompt, "")
        else:
            sys_prompt = ""

        # Grok: use json_schema with strict=true to guarantee positive/negative
        # split — text format instructions are unreliable with Grok.
        # Detected here (before tails are appended) so we can choose the right tail.
        grok_pair_schema: dict | None = None
        if (
            provider == "Grok (xAI)"
            and output_format == "text"
            and sys_prompt
            and "|" in sys_prompt
            and "negative" in sys_prompt.lower()
        ):
            grok_pair_schema = {
                "type": "json_schema",
                "json_schema": {
                    "name": "prompt_pair",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "positive": {
                                "type": "string",
                                "description": "The positive image generation prompt",
                            },
                            "negative": {
                                "type": "string",
                                "description": "The negative image generation prompt",
                            },
                        },
                        "required": ["positive", "negative"],
                        "additionalProperties": False,
                    },
                },
            }

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
            if grok_pair_schema:
                # Tell Grok to fill JSON fields — "no JSON" would contradict the schema
                sys_prompt += (
                    "\n\nOutput the positive prompt in the \"positive\" field "
                    "and the negative prompt in the \"negative\" field. "
                    "No extra text outside the JSON."
                )
            else:
                sys_prompt += (
                    "\n\nReturn only the final prompt text. "
                    "No preface, no explanations, no JSON, no markdown fences."
                )
                # Authoritative positive/negative delimiter contract (identical to
                # the GGUF node). Labelled markers survive where a bare '|' gets
                # dropped; the shared parser strips them so outputs stay clean.
                if split_output:
                    sys_prompt += (
                        "\n\nIf your instructions produce a NEGATIVE prompt, format your entire "
                        "answer EXACTLY like this, each marker on its own line:\n"
                        "[POSITIVE]\n<the full positive prompt>\n[NEGATIVE]\n<the full negative prompt>\n"
                        "Write nothing before [POSITIVE] and nothing after the negative prompt. "
                        "If there is no negative prompt, write [POSITIVE] then your prompt and stop."
                    )

        # Pre-detected regions (e.g. SAM 3.1): pixel [[{x,y,width,height,score}]] ->
        # a fixed [ymin,xmin,ymax,xmax] 0-1000 block the model must reuse verbatim.
        def _sam_region_block():
            if not bboxes:
                return ""
            nw, nh = width, height
            if (nw <= 0 or nh <= 0) and image is not None:
                try:
                    nh, nw = int(image.shape[1]), int(image.shape[2])
                except Exception:
                    nw = nh = 0
            if nw <= 0 or nh <= 0:
                return ""
            if isinstance(bboxes, dict):
                frame = [bboxes]
            elif isinstance(bboxes, (list, tuple)) and bboxes and isinstance(bboxes[0], (list, tuple)):
                frame = bboxes[0]
            else:
                frame = bboxes
            def c(v, m):
                return max(0, min(1000, round(v / m * 1000)))
            rows = []
            for bb in frame or []:
                if not isinstance(bb, dict):
                    continue
                if bbox_min_score and float(bb.get("score", 1.0)) < bbox_min_score:
                    continue
                x, y = bb.get("x", 0), bb.get("y", 0)
                w, h = bb.get("width", 0), bb.get("height", 0)
                rows.append([c(y, nh), c(x, nw), c(y + h, nh), c(x + w, nw)])
            if not rows:
                return ""
            lines = "\n".join(f"  - Region {i + 1}: bbox {b}" for i, b in enumerate(rows))
            return (
                "PRE-DETECTED REGIONS (from image segmentation). Use these EXACT bboxes "
                "verbatim — do NOT move, resize, add, or drop any. For each region, describe "
                "what occupies it in the context of the image, and assemble the output using "
                "exactly these bboxes (format [ymin,xmin,ymax,xmax] on a 0-1000 grid):\n" + lines
            )

        # Effective canvas dims: wired width/height, else the input image's dims,
        # so composition is ALWAYS aspect-ratio-driven.
        eff_w, eff_h = width, height
        if (eff_w <= 0 or eff_h <= 0) and image is not None:
            try:
                eff_h, eff_w = int(image.shape[1]), int(image.shape[2])
            except Exception:
                pass

        # Build user prompt — STABLE FIRST, VARIABLE LAST for cache friendliness.
        # LEAD with the aspect-ratio canvas profile, then STYLE -> REGIONS / USER REQUEST.
        stable_parts: list[str] = []
        if eff_w > 0 and eff_h > 0:
            canvas_block = _build_canvas_profile(eff_w, eff_h)
            if canvas_block:
                stable_parts.append(canvas_block)
        if style and style.strip():
            stable_parts.append(f"STYLE:\n{style.strip()}")

        region_block = _sam_region_block()

        variable_parts: list[str] = []
        if region_block:
            variable_parts.append(region_block)
        if user_prompt and user_prompt.strip():
            variable_parts.append(f"USER REQUEST:\n{user_prompt.strip()}")

        all_parts = stable_parts + variable_parts
        final_user_prompt = "\n\n".join(all_parts) if all_parts else "Describe a scene vividly."

        # Stable text for Gemini caching purposes (system + stable user portion)
        stable_user_text = "\n\n".join(stable_parts)

        # Vision content. Guard against a disabled/empty upstream (e.g. a bypassed
        # image loader): treat a missing or zero-length tensor as "no image" -> t2i.
        images_b64: list[str] = []
        if image is not None and getattr(image, "shape", (0,))[0] > 0:
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

        # Send the request — route Gemini through its native API for full
        # feature support (thinking_budget, safety, top_k, caching). Other
        # providers go through the OpenAI-compatible /v1/chat/completions path.
        if cfg.get("native_protocol") == "gemini":
            # Caching: create or reuse a cached content resource for the stable prefix
            cache_name: str | None = None
            if enable_caching and resolved_key and sys_prompt and stable_user_text:
                cache_name = _gemini_create_cached_content(
                    api_key=resolved_key,
                    model=model_name,
                    system_text=sys_prompt,
                    stable_user_text=stable_user_text,
                )
                if cache_name:
                    print(f"[LLM_Prompt_API] Using Gemini cached content: {cache_name}")

            raw = _send_gemini_native(
                base_url=base_url,
                api_key=resolved_key,
                model=model_name,
                messages=messages,
                sampling=sampling,
                output_format=output_format,
                stop_sequences=stops,
                thinking_budget=gemini_thinking_budget,
                thinking_level=gemini_thinking_level,
                cached_content_name=cache_name,
                timeout=float(timeout_seconds),
            )
        else:
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
                extra_body=None,
                response_format_override=grok_pair_schema,
            )

        if grok_pair_schema:
            # Grok returned a guaranteed JSON object — assemble positive|negative directly.
            # No text cleaning needed; the schema enforces clean string fields.
            try:
                pair = json.loads(raw)
                positive = str(pair.get("positive") or "").strip()
                negative = str(pair.get("negative") or "").strip()
                cleaned = f"{positive}|{negative}" if negative else positive
            except Exception as e:
                print(f"[LLM_Prompt_API] Grok JSON parse failed ({e}), falling back to text cleaning")
                cleaned = clean_model_output(raw, OutputCleanConfig(mode="prompt")) or raw
                cleaned = normalize_prompt_separator(cleaned)
        elif output_format == "text":
            # The shared hardened split_positive_negative() below handles markers,
            # labels, pipe and JSON, so the old normalize-to-pipe pre-pass is gone.
            cleaned = clean_model_output(raw, OutputCleanConfig(mode="prompt")) or raw
        else:
            cleaned = raw

        # Split 'positive|negative' into the two outputs (or full text on
        # positive + empty negative when split_output is off / no pipe).
        positive, negative = split_positive_negative(cleaned, split_output)
        return io.NodeOutput(positive, negative)


# ---------------------------------------------------------------------------
# ComfyUI registration
# ---------------------------------------------------------------------------

NODE_CLASS_MAPPINGS = {
    "LLMPromptAPI": LLMPromptAPINode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LLMPromptAPI": "LLM Prompt (API)",
}
