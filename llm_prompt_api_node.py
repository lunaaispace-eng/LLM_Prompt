"""LLM Prompt (API) — ComfyUI node for prompt generation via remote/local OpenAI-compatible endpoints.

Supports LM Studio (local server), Gemini, Grok (xAI), OpenAI, OpenRouter,
and arbitrary OpenAI-compatible servers via a single HTTP code path.

Why this exists: the embedded llama-cpp-python approach (used by the sister
node LLMPromptNode) is brittle across chat templates, EOS handling, thinking
modes, and KV cache. Outsourcing inference to LM Studio or a cloud provider
eliminates every one of those failure modes — we just send messages and read
text back.

Settings exposed: provider, server URL, model, API key, sampling params
(temperature, top_p, top_k, min_p, repetition_penalty, max_tokens, seed),
system prompt presets (.md files), custom system prompt, user prompt,
output format (text/json/list), style, width, height, image, video.

Models for LM Studio: live-fetched from /v1/models on each scan.
Models for cloud providers: static curated list per provider (so the node
shows up even when offline / no API key configured).
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from typing import Any

# Reuse helpers from the sister node — system prompt loading, canvas profile,
# image/video conversion. Keeps both nodes in sync if those evolve.
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

# Each provider entry:
#   base_url        : root URL up to /v1 (we append /chat/completions etc.)
#   env_var         : environment variable name for API key
#   needs_auth      : whether Authorization: Bearer header is required
#   live_models     : whether to query /v1/models for the dropdown
#   fallback_models : static list used when live query fails / not applicable
#   supports_vision : whether the provider accepts image_url messages

PROVIDERS = {
    "LM Studio (local)": {
        "base_url": "http://localhost:1234/v1",
        "env_var": None,
        "needs_auth": False,
        "live_models": True,
        "fallback_models": ["<no model loaded — load one in LM Studio>"],
        "supports_vision": True,  # depends on the loaded model
    },
    "Gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "env_var": "GEMINI_API_KEY",
        "needs_auth": True,
        "live_models": False,
        "fallback_models": [
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
        ],
        "supports_vision": True,
    },
    "Grok (xAI)": {
        "base_url": "https://api.x.ai/v1",
        "env_var": "XAI_API_KEY",
        "needs_auth": True,
        "live_models": False,
        "fallback_models": [
            "grok-4",
            "grok-4-fast",
            "grok-3",
            "grok-3-mini",
            "grok-2-vision",
            "grok-2",
        ],
        "supports_vision": True,  # grok-4, grok-2-vision support it
    },
    "OpenAI": {
        "base_url": "https://api.openai.com/v1",
        "env_var": "OPENAI_API_KEY",
        "needs_auth": True,
        "live_models": False,
        "fallback_models": [
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4-turbo",
            "o1",
            "o1-mini",
            "o3-mini",
        ],
        "supports_vision": True,
    },
    "OpenRouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "env_var": "OPENROUTER_API_KEY",
        "needs_auth": True,
        "live_models": False,
        "fallback_models": [
            "anthropic/claude-3.5-sonnet",
            "openai/gpt-4o",
            "google/gemini-2.5-pro",
            "meta-llama/llama-3.3-70b-instruct",
            "qwen/qwen-2.5-72b-instruct",
        ],
        "supports_vision": True,  # depends on chosen model
    },
    "Custom": {
        "base_url": "",  # user supplies via the server_url widget
        "env_var": None,
        "needs_auth": False,
        "live_models": True,  # attempt /v1/models, fall back gracefully
        "fallback_models": ["<set server_url and refresh>"],
        "supports_vision": True,
    },
}


# Cache (provider, base_url) -> list of model names. ComfyUI calls INPUT_TYPES
# multiple times per workflow validation; this prevents re-hammering the LM
# Studio /v1/models endpoint every call.
_MODEL_LIST_CACHE: dict[tuple[str, str], list[str]] = {}
_MODEL_LIST_CACHE_TS: dict[tuple[str, str], float] = {}
_MODEL_CACHE_TTL_SECONDS = 30


def _fetch_models_from_server(base_url: str, api_key: str | None = None) -> list[str]:
    """Query the server's /v1/models endpoint. Returns [] on any failure."""
    if not base_url:
        return []
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, method="GET")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError):
        return []
    except Exception:
        return []

    # Expected shape: {"data": [{"id": "model-name"}, ...]}
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    names = []
    for entry in data:
        if isinstance(entry, dict):
            name = entry.get("id") or entry.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return names


