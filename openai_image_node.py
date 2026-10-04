"""GPT Image (API Key) — OpenAI image generation and editing on your own key.

Calls OpenAI's Images API directly with OPENAI_API_KEY (env / .env, same
resolver as the other nodes — never a widget, never in the workflow). No Codex
CLI, no ComfyUI credits, no proxy. The Codex route lives in ComfyUI-GPTImage2.

  no reference image -> POST /v1/images/generations   (JSON)
  reference image(s) -> POST /v1/images/edits         (multipart, image[] + mask)

Everything below was LIVE-VERIFIED on 2026-10-04 (~100 low-quality calls):
  - gpt-image-2.5-flare / -sunburst (launched 2026-09-08) and gpt-image-2 take
    ANY WxH: both edges divisible by 16, longest edge <= 3840. Docs add: aspect
    1:3..3:1, 655,360..8,294,400 total pixels, >2560x1440 "experimental".
  - The 1.x family and chatgpt-image-latest take ONLY 1024x1024, 1024x1536,
    1536x1024 or auto.
  - quality xhigh / max exist on 2.5 only — and the 400 for a bad value on 2.5
    lists only low/medium/high/auto, so don't trust that message.
  - background=transparent: every model EXCEPT gpt-image-2.
  - input_fidelity: gpt-image-1, gpt-image-1.5, chatgpt-image-latest only.
  - moderation: auto | low.
  - Edits accepted 101 input images on the 2.x models (and 17 on 1.x, the most
    tried) and billed them all; the documented cap was ~16.
  - flare vs sunburst: identical parameters and errors. OpenAI positions flare
    as the fast default and sunburst for precision edits / fine detail.
  - Response: data[].b64_json (never a URL), plus usage with image token counts.
  - No seed parameter exists; the seed widget is only a re-run trigger.
"""

from __future__ import annotations

import base64
import io as _io
import json
import math
import time
import urllib.error
import urllib.request
import uuid

import numpy as np
import torch
from PIL import Image

from comfy_api.latest import io

from .llm_prompt_api_node import _resolve_api_key, _comfyui_root

_BASE = "https://api.openai.com/v1"

MODELS = [
    "gpt-image-2.5-flare",
    "gpt-image-2.5-sunburst",
    "gpt-image-2",
    "gpt-image-1.5",
    "gpt-image-1",
    "gpt-image-1-mini",
    "chatgpt-image-latest",
]

ASPECT_RATIOS = ["auto", "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4",
                 "9:16", "16:9", "21:9", "1:3", "3:1"]
# Target pixel budgets for the free-size (2.x) models. 4K is the API's
# 8,294,400-pixel ceiling (3840x2160).
RESOLUTIONS = {"auto": None, "1K": 1024 * 1024, "2K": 2048 * 2048, "4K": 3840 * 2160}
QUALITIES = ["auto", "low", "medium", "high", "xhigh", "max"]
BACKGROUNDS = ["auto", "opaque", "transparent"]
FORMATS = ["png", "webp", "jpeg"]
MODERATION = ["auto", "low"]
FIDELITY = ["auto", "low", "high"]

_FIXED_SIZES = [(1024, 1024), (1536, 1024), (1024, 1536)]
_MIN_PIX, _MAX_PIX, _MAX_EDGE = 655_360, 8_294_400, 3840
REF_SLOTS = 16  # UI cap; the 2.x API took 101 live — a batch per slot goes past it


def _free_size(model: str) -> bool:
    return model.startswith("gpt-image-2")


def _is_25(model: str) -> bool:
    return model.startswith("gpt-image-2.5")


def _snap16(v: float) -> int:
    return max(16, int(round(v / 16.0)) * 16)


