"""Gemini Image (API Key) — ONE node for every Google Gemini image model.

Why this exists
---------------
ComfyUI core ships Nano Banana nodes, but they route through
``/proxy/vertexai`` on Comfy credits. The third-party packs need three nodes
chained (key-from-json -> config -> generate) because they have no .env
support. This node is one node, calls Google directly, and reads the same
GEMINI_API_KEY that `LLM Prompt (API)` already uses — so the key never lands
in workflow JSON.

Compatible with LLM Prompt (API)
--------------------------------
`prompt` is a plain STRING widget, so the text output of `LLM Prompt (API)`
(or the GGUF node) wires straight into it — write the prompt with Gemini,
render it with Gemini, one key, one .env, no glue nodes.

Model list is live
------------------
The model combo is built from ListModels on YOUR key, cached to
`.gemini_models.json` beside this file and refreshed every 24h (or on demand
via the `refresh_models` toggle). New Google image models appear on their own;
nothing is hardcoded except a fallback list for when the network is down.

Multimodal — the part most wrappers get wrong
---------------------------------------------
Every Gemini image model is multimodal in BOTH directions:
  * Input  : text + N reference images in one request (editing, character and
             style transfer). The per-model reference budget differs — see CAPS.
  * Output : the response is a *stream of parts*, not an image. One call can
             return interleaved text and several images, and on the thinking
             models (3 Pro Image, 3.1 Flash Image) some of those images are
             INTERIM "thought images" the model drew while composing, flagged
             with `part.thought == True`.
             Naively concatenating every inline_data part gives you a batch of
             half-finished drafts mixed with the real render. This node splits
             them: final images on `IMAGE`, drafts on `thought_images`.
"""

from __future__ import annotations

import io as _io
import json
import time
import urllib.request
from pathlib import Path

import numpy as np
import torch
from PIL import Image

from comfy_api.latest import io

# Same key resolver the LLM Prompt (API) node uses — env var, then
# <ComfyUI root>/.env, then <node folder>/.env. Keys never touch the workflow.
from .llm_prompt_api_node import _resolve_api_key, _comfyui_root

# ---------------------------------------------------------------------------
# Static option sets (verified against google-genai 2.7.0 types.ImageConfig
# and https://ai.google.dev/gemini-api/docs/image-generation, 2026-08-30)
# ---------------------------------------------------------------------------

ASPECT_RATIOS = ["auto", "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4",
                 "9:16", "16:9", "21:9"]

# Uppercase K is mandatory — the API rejects "1k".
IMAGE_SIZES = ["auto", "0.5K", "1K", "2K", "4K"]

# NO person_generation widget on purpose. types.ImageConfig accepts the field
# and the SDK docstring does NOT mark it Vertex-only (unlike output_mime_type,
# output_compression_quality and prominent_people, which it does mark) — but
# sending it on an API key fails at run time with:
#   "person_generation parameter is only supported in Gemini Enterprise Agent
#    Platform mode, not in Gemini Developer API mode."
# Confirmed by Peti in live use 2026-08-30. This node is Developer-API only, so
# the control can never work here; a widget that always errors is worse than no
# widget. Only add it back behind a real Vertex/ADC auth path.

# Same hardcoded BLOCK_NONE the LLM Prompt (API) node uses for Gemini. Google's
# hard server-side filter catches genuinely forbidden content no matter what
# this says; everything else should be unblocked for creative work. Kept as
# plain (category, threshold) pairs so the SafetySetting objects are built
# inside execute(), where `types` is imported.
_SAFETY_BLOCK_NONE = [
    ("HARM_CATEGORY_HARASSMENT", "BLOCK_NONE"),
    ("HARM_CATEGORY_HATE_SPEECH", "BLOCK_NONE"),
    ("HARM_CATEGORY_SEXUALLY_EXPLICIT", "BLOCK_NONE"),
    ("HARM_CATEGORY_DANGEROUS_CONTENT", "BLOCK_NONE"),
]