def _get_models_for_provider(provider: str, custom_url: str = "") -> list[str]:
    """Return the model dropdown options for a provider.

    For providers with live_models=True, attempts a /v1/models query first
    and falls back to the static list if that fails. Caches results for
    _MODEL_CACHE_TTL_SECONDS so ComfyUI's repeated INPUT_TYPES calls don't
    hammer the endpoint.
    """
    cfg = PROVIDERS.get(provider)
    if not cfg:
        return ["<unknown provider>"]

    base_url = custom_url if provider == "Custom" else cfg["base_url"]
    cache_key = (provider, base_url)
    now = time.time()
    cached_ts = _MODEL_LIST_CACHE_TS.get(cache_key, 0)
    if (now - cached_ts) < _MODEL_CACHE_TTL_SECONDS:
        cached = _MODEL_LIST_CACHE.get(cache_key)
        if cached:
            return cached

    api_key = None
    if cfg.get("env_var"):
        api_key = os.environ.get(cfg["env_var"]) or None

    if cfg.get("live_models") and base_url:
        live = _fetch_models_from_server(base_url, api_key)
        if live:
            _MODEL_LIST_CACHE[cache_key] = live
            _MODEL_LIST_CACHE_TS[cache_key] = now
            return live

    # Fallback path: static list
    fallback = list(cfg.get("fallback_models") or [])
    _MODEL_LIST_CACHE[cache_key] = fallback
    _MODEL_LIST_CACHE_TS[cache_key] = now
    return fallback


def _resolve_api_key(provider: str, override_key: str = "") -> str | None:
    """Pick the API key from override -> env var -> None."""
    cfg = PROVIDERS.get(provider, {})
    if override_key and override_key.strip():
        return override_key.strip()
    env_var = cfg.get("env_var")
    if env_var:
        v = os.environ.get(env_var)
        if v:
            return v.strip()
    return None


def _resolve_base_url(provider: str, custom_url: str = "") -> str:
    """Pick the base URL — custom override beats default."""
    cfg = PROVIDERS.get(provider, {})
    if custom_url and custom_url.strip():
        return custom_url.strip().rstrip("/")
    return str(cfg.get("base_url") or "").rstrip("/")


def _unload_model_lm_studio(base_url: str, model_name: str) -> bool:
    """Unload a model from LM Studio. Best-effort. Returns True on success."""
    # LM Studio's native unload endpoint is at /api/v0/models/unload
    # (not the OpenAI-compat /v1/ path)
    if "localhost" not in base_url and "127.0.0.1" not in base_url:
        # Don't issue unload to non-local endpoints
        return False
    # Strip /v1 if present, append the LM Studio native path
    root = base_url.replace("/v1", "")
    url = root.rstrip("/") + "/api/v0/models/unload"
    body = json.dumps({"identifier": model_name}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


# ---------------------------------------------------------------------------
# HTTP chat completion
# ---------------------------------------------------------------------------

def _send_chat_completion(
    base_url: str,
    api_key: str | None,
    model: str,
    messages: list[dict],
    temperature: float,
    top_p: float,
    top_k: int,
    min_p: float,
    repetition_penalty: float,
    max_tokens: int,
    seed: int,
    timeout: float = 120.0,
) -> str:
    """POST to /v1/chat/completions and return the message content.

    Raises RuntimeError with a readable message on any failure.
    """
    if not base_url:
        raise RuntimeError("Empty server URL. Set a provider or fill in server_url.")
    if not model or model.startswith("<"):
        raise RuntimeError(f"Invalid model: {model!r}. Pick a real model from the dropdown.")

    url = base_url.rstrip("/") + "/chat/completions"

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": float(temperature),
        "top_p": float(top_p),
        "max_tokens": int(max_tokens),
    }
    # Some providers reject unknown params; only include the optional ones
    # when they have non-default values.
    if int(top_k) > 0:
        payload["top_k"] = int(top_k)
    if float(min_p) > 0:
        payload["min_p"] = float(min_p)
    if abs(float(repetition_penalty) - 1.0) > 1e-6:
        payload["repetition_penalty"] = float(repetition_penalty)
    if int(seed) > 0:
        payload["seed"] = int(seed)

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")

    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        # Try to read the response body for a useful error
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
        raise RuntimeError(f"Timed out after {timeout}s waiting for {url}") from None
    elapsed = max(time.perf_counter() - start, 1e-6)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Non-JSON response from {url}: {raw[:500]}") from None

    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected response shape from {url}: {raw[:500]}")

    # OpenAI-compatible shape: {"choices": [{"message": {"content": "..."}}]}
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        # Some providers return errors as {"error": {"message": "..."}}
        err = data.get("error")
        if isinstance(err, dict):
            raise RuntimeError(f"API error: {err.get('message', err)}")
        raise RuntimeError(f"No choices in response: {raw[:500]}")

    first = choices[0] or {}
    msg = first.get("message") or {}
    content = msg.get("content", "")

    # Some providers (Anthropic via OpenRouter, etc.) return content as a list
    # of typed blocks instead of a plain string. Flatten.
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
    print(
        f"[LLM_Prompt_API] {model} | prompt={pt}, completion={ct}, "
        f"time={elapsed:.2f}s"
    )

    return str(content or "").strip()