def _fit_free(w: float, h: float) -> tuple[int, int]:
    """Clamp an arbitrary target into the 2.x rules: /16, edge <= 3840,
    aspect 1:3..3:1, pixel count inside the documented window."""
    ar = max(1 / 3, min(3.0, w / h))
    pix = max(_MIN_PIX, min(_MAX_PIX, w * h))
    w = math.sqrt(pix * ar)
    h = w / ar
    scale = min(1.0, _MAX_EDGE / max(w, h))
    w, h = _snap16(w * scale), _snap16(h * scale)
    # Rounding can step over either bound. Live: 1000x600 snapped to 1040x624
    # (648,960 px) and 400'd "below the current minimum pixel budget".
    while w * h > _MAX_PIX:
        w, h = (w - 16, h) if w >= h else (w, h - 16)
    while w * h < _MIN_PIX:
        w, h = (w, h + 16) if w >= h else (w + 16, h)
    return w, h


def _ar_float(ar: str) -> float:
    a, b = ar.split(":")
    return float(a) / float(b)


def _resolve_size(model, aspect_ratio, resolution, width, height, notes) -> str:
    """Widgets (or wired width/height) -> the API's `size` string."""
    wired = bool(width and height and width > 0 and height > 0)
    if _free_size(model):
        if wired:
            w, h = _fit_free(float(width), float(height))
            if (w, h) != (int(width), int(height)):
                notes.append(f"{width}x{height} -> {w}x{h} (API rules; output resized back)")
            return f"{w}x{h}"
        if aspect_ratio == "auto" and resolution == "auto":
            return "auto"
        pix = RESOLUTIONS[resolution] or RESOLUTIONS["1K"]
        ar = 1.0 if aspect_ratio == "auto" else _ar_float(aspect_ratio)
        w, h = _fit_free(math.sqrt(pix * ar), math.sqrt(pix / ar))
        return f"{w}x{h}"
    # 1.x / chatgpt-image-latest: three fixed sizes.
    if not wired and aspect_ratio == "auto":
        return "auto"
    ar = (float(width) / float(height)) if wired else _ar_float(aspect_ratio)
    w, h = min(_FIXED_SIZES, key=lambda s: abs(math.log((s[0] / s[1]) / ar)))
    if resolution not in ("auto", "1K"):
        notes.append(f"{model} has fixed sizes only - {resolution} ignored")
    return f"{w}x{h}"


# ---------------------------------------------------------------------------
# Tensors
# ---------------------------------------------------------------------------

def _iter_images(image):
    if image is None:
        return
    if image.ndim == 4:
        for i in range(image.shape[0]):
            yield image[i]
    else:
        yield image


def _collect_refs(src) -> list:
    if src is None:
        return []
    if isinstance(src, dict):
        def _slot(name):
            tail = str(name).rsplit("_", 1)[-1]
            return int(tail) if tail.isdigit() else 0
        out = []
        for _, v in sorted(src.items(), key=lambda kv: _slot(kv[0])):
            out.extend(_iter_images(v))
        return out
    return list(_iter_images(src))


def _png_bytes(frame: torch.Tensor) -> bytes:
    arr = (frame.clamp(0, 1) * 255.0).to(torch.uint8).cpu().numpy()
    buf = _io.BytesIO()
    Image.fromarray(arr, mode="RGB").save(buf, format="PNG")
    return buf.getvalue()


def _mask_png(mask: torch.Tensor, size: tuple[int, int], invert: bool) -> bytes:
    """ComfyUI MASK (1 = selected) -> OpenAI mask PNG, where ALPHA 0 marks the
    area to repaint. Resized to the first image, which the API requires."""
    m = mask[0] if mask.ndim == 3 else mask
    m = m.clamp(0, 1).cpu().numpy()
    if invert:
        m = 1.0 - m
    alpha = ((1.0 - m) * 255.0).astype(np.uint8)
    img = Image.fromarray(alpha, mode="L").resize(size, Image.NEAREST)
    rgba = Image.new("RGBA", size, (0, 0, 0, 255))
    rgba.putalpha(img)
    buf = _io.BytesIO()
    rgba.save(buf, format="PNG")
    return buf.getvalue()