SAFETY_MODES = ["block_none", "google_default"]

THINKING_LEVELS = ["auto", "minimal", "high"]

RESPONSE_MODES = ["image + text", "image only", "text only"]

# Used only when ListModels can't be reached.
FALLBACK_IMAGE_MODELS = [
    "gemini-3-pro-image",
    "gemini-3-pro-image-preview",
    "nano-banana-pro-preview",
    "gemini-3.1-flash-image",
    "gemini-3.1-flash-image-preview",
    "gemini-3.1-flash-lite-image",
    "gemini-2.5-flash-image",
]

# Preference order for model="auto" — best first.
AUTO_PREFERENCE = [
    "gemini-3-pro-image",
    "nano-banana-pro-preview",
    "gemini-3-pro-image-preview",
    "gemini-3.1-flash-image",
    "gemini-3.1-flash-image-preview",
    "gemini-3.1-flash-lite-image",
    "gemini-2.5-flash-image",
]

# Per-model capabilities. Matched by longest-prefix so unreleased variants
# inherit their family's limits instead of failing.
#   sizes     : image_size values the model actually accepts
#   max_refs  : total reference images the model will take
#   thinking  : "level" = thinking_level supported, "always" = on, can't tune,
#               None = no thinking (sending a thinking_config errors)
CAPS = {
    "gemini-3-pro-image": {
        "label": "Nano Banana Pro",
        "sizes": ["1K", "2K", "4K"], "max_refs": 14, "thinking": "always",
    },
    "nano-banana-pro": {
        "label": "Nano Banana Pro",
        "sizes": ["1K", "2K", "4K"], "max_refs": 14, "thinking": "always",
    },
    "gemini-3.1-flash-lite-image": {
        "label": "Nano Banana 2 Lite",
        "sizes": ["1K"], "max_refs": 14, "thinking": None,
    },
    "gemini-3.1-flash-image": {
        "label": "Nano Banana 2",
        "sizes": ["0.5K", "1K", "2K", "4K"], "max_refs": 17, "thinking": "level",
    },
    "gemini-2.5-flash-image": {
        "label": "Nano Banana (legacy)",
        "sizes": ["1K"], "max_refs": 3, "thinking": None,
    },
}

DEFAULT_CAPS = {"label": "unknown", "sizes": ["1K", "2K", "4K"],
                "max_refs": 14, "thinking": "level"}

MODEL_CACHE_FILE = Path(__file__).resolve().parent / ".gemini_models.json"
MODEL_CACHE_TTL = 24 * 3600  # seconds

LIST_MODELS_URL = ("https://generativelanguage.googleapis.com/v1beta/models"
                   "?pageSize=1000&key={key}")


def _caps_for(model: str) -> dict:
    """Longest-prefix capability lookup, so `-preview` suffixes inherit."""
    best, best_len = DEFAULT_CAPS, -1
    for prefix, caps in CAPS.items():
        if model.startswith(prefix) and len(prefix) > best_len:
            best, best_len = caps, len(prefix)
    return best


# ---------------------------------------------------------------------------
# Live model list
# ---------------------------------------------------------------------------

def _is_image_model(model_id: str, methods: list) -> bool:
    """An image model = generateContent + an image marker in the id.

    Checked against all 53 models the Gemini API currently returns: this
    matches exactly the image family and nothing else (no embeddings, no
    computer-use, no Veo — Veo is predictLongRunning, not generateContent).
    """
    if "generateContent" not in (methods or []):
        return False
    mid = model_id.lower()
    return "image" in mid or "banana" in mid


def _fetch_image_models(api_key: str, timeout: float = 15.0) -> list[str]:
    req = urllib.request.Request(LIST_MODELS_URL.format(key=api_key))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    found = []
    for m in payload.get("models", []):
        mid = (m.get("name") or "").replace("models/", "")
        if mid and _is_image_model(mid, m.get("supportedGenerationMethods")):
            found.append(mid)
    return found