# ---------------------------------------------------------------------------
# Image / video helpers
# ---------------------------------------------------------------------------

def _build_image_message_content(user_text: str, images_b64: list[str]) -> list[dict]:
    """Build an OpenAI-style multimodal message content array."""
    parts: list[dict] = [{"type": "text", "text": user_text}]
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
    """Prompt generation via OpenAI-compatible API (LM Studio, Gemini, Grok, OpenAI, OpenRouter, Custom).

    No GGUF loading. No chat templates. No KV cache management. Just HTTP.
    """

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("PROMPT",)
    FUNCTION = "generate"
    CATEGORY = "Luna/LLM"

    def __init__(self):
        self.last_loaded_model = None
        self.last_base_url = None

    @classmethod
    def INPUT_TYPES(cls):
        prompts = load_system_prompts()
        prompt_names = ["None"] + sorted(prompts.keys())
        providers = list(PROVIDERS.keys())
        # Default model list reflects the first provider's options. Switching
        # provider in the UI will not refresh this list in-place — ComfyUI
        # rebuilds INPUT_TYPES when the workflow reloads, so users may need
        # to reload the workflow once after changing provider. The frontend
        # extension (llm_prompt_api_presets.js) handles provider->URL sync
        # and validation.
        default_provider = providers[0] if providers else "LM Studio (local)"
        model_options = _get_models_for_provider(default_provider, "")
        if not model_options:
            model_options = ["<unavailable — check server / API key>"]

        return {
            "required": {
                "provider": (providers, {
                    "default": default_provider,
                    "tooltip": "Backend to call. LM Studio = local server. Gemini/Grok/OpenAI/OpenRouter = cloud APIs (need API key in env var or override field). Custom = arbitrary OpenAI-compatible endpoint.",
                }),
                "model_name": (model_options, {
                    "tooltip": "Live-fetched from LM Studio. Static curated list for cloud providers. Reload the workflow to refresh.",
                }),
                "server_url": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "Override the provider's default URL. Required for 'Custom' provider. Leave empty to use the provider default.",
                }),
                "api_key": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "API key override. Leave empty to use the provider's environment variable (GEMINI_API_KEY, XAI_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY).",
                }),
                "system_prompt": (prompt_names, {
                    "default": prompt_names[0],
                    "tooltip": "System prompt preset from prompts/*.md. Choose None to use only custom_system_prompt.",
                }),
                "custom_system_prompt": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "tooltip": "Override or extend the system prompt. If non-empty, replaces the preset.",
                }),
                "user_prompt": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "tooltip": "Your scene concept, subject, idea.",
                    "forceInput": False,
                }),
                "output_format": (["text", "json", "list"], {
                    "default": "text",
                    "tooltip": "text = paragraph. json = structured JSON. list = numbered multi-scene list (for LTX video tracks).",
                }),
                "max_tokens": ("INT", {
                    "default": 4096,
                    "min": 64,
                    "max": 32000,
                    "tooltip": "Max tokens to generate.",
                }),
                "temperature": ("FLOAT", {
                    "default": 0.7,
                    "min": 0.0,
                    "max": 2.0,
                    "step": 0.05,
                }),
                "top_p": ("FLOAT", {
                    "default": 0.9,
                    "min": 0.0,
                    "max": 1.0,
                    "step": 0.05,
                }),
                "top_k": ("INT", {
                    "default": 0,
                    "min": 0,
                    "max": 200,
                    "tooltip": "Top-K sampling. 0 = provider default (some cloud APIs don't accept top_k).",
                }),
                "min_p": ("FLOAT", {
                    "default": 0.0,
                    "min": 0.0,
                    "max": 1.0,
                    "step": 0.01,
                    "tooltip": "Min-P sampling. 0 = disabled (cloud APIs may not accept it).",
                }),
                "repetition_penalty": ("FLOAT", {
                    "default": 1.0,
                    "min": 0.5,
                    "max": 2.0,
                    "step": 0.05,
                    "tooltip": "1.0 = disabled. Most cloud APIs do not honor this.",
                }),
                "seed": ("INT", {
                    "default": 0,
                    "min": 0,
                    "max": 2**32 - 1,
                    "tooltip": "0 = provider default (non-deterministic).",
                }),
                "unload_after_run": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "LM Studio only: send an unload request after inference. Frees VRAM on the LM Studio side.",
                }),
                "timeout_seconds": ("INT", {
                    "default": 120,
                    "min": 5,
                    "max": 600,
                    "tooltip": "HTTP timeout for the chat-completion call.",
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
        # Always re-run on every prompt — model list and server state are external.
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
        system_prompt: str,
        custom_system_prompt: str,
        user_prompt: str,
        output_format: str,
        max_tokens: int,
        temperature: float,
        top_p: float,
        top_k: int,
        min_p: float,
        repetition_penalty: float,
        seed: int,
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

        # Resolve URL & key
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

        # Append the output-format directive to the system prompt
        if output_format == "json":
            sys_prompt += (
                "\n\nReturn only the final prompt as a JSON object. "
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

        # Build the user prompt
        parts: list[str] = []
        if user_prompt and user_prompt.strip():
            parts.append(f"USER REQUEST:\n{user_prompt.strip()}")
        if style and style.strip():
            parts.append(f"STYLE:\n{style.strip()}")
        if width > 0 and height > 0:
            canvas_block = _build_canvas_profile(width, height)
            if canvas_block:
                parts.append(canvas_block)

        final_user_prompt = "\n\n".join(parts) if parts else "Describe a scene vividly."

        # Convert image/video tensors to base64 (vision-capable providers only)
        images_b64: list[str] = []
        if cfg.get("supports_vision"):
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

        # Build the messages array
        messages: list[dict] = []
        if sys_prompt:
            messages.append({"role": "system", "content": sys_prompt})

        if images_b64:
            user_content = _build_image_message_content(final_user_prompt, images_b64)
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": final_user_prompt})

        # Send the request
        try:
            raw = _send_chat_completion(
                base_url=base_url,
                api_key=resolved_key,
                model=model_name,
                messages=messages,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                min_p=min_p,
                repetition_penalty=repetition_penalty,
                max_tokens=max_tokens,
                seed=seed,
                timeout=float(timeout_seconds),
            )
        finally:
            if unload_after_run and provider == "LM Studio (local)":
                ok = _unload_model_lm_studio(base_url, model_name)
                if ok:
                    print(f"[LLM_Prompt_API] Unloaded {model_name} from LM Studio.")
                else:
                    print(f"[LLM_Prompt_API] Unload request for {model_name} did not succeed (model may already be unloaded).")

        # Light cleanup — cloud providers usually return clean text, but a few
        # echo a leading "Assistant:" or similar prefix on rare occasions.
        if output_format == "text":
            cleaned = clean_model_output(raw, OutputCleanConfig(mode="prompt")) or raw
        else:
            cleaned = raw

        self.last_loaded_model = model_name
        self.last_base_url = base_url

        return (cleaned,)


# ---------------------------------------------------------------------------
# ComfyUI registration
# ---------------------------------------------------------------------------

NODE_CLASS_MAPPINGS = {
    "LLMPromptAPI": LLMPromptAPINode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LLMPromptAPI": "LLM Prompt (API)",
}