def _decode(b64: str) -> tuple[torch.Tensor, torch.Tensor]:
    """-> (IMAGE [1,H,W,3], MASK [1,H,W]); mask = alpha (1 = opaque)."""
    img = Image.open(_io.BytesIO(base64.b64decode(b64)))
    rgba = img.convert("RGBA")
    arr = np.asarray(rgba, dtype=np.float32) / 255.0
    return torch.from_numpy(arr[..., :3])[None], torch.from_numpy(arr[..., 3])[None]


def _resize_exact(t: torch.Tensor, w: int, h: int, mask: bool = False) -> torch.Tensor:
    import torch.nn.functional as F
    if mask:
        return F.interpolate(t[:, None], size=(h, w), mode="bilinear",
                             align_corners=False)[:, 0].clamp(0, 1)
    x = F.interpolate(t.permute(0, 3, 1, 2), size=(h, w), mode="bicubic", align_corners=False)
    return x.permute(0, 2, 3, 1).clamp(0, 1).contiguous()


def _stack(frames: list[torch.Tensor], mask: bool = False) -> torch.Tensor:
    if len(frames) == 1:
        return frames[0]
    h, w = frames[0].shape[1], frames[0].shape[2]
    return torch.cat([f if (f.shape[1], f.shape[2]) == (h, w) else _resize_exact(f, w, h, mask)
                      for f in frames], dim=0)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _multipart(fields: dict, files: list[tuple[str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "----LunaGPTImage" + uuid.uuid4().hex
    out = _io.BytesIO()
    for k, v in fields.items():
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                  f"{v}\r\n".encode("utf-8"))
    for field, fname, data in files:
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
                  f"filename=\"{fname}\"\r\nContent-Type: image/png\r\n\r\n".encode("utf-8"))
        out.write(data)
        out.write(b"\r\n")
    out.write(f"--{boundary}--\r\n".encode("utf-8"))
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def _call(path: str, key: str, timeout: float, json_body: dict | None = None,
          multipart: tuple[bytes, str] | None = None) -> dict:
    if json_body is not None:
        data, ctype = json.dumps(json_body).encode("utf-8"), "application/json"
    else:
        data, ctype = multipart
    req = urllib.request.Request(f"{_BASE}{path}", data=data, method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body).get("error", {}).get("message") or body
        except Exception:
            msg = body
        err = RuntimeError(f"OpenAI HTTP {e.code}: {msg[:600]}")
        err.status = e.code
        raise err from None


# ---------------------------------------------------------------------------
# The node
# ---------------------------------------------------------------------------

class OpenAIImageNode(io.ComfyNode):
    """Text-to-image and image editing (multi-image + mask) on OpenAI GPT Image."""

    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="LunaOpenAIImage",
            display_name="GPT Image (API Key)",
            category="Luna/LLM",
            description=(
                "OpenAI GPT Image generation and editing with your own "
                "OPENAI_API_KEY — no Codex, no ComfyUI credits. Connect reference "
                "images to edit; add a MASK to repaint only part of the first one."
            ),
            inputs=[
                # ===== CORE (same order as Gemini Image) =====
                io.String.Input(
                    "prompt", multiline=True, default="",
                    tooltip="What to generate, or how to edit the reference images."),
                io.Combo.Input(
                    "model", options=MODELS, default=MODELS[0],
                    tooltip="2.5-flare = fast default; 2.5-sunburst = precision "
                            "edits / fine detail (same parameters, live-checked). "
                            "2.x take any size; 1.x and chatgpt-image-latest only "
                            "1024x1024 / 1536x1024 / 1024x1536."),
                io.Combo.Input(
                    "aspect_ratio", options=ASPECT_RATIOS, default="auto",
                    tooltip="'auto' with resolution 'auto' lets the model choose. "
                            "On 1.x models the nearest of the three fixed sizes is used."),
                io.Combo.Input(
                    "resolution", options=list(RESOLUTIONS), default="1K",
                    tooltip="Pixel budget for the 2.x models: 1K ~1 MP, 2K ~4 MP, "
                            "4K = 3840x2160 (the API ceiling; above 2560x1440 is "
                            "'experimental'). Sizes snap to multiples of 16."),
                io.Int.Input(
                    "batch_count", default=1, min=1, max=8,
                    tooltip="Images per call (the API's `n`). Each is billed."),
                io.Int.Input(
                    "seed", default=0, min=0, max=0x7FFFFFFF, control_after_generate=True,
                    tooltip="Re-run trigger only — the Images API has no seed."),

                # ===== EDIT INPUTS =====
                io.Autogrow.Input(
                    "reference_images", optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        input=io.Image.Input("ref"),
                        prefix="reference_image_", min=0, max=REF_SLOTS,
                    ),
                    tooltip="Optional. Any image connected switches to the edit "
                            "endpoint. Sockets grow as you connect them; a batch in "
                            "one slot is expanded. The first image is the one the "
                            "mask applies to; the rest are references. Live: 101 "
                            "images accepted on 2.x, 17 on 1.x."),
                io.Mask.Input(
                    "mask", optional=True,
                    tooltip="Optional. ComfyUI MASK (white = area to repaint) for the "
                            "FIRST reference image — from the mask editor, SAM, or any "
                            "segmentation node. Converted to OpenAI's alpha mask."),
                io.Boolean.Input(
                    "invert_mask", default=False, optional=True,
                    tooltip="Repaint everything EXCEPT the white area."),

                # ===== GENERATION SETTINGS =====
                io.Combo.Input(
                    "quality", options=QUALITIES, default="auto",
                    tooltip="xhigh / max exist on 2.5 only; on other models they are "
                            "lowered to high with a note."),
                io.Combo.Input(
                    "background", options=BACKGROUNDS, default="auto",
                    tooltip="transparent works on every model except gpt-image-2, and "
                            "needs png or webp. The alpha comes out on the `alpha` output."),
                io.Combo.Input("output_format", options=FORMATS, default="png"),
                io.Int.Input("output_compression", default=100, min=0, max=100,
                             tooltip="jpeg / webp only."),
                io.Combo.Input("moderation", options=MODERATION, default="low",
                               tooltip="'low' is the least restrictive value the API takes."),
                io.Combo.Input(
                    "input_fidelity", options=FIDELITY, default="auto",
                    tooltip="Edit only, and only gpt-image-1 / 1.5 / chatgpt-image-latest "
                            "(the others 400 on it — it is not sent to them)."),

                # ===== PLUMBING =====
                io.Int.Input("width", default=0, min=0, max=32768, optional=True,
                             force_input=True,
                             tooltip="Optional, from a size node. With height, overrides "
                                     "aspect_ratio/resolution and the output is resized to "
                                     "exactly this size."),
                io.Int.Input("height", default=0, min=0, max=32768, optional=True,
                             force_input=True, tooltip="See width."),
                io.Int.Input("timeout", default=300, min=30, max=900, step=10,
                             tooltip="Complex prompts can take ~2 minutes."),
                io.Int.Input("max_retries", default=2, min=0, max=5,
                             tooltip="Retries on 5xx / network errors. A 400 is not retried."),
            ],
            outputs=[
                io.Image.Output("image", tooltip="The render(s)."),
                io.Mask.Output("alpha", tooltip="Alpha channel (1 = opaque). Useful with "
                                                "background=transparent."),
                io.String.Output("info", tooltip="Model, size and settings sent, usage, timing."),
            ],
        )

    @classmethod
    def execute(cls, prompt, model, aspect_ratio, resolution, batch_count, seed,
                reference_images=None, mask=None, invert_mask=False,
                quality="auto", background="auto", output_format="png",
                output_compression=100, moderation="low", input_fidelity="auto",
                width=0, height=0, timeout=300, max_retries=2) -> io.NodeOutput:
        key = _resolve_api_key("OpenAI", "")
        if not key:
            raise RuntimeError(
                "No OpenAI API key found. Set OPENAI_API_KEY or add it to "
                f"{_comfyui_root() / '.env'} — it is never stored in the workflow.")
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("Prompt is required.")

        notes: list[str] = []
        refs = _collect_refs(reference_images)
        size = _resolve_size(model, aspect_ratio, resolution, width, height, notes)

        if quality in ("xhigh", "max") and not _is_25(model):
            notes.append(f"{model} has no quality '{quality}' - sent 'high'")
            quality = "high"
        if background == "transparent":
            if model == "gpt-image-2":
                notes.append("gpt-image-2 does not support transparent - sent 'auto'")
                background = "auto"
            elif output_format == "jpeg":
                notes.append("transparent needs png/webp - output_format switched to png")
                output_format = "png"

        params = {
            "model": model, "prompt": prompt, "n": int(batch_count), "size": size,
            "quality": quality, "background": background,
            "output_format": output_format, "moderation": moderation,
        }
        if output_format in ("jpeg", "webp"):
            params["output_compression"] = int(output_compression)

        if refs:
            path = "/images/edits"
            if input_fidelity != "auto":
                if model in ("gpt-image-1", "gpt-image-1.5", "chatgpt-image-latest"):
                    params["input_fidelity"] = input_fidelity
                else:
                    notes.append(f"input_fidelity not supported by {model} - not sent")
            files = [("image[]", f"ref_{i:02d}.png", _png_bytes(f)) for i, f in enumerate(refs)]
            if mask is not None:
                first = refs[0]
                files.append(("mask", "mask.png",
                              _mask_png(mask, (first.shape[1], first.shape[0]), bool(invert_mask))))
            payload = {"multipart": _multipart({k: str(v) for k, v in params.items()}, files)}
        else:
            path = "/images/generations"
            if mask is not None:
                notes.append("mask ignored - it needs a reference image to apply to")
            payload = {"json_body": params}

        started = time.time()
        resp, attempt = None, 0
        while True:
            try:
                resp = _call(path, key, float(timeout), **payload)
                break
            except Exception as e:
                status = getattr(e, "status", None)
                attempt += 1
                if (status is not None and status < 500 and status != 429) or attempt > int(max_retries):
                    raise
                wait = 2 ** attempt
                print(f"[GPT Image] {e}; retry {attempt} in {wait}s")
                time.sleep(wait)
        elapsed = time.time() - started

        decoded = [_decode(d["b64_json"]) for d in (resp.get("data") or []) if d.get("b64_json")]
        if not decoded:
            raise RuntimeError(f"No image in the OpenAI response: {json.dumps(resp)[:500]}")
        images = _stack([d[0] for d in decoded])
        alphas = _stack([d[1] for d in decoded], mask=True)
        if width and height and width > 0 and height > 0 and \
                (images.shape[2], images.shape[1]) != (int(width), int(height)):
            images = _resize_exact(images, int(width), int(height))
            alphas = _resize_exact(alphas, int(width), int(height), mask=True)

        usage = resp.get("usage") or {}
        info = "\n".join([
            f"model      : {model}",
            f"endpoint   : {path}" + (f" ({len(refs)} image(s)"
                                       + (", mask" if (refs and mask is not None) else "") + ")"
                                       if refs else ""),
            f"size       : sent {size}, got {images.shape[2]}x{images.shape[1]}",
            f"settings   : quality={quality} background={background} "
            f"format={output_format} moderation={moderation}",
            f"images     : {images.shape[0]}",
            f"usage      : {json.dumps(usage)[:300]}",
            f"elapsed    : {elapsed:.1f}s",
        ] + [f"note       : {n}" for n in notes])
        print(f"[GPT Image] {model} {path}: {images.shape[0]} image(s) {size} in {elapsed:.1f}s")
        for n in notes:
            print(f"[GPT Image] note: {n}")
        return io.NodeOutput(images, alphas, info)


NODE_CLASS_MAPPINGS = {"LunaOpenAIImage": OpenAIImageNode}
NODE_DISPLAY_NAME_MAPPINGS = {"LunaOpenAIImage": "GPT Image (API Key)"}