def _read_cache() -> tuple[list[str], float]:
    try:
        data = json.loads(MODEL_CACHE_FILE.read_text(encoding="utf-8"))
        return list(data.get("models") or []), float(data.get("fetched_at") or 0)
    except Exception:
        return [], 0.0


def _write_cache(models: list[str]) -> None:
    try:
        MODEL_CACHE_FILE.write_text(
            json.dumps({"fetched_at": time.time(), "models": models}, indent=2),
            encoding="utf-8")
    except Exception as e:
        print(f"[Gemini Image] Could not write model cache: {e}")


def _available_models(force: bool = False) -> list[str]:
    """Cached-with-TTL live model list. Never raises — falls back to the
    hardcoded list so the node still loads with no network / no key."""
    cached, fetched_at = _read_cache()
    fresh = cached and (time.time() - fetched_at) < MODEL_CACHE_TTL
    if fresh and not force:
        return cached
    key = _resolve_api_key("Gemini", "")
    if key:
        try:
            live = _fetch_image_models(key)
            if live:
                _write_cache(live)
                if force or live != cached:
                    print(f"[Gemini Image] Model list refreshed: {len(live)} image models")
                return live
        except Exception as e:
            print(f"[Gemini Image] ListModels failed ({e}); using cached/fallback list")
    return cached or list(FALLBACK_IMAGE_MODELS)


def _pick_auto(models: list[str]) -> str:
    for want in AUTO_PREFERENCE:
        if want in models:
            return want
    return models[0] if models else FALLBACK_IMAGE_MODELS[0]


# ---------------------------------------------------------------------------
# Tensor helpers (same conventions as grok_imagine_nodes.py)
# ---------------------------------------------------------------------------

def _iter_images(image):
    if image is None:
        return
    if image.ndim == 4:
        for i in range(image.shape[0]):
            yield image[i]
    else:
        yield image


def _tensor_to_png_bytes(tensor: torch.Tensor) -> bytes:
    t = tensor[0] if tensor.ndim == 4 else tensor
    arr = (t.clamp(0, 1) * 255.0).to(torch.uint8).cpu().numpy()
    buf = _io.BytesIO()
    Image.fromarray(arr, mode="RGB").save(buf, format="PNG")
    return buf.getvalue()


def _bytes_to_tensor(data: bytes) -> torch.Tensor:
    img = Image.open(_io.BytesIO(data)).convert("RGB")
    arr = np.asarray(img, dtype=np.float32) / 255.0
    return torch.from_numpy(arr)[None, ...]  # [1,H,W,C]


def _placeholder(size: int = 64) -> torch.Tensor:
    """Black frame — ComfyUI IMAGE outputs can't be None."""
    return torch.zeros((1, size, size, 3), dtype=torch.float32)


def _stack(frames: list[torch.Tensor]) -> torch.Tensor:
    """Concatenate a list of [1,H,W,C] tensors.

    Gemini can return several images of DIFFERENT sizes in one response
    (thought images are usually smaller than the final render), and torch.cat
    would explode. Anything that doesn't match the first frame is resized to it.
    """
    if not frames:
        return _placeholder()
    if len(frames) == 1:
        return frames[0]
    import torch.nn.functional as F
    h, w = frames[0].shape[1], frames[0].shape[2]
    out = [frames[0]]
    for f in frames[1:]:
        if f.shape[1] != h or f.shape[2] != w:
            f = F.interpolate(f.permute(0, 3, 1, 2), size=(h, w),
                              mode="bilinear", align_corners=False).permute(0, 2, 3, 1)
        out.append(f)
    return torch.cat(out, dim=0)


# ---------------------------------------------------------------------------
# The node
# ---------------------------------------------------------------------------

class GeminiImageNode(io.ComfyNode):
    """Text-to-image and image-editing on any Google Gemini image model."""

    @classmethod
    def define_schema(cls) -> io.Schema:
        models = _available_models()
        # "auto" first so a fresh node picks the best model without thinking.
        model_options = ["auto"] + models

        return io.Schema(
            node_id="LunaGeminiImage",
            display_name="Gemini Image (API Key)",
            category="Luna/LLM",
            description=(
                "Google Gemini image generation and editing with your own "
                "GEMINI_API_KEY (no ComfyUI credits, no proxy). Model list is "
                "queried live from your key. Handles multimodal responses — "
                "final images, interim 'thought' images and text come out on "
                "separate outputs."
            ),
            inputs=[
                # ===== CORE =====
                io.String.Input(
                    "prompt", multiline=True, default="",
                    tooltip="What to generate, or how to edit the reference images. "
                            "Wire the text output of LLM Prompt (API) straight in here."),
                io.Combo.Input(
                    "model", options=model_options, default="auto",
                    tooltip="Live list from ListModels on your key, cached 24h. "
                            "'auto' picks the best installed: 3 Pro Image > 3.1 Flash "
                            "Image > 3.1 Flash Lite > 2.5 Flash Image."),
                io.Combo.Input(
                    "aspect_ratio", options=ASPECT_RATIOS, default="auto",
                    tooltip="'auto' lets the model decide (and inherit the aspect of a "
                            "reference image when editing)."),
                io.Combo.Input(
                    "resolution", options=IMAGE_SIZES, default="auto",
                    tooltip="1K~1MP, 2K~4MP, 4K~16MP. 0.5K is 3.1 Flash Image only. "
                            "Nano Banana Pro does 1K/2K/4K; 2.5 Flash Image and 3.1 "
                            "Flash Lite are 1K only — an unsupported size is clamped "
                            "down with a console note instead of failing the run."),
                io.Int.Input(
                    "batch_count", default=1, min=1, max=8,
                    tooltip="How many images. Gemini returns one image per call, so "
                            "this is N sequential calls with the seed stepped by 1. "
                            "N calls = N times the cost."),
                io.Int.Input(
                    "seed", default=0, min=0, max=0x7FFFFFFF, control_after_generate=True),

                # ===== REFERENCE IMAGES (editing / character / style) =====
                io.Image.Input(
                    "reference_images", optional=True,
                    tooltip="Optional. Batch them (any Batch Images node) to send "
                            "several — order is preserved. Budgets: 3 Pro Image 14, "
                            "3.1 Flash Image 17, 3.1 Flash Lite 14, 2.5 Flash Image 3."),
                io.Image.Input(
                    "reference_images_2", optional=True,
                    tooltip="Second reference socket, appended after the first — "
                            "saves a Batch Images node when refs come from two places."),

                # ===== GENERATION SETTINGS =====
                io.String.Input(
                    "system_instruction", multiline=True, default="", optional=True,
                    tooltip="Optional standing art direction, applied before the prompt."),
                io.Float.Input("temperature", default=1.0, min=0.0, max=2.0, step=0.05),
                io.Float.Input("top_p", default=0.95, min=0.0, max=1.0, step=0.01),
                io.Int.Input("top_k", default=64, min=1, max=200),
                io.Combo.Input(
                    "thinking_level", options=THINKING_LEVELS, default="auto",
                    tooltip="3.1 Flash Image only: 'minimal' (fast) or 'high' (better "
                            "composition). Nano Banana Pro always thinks and ignores "
                            "this; the 1K-only models have no thinking and it is not "
                            "sent to them."),
                io.Combo.Input(
                    "safety", options=SAFETY_MODES, default="block_none",
                    tooltip="block_none sends BLOCK_NONE on all four configurable "
                            "categories — the same setting LLM Prompt (API) uses for "
                            "Gemini. Google's hard server-side filter still applies and "
                            "cannot be turned off from here; a block shows up as "
                            "block_reason / finish_reason on the error."),
                io.Combo.Input(
                    "response_mode", options=RESPONSE_MODES, default="image + text",
                    tooltip="What modalities to ask for. 'image + text' is the native "
                            "mode — the model narrates what it drew, which lands on "
                            "the text output."),
                io.Boolean.Input(
                    "include_thought_images", default=False,
                    tooltip="Return the interim images the thinking models draw while "
                            "composing, on the thought_images output. Off = that "
                            "output is a black placeholder frame."),

                # ===== PLUMBING =====
                io.Int.Input("timeout", default=180, min=30, max=900, step=10),
                io.Int.Input(
                    "max_retries", default=2, min=0, max=5,
                    tooltip="Retries on API/network error, with backoff. A safety "
                            "block is NOT retried — it would just fail again."),
                io.Boolean.Input(
                    "refresh_models", default=False,
                    tooltip="Re-query ListModels on the next run and rewrite the cache. "
                            "New models show up in the combo after a browser refresh."),
            ],
            outputs=[
                io.Image.Output("image", tooltip="The final render(s)."),
                io.Image.Output("thought_images", tooltip="Interim composition drafts."),
                io.String.Output("text", tooltip="Any text the model returned alongside."),
                io.String.Output("info", tooltip="Model, settings actually sent, timings."),
            ],
        )

    # -- request ------------------------------------------------------------

    @classmethod
    def _build_config(cls, types, model, caps, aspect_ratio, resolution,
                      temperature, top_p, top_k, seed, system_instruction,
                      thinking_level, response_mode,
                      include_thought_images, safety, notes):
        # Response modalities. The API wants explicit modalities for image models.
        if response_mode == "image only":
            modalities = ["IMAGE"]
        elif response_mode == "text only":
            modalities = ["TEXT"]
        else:
            modalities = ["TEXT", "IMAGE"]

        # image_config — only send fields the user actually set.
        img_kwargs = {}
        if aspect_ratio != "auto":
            img_kwargs["aspect_ratio"] = aspect_ratio
        if resolution != "auto":
            size = resolution
            if size not in caps["sizes"]:
                fallback = caps["sizes"][-1]
                notes.append(f"{model} does not accept {size} - clamped to {fallback}")
                size = fallback
            img_kwargs["image_size"] = size
        # person_generation deliberately never sent — Vertex-only, see the note
        # at the top of this file.

        cfg_kwargs = {
            "response_modalities": modalities,
            "temperature": float(temperature),
            "top_p": float(top_p),
            "top_k": int(top_k),
            "seed": int(seed),
        }
        if img_kwargs:
            cfg_kwargs["image_config"] = types.ImageConfig(**img_kwargs)
        if safety == "block_none":
            cfg_kwargs["safety_settings"] = [
                types.SafetySetting(category=c, threshold=t)
                for c, t in _SAFETY_BLOCK_NONE
            ]
        if system_instruction and system_instruction.strip():
            cfg_kwargs["system_instruction"] = system_instruction.strip()

        # Thinking. Sending a thinking_config to a non-thinking model is an
        # error, and Nano Banana Pro thinks unconditionally with no level to set
        # — but it will still EMIT its thought images if you ask for them.
        think_kwargs = {}
        if thinking_level != "auto":
            if caps["thinking"] == "level":
                think_kwargs["thinking_level"] = thinking_level
            else:
                notes.append(
                    f"thinking_level ignored - {model} is "
                    + ("always-thinking" if caps["thinking"] == "always"
                       else "a non-thinking model"))
        if include_thought_images:
            # Verified 2026-08-30: without include_thoughts the API returns NO
            # thought parts at all, so filtering for them finds nothing.
            if caps["thinking"]:
                think_kwargs["include_thoughts"] = True
            else:
                notes.append(f"no thought images - {model} does not think")
        if think_kwargs:
            cfg_kwargs["thinking_config"] = types.ThinkingConfig(**think_kwargs)

        return types.GenerateContentConfig(**cfg_kwargs)

    @classmethod
    def _build_contents(cls, types, prompt, refs, caps, notes):
        """Reference images FIRST, then the instruction — the order Google's
        editing examples use, and the one the models follow most reliably."""
        parts = []
        if len(refs) > caps["max_refs"]:
            notes.append(
                f"{len(refs)} reference images sent but this model takes "
                f"{caps['max_refs']} — extras may be ignored or rejected")
        for frame in refs:
            parts.append(types.Part.from_bytes(
                data=_tensor_to_png_bytes(frame), mime_type="image/png"))
        if prompt and prompt.strip():
            parts.append(types.Part.from_text(text=prompt.strip()))
        if not parts:
            raise RuntimeError("Nothing to send — the prompt is empty and no "
                               "reference image is connected.")
        return [types.Content(role="user", parts=parts)]

    @staticmethod
    def _split_response(response):
        """Pull a multimodal response apart.

        parts can be: final images, THOUGHT images (part.thought is True on the
        thinking models), thought text, and answer text — interleaved in one
        candidate. Keeping them separate is the whole point of this function.
        """
        finals, thoughts, texts, thought_texts = [], [], [], []
        for cand in (getattr(response, "candidates", None) or []):
            content = getattr(cand, "content", None)
            for part in (getattr(content, "parts", None) or []):
                is_thought = bool(getattr(part, "thought", False))
                inline = getattr(part, "inline_data", None)
                if inline is not None and getattr(inline, "data", None):
                    tensor = _bytes_to_tensor(inline.data)
                    (thoughts if is_thought else finals).append(tensor)
                elif getattr(part, "text", None):
                    (thought_texts if is_thought else texts).append(part.text)
        return finals, thoughts, texts, thought_texts

    # -- execute ------------------------------------------------------------

    @classmethod
    def execute(cls, prompt, model, aspect_ratio, resolution, batch_count, seed,
                system_instruction="", temperature=1.0, top_p=0.95, top_k=64,
                thinking_level="auto", person_generation=None,
                safety="block_none", response_mode="image + text",
                include_thought_images=False,
                timeout=180, max_retries=2, refresh_models=False,
                reference_images=None, reference_images_2=None) -> io.NodeOutput:
        # person_generation is accepted and ignored: workflows saved against the
        # first version of this node still carry the widget value. Vertex-only.

        try:
            from google import genai
            from google.genai import types
        except ImportError:
            raise RuntimeError(
                "google-genai SDK not installed. Run:\n"
                "    python_embeded\\python.exe -m pip install google-genai")

        api_key = _resolve_api_key("Gemini", "")
        if not api_key:
            env_path = _comfyui_root() / ".env"
            raise RuntimeError(
                "No Gemini API key found. Either:\n"
                "  1. Set GEMINI_API_KEY (or GOOGLE_API_KEY) before launching ComfyUI, or\n"
                f"  2. Add this line to {env_path}:\n"
                "       GEMINI_API_KEY=your_key_here\n"
                "The key is read only from env/.env — never stored in the workflow.")

        notes: list[str] = []
        models = _available_models(force=bool(refresh_models))
        if refresh_models:
            notes.append(f"model list refreshed ({len(models)} image models)")

        if model == "auto":
            model = _pick_auto(models)
            notes.append(f"auto -> {model}")
        elif models and model not in models:
            notes.append(f"WARNING: {model} is not in the live model list for this key")

        caps = _caps_for(model)

        refs = list(_iter_images(reference_images)) + list(_iter_images(reference_images_2))

        config = cls._build_config(types, model, caps, aspect_ratio, resolution,
                                   temperature, top_p, top_k, seed,
                                   system_instruction, thinking_level,
                                   response_mode,
                                   bool(include_thought_images), safety, notes)
        contents = cls._build_contents(types, prompt, refs, caps, notes)

        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(timeout) * 1000),  # ms
        )

        all_finals, all_thoughts, all_texts = [], [], []
        started = time.time()
        last_err = None

        for i in range(int(batch_count)):
            if i > 0:  # step the seed so a batch isn't N copies of one image
                config.seed = int(seed) + i
            attempt = 0
            last_err = None
            while attempt <= int(max_retries):
                try:
                    response = client.models.generate_content(
                        model=model, contents=contents, config=config)
                    finals, thoughts, texts, _ = cls._split_response(response)
                    if not finals and response_mode != "text only":
                        # Almost always a block. Two different places carry the
                        # reason: prompt_feedback when the PROMPT was rejected
                        # outright, finish_reason when the generated image was.
                        pf = getattr(response, "prompt_feedback", None)
                        block_reason = getattr(pf, "block_reason", None)
                        finish = ""
                        for cand in (getattr(response, "candidates", None) or []):
                            fr = getattr(cand, "finish_reason", None)
                            if fr:
                                finish = str(fr)
                                break
                        detail = ", ".join(
                            p for p in (
                                f"block_reason={block_reason}" if block_reason else "",
                                f"finish_reason={finish}" if finish else "",
                            ) if p) or "no reason given"
                        raise RuntimeError(
                            f"No image returned ({detail}"
                            + (", safety=block_none was already set"
                               if safety == "block_none" else
                               ", try safety=block_none")
                            + "). " + (" ".join(texts)[:300] if texts else ""))
                    all_finals.extend(finals)
                    all_thoughts.extend(thoughts)
                    all_texts.extend(texts)
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    msg = str(e)
                    # A block or a bad request will fail identically on retry.
                    if ("finish_reason" in msg or "INVALID_ARGUMENT" in msg
                            or "400" in msg):
                        break
                    attempt += 1
                    if attempt > int(max_retries):
                        break
                    wait = 2 ** attempt
                    print(f"[Gemini Image] call {i+1} failed ({e}); retry {attempt} in {wait}s")
                    time.sleep(wait)
            if last_err is not None:
                notes.append(f"image {i+1}/{batch_count} failed: {last_err}")

        if not all_finals and response_mode != "text only":
            raise RuntimeError(f"Gemini image request failed: {last_err}")

        elapsed = time.time() - started

        image_out = _stack(all_finals) if all_finals else _placeholder()
        thought_out = _stack(all_thoughts) if (include_thought_images and all_thoughts) \
            else _placeholder()
        text_out = "\n".join(t.strip() for t in all_texts if t and t.strip())

        info_lines = [
            f"model      : {model} ({caps['label']})",
            f"images     : {len(all_finals)}"
            + (f"  thought: {len(all_thoughts)}" if all_thoughts else ""),
            f"size       : {image_out.shape[2]}x{image_out.shape[1]}"
            f"  requested {resolution} / {aspect_ratio}",
            f"refs sent  : {len(refs)} (model max {caps['max_refs']})",
            f"seed       : {seed}" + (f"..{seed + batch_count - 1}" if batch_count > 1 else ""),
            f"elapsed    : {elapsed:.1f}s",
        ]
        info_lines.extend(f"note       : {n}" for n in notes)
        info = "\n".join(info_lines)
        print(f"[Gemini Image] {model}: {len(all_finals)} image(s) in {elapsed:.1f}s")
        for n in notes:
            print(f"[Gemini Image] note: {n}")

        return io.NodeOutput(image_out, thought_out, text_out, info)


# ---------------------------------------------------------------------------
# ComfyUI registration
# ---------------------------------------------------------------------------

NODE_CLASS_MAPPINGS = {
    "LunaGeminiImage": GeminiImageNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LunaGeminiImage": "Gemini Image (API Key)",
}
