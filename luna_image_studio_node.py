"""Luna Image Studio (API Key) — one image node over every provider.

A thin ComfyUI adapter over the ComfyUI-free core `luna_imaging/`: tensors <-> PIL,
key resolution, then one call to `studio.run(req, key)`. All provider, region,
crop/composite and outpaint logic lives in the core (SPEC › 5).

Keys come from env / .env only (never a widget, never in the workflow):
OpenAI -> OPENAI_API_KEY, Gemini -> GEMINI_API_KEY, xAI -> XAI_API_KEY.
"""

from __future__ import annotations

import json

import numpy as np
import torch
from PIL import Image, ImageChops

from comfy_api.latest import io

from .gemini_image_node import FALLBACK_IMAGE_MODELS
from .grok_imagine_nodes import _IMAGE_MODELS as _GROK_IMAGE_MODELS
from .grok_imagine_nodes import _resolve_xai_key
from .llm_prompt_api_node import _comfyui_root, _resolve_api_key
from .luna_imaging import studio
from .luna_imaging.capabilities import provider_for
from .luna_imaging.masks import boxes_to_mask
from .luna_imaging.types import EditRequest
from .openai_image_node import ASPECT_RATIOS, REF_SLOTS, _collect_refs, _resize_exact, _stack
from .openai_image_node import MODELS as _OPENAI_MODELS

MODELS = list(dict.fromkeys(_OPENAI_MODELS + FALLBACK_IMAGE_MODELS + _GROK_IMAGE_MODELS))
OPERATIONS = ["generate", "edit", "inpaint", "outpaint", "compose"]
RESOLUTIONS = ["auto", "0.5K", "1K", "1.5K", "2K", "4K"]
QUALITIES = ["auto", "low", "medium", "high", "xhigh", "max"]
BACKGROUNDS = ["auto", "opaque", "transparent"]
MASK_MODES = ["auto", "native", "crop"]

_BBOX_FORMAT = ("bboxes must be JSON [[x, y, w, h], ...] - numbers, in pixels, or all "
                "values <= 1 for fractions of the first image")


# ---------------------------------------------------------------------------
# Tensors <-> PIL
# ---------------------------------------------------------------------------

def _to_pil(frame: torch.Tensor) -> Image.Image:
    arr = (frame[..., :3].clamp(0, 1) * 255.0).round().to(torch.uint8).cpu().numpy()
    return Image.fromarray(arr, mode="RGB")


def _mask_to_pil(mask: torch.Tensor, invert: bool) -> Image.Image:
    m = mask[0] if mask.ndim == 3 else mask
    m = m.clamp(0, 1)
    if invert:
        m = 1.0 - m
    return Image.fromarray((m * 255.0).round().to(torch.uint8).cpu().numpy(), mode="L")


def _image_tensor(img: Image.Image) -> torch.Tensor:
    arr = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    return torch.from_numpy(arr)[None]


def _mask_tensor(m: Image.Image) -> torch.Tensor:
    arr = np.asarray(m.convert("L"), dtype=np.float32) / 255.0
    return torch.from_numpy(arr)[None]


def _parse_bboxes(text: str) -> list[list[float]]:
    text = (text or "").strip()
    if not text:
        return []
    try:
        boxes = json.loads(text)
    except ValueError:
        raise ValueError(_BBOX_FORMAT) from None
    ok = isinstance(boxes, list) and all(
        isinstance(b, list) and len(b) == 4
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in b)
        for b in boxes)
    if not ok:
        raise ValueError(_BBOX_FORMAT)
    return boxes


def _resolve_key(provider: str) -> str:
    if provider == "xai":
        return _resolve_xai_key()  # raises with the env-var / .env instructions
    label, env = ("OpenAI", "OPENAI_API_KEY") if provider == "openai" else ("Gemini", "GEMINI_API_KEY")
    key = _resolve_api_key(label, "")
    if not key:
        raise RuntimeError(
            f"No {label} API key found. Set {env} or add it to "
            f"{_comfyui_root() / '.env'} — it is never stored in the workflow.")
    return key


# ---------------------------------------------------------------------------
# The node
# ---------------------------------------------------------------------------

class LunaImageStudio(io.ComfyNode):
    """Generate, edit, inpaint, outpaint and compose on OpenAI, Gemini or Grok."""

    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="LunaImageStudio",
            display_name="Luna Image Studio (API Key)",
            category="Luna/LLM",
            description=(
                "One image node for OpenAI GPT Image, Gemini and Grok Imagine on your own "
                "keys. Region edits work on every provider: native mask on OpenAI, crop + "
                "composite elsewhere. Pixels outside the region always stay the original's."
            ),
            inputs=[
                io.String.Input(
                    "prompt", multiline=True, default="",
                    tooltip="What to generate, or how to change the image / region."),
                io.Combo.Input(
                    "model", options=MODELS, default=MODELS[0],
                    tooltip="The provider follows the model: gpt-/chatgpt- = OpenAI, "
                            "gemini-/nano-banana = Gemini, grok- = xAI."),
                io.Combo.Input(
                    "operation", options=OPERATIONS, default="edit",
                    tooltip="generate: no images. edit / compose: whole images as input. "
                            "inpaint: change only the mask / bbox region of the first image. "
                            "outpaint: extend the first image by the outpaint margins."),
                io.Combo.Input(
                    "aspect_ratio", options=ASPECT_RATIOS, default="auto",
                    tooltip="Region edits follow the crop / image size instead."),
                io.Combo.Input(
                    "resolution", options=RESOLUTIONS, default="1K",
                    tooltip="Pixel budget. Each provider takes the sizes it supports; "
                            "anything changed is noted in info."),
                io.Int.Input(
                    "batch_count", default=1, min=1, max=8,
                    tooltip="Images per run. Each is billed."),
                io.Int.Input(
                    "seed", default=0, min=0, max=0x7FFFFFFF, control_after_generate=True,
                    tooltip="Re-run trigger only — it is not sent to the provider."),

                io.Autogrow.Input(
                    "reference_images", optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        input=io.Image.Input("ref"),
                        prefix="reference_image_", min=0, max=REF_SLOTS,
                    ),
                    tooltip="Input images. Sockets grow as you connect them; a batch in one "
                            "slot is expanded. The first image is the one inpaint / outpaint "
                            "work on; the rest are references."),
                io.Mask.Input(
                    "mask", optional=True,
                    tooltip="ComfyUI MASK (white = region to change) for the first image. "
                            "Any resolution — it is fitted to the image."),
                io.Boolean.Input(
                    "invert_mask", default=False, optional=True,
                    tooltip="Change everything EXCEPT the white area (applies to the MASK, "
                            "not to bboxes)."),
                io.String.Input(
                    "bboxes", default="", optional=True,
                    tooltip="Optional region boxes as JSON [[x, y, w, h], ...] on the first "
                            "image — pixels, or fractions when every value is <= 1. Unioned "
                            "with the mask."),
                io.Combo.Input(
                    "mask_mode", options=MASK_MODES, default="auto",
                    tooltip="auto: native mask where the provider has one (OpenAI), crop + "
                            "composite elsewhere. crop: always crop + composite."),
                io.Float.Input(
                    "crop_padding", default=0.25, min=0.0, max=1.0, step=0.05,
                    tooltip="Crop mode: context around the region, as a fraction of its size."),
                io.Int.Input(
                    "feather", default=16, min=0, max=128,
                    tooltip="Soft blend in pixels, inward only — pixels outside the region "
                            "never change."),
                io.Int.Input("outpaint_left", default=0, min=0, max=2048),
                io.Int.Input("outpaint_top", default=0, min=0, max=2048),
                io.Int.Input("outpaint_right", default=0, min=0, max=2048),
                io.Int.Input("outpaint_bottom", default=0, min=0, max=2048),
                io.Combo.Input(
                    "quality", options=QUALITIES, default="auto",
                    tooltip="Sent where the provider has it (xhigh / max: gpt-image-2.5 only)."),
                io.Combo.Input(
                    "background", options=BACKGROUNDS, default="auto",
                    tooltip="transparent: OpenAI except gpt-image-2. The alpha comes out on "
                            "the mask output."),

                io.Int.Input("width", default=0, min=0, max=32768, optional=True,
                             force_input=True,
                             tooltip="Optional, from a size node. With height, overrides "
                                     "aspect_ratio / resolution."),
                io.Int.Input("height", default=0, min=0, max=32768, optional=True,
                             force_input=True, tooltip="See width."),
                io.Int.Input("timeout", default=600, min=30, max=1800, step=10),
                io.Int.Input("max_retries", default=2, min=0, max=5,
                             tooltip="Retries on 5xx / 429 / network errors."),
            ],
            outputs=[
                io.Image.Output("image", tooltip="The result(s)."),
                io.Mask.Output("mask", tooltip="The region actually changed (1 = edited), or "
                                               "the alpha when background is transparent."),
                io.String.Output("text", tooltip="Any text the model returned."),
                io.String.Output("info", tooltip="Model, operation, mode, notes and cost."),
            ],
        )

    @classmethod
    def execute(cls, prompt, model, operation, aspect_ratio, resolution, batch_count, seed,
                reference_images=None, mask=None, invert_mask=False, bboxes="",
                mask_mode="auto", crop_padding=0.25, feather=16,
                outpaint_left=0, outpaint_top=0, outpaint_right=0, outpaint_bottom=0,
                quality="auto", background="auto", width=0, height=0,
                timeout=600, max_retries=2) -> io.NodeOutput:
        # seed is a re-run trigger only; it is deliberately not part of the request.
        images = [_to_pil(f) for f in _collect_refs(reference_images)]

        region = _mask_to_pil(mask, bool(invert_mask)) if mask is not None else None
        boxes = _parse_bboxes(bboxes)
        if boxes:
            if not images:
                raise ValueError("bboxes need an image to apply to (reference_image_1)")
            size = images[0].size
            normalized = all(abs(v) <= 1 for b in boxes for v in b)
            box_mask = boxes_to_mask(boxes, size, normalized)
            if region is None:
                region = box_mask
            else:
                if region.size != size:
                    region = region.resize(size, Image.NEAREST)
                region = ImageChops.lighter(region, box_mask)

        provider = provider_for(model)
        key = _resolve_key(provider)

        req = EditRequest(
            provider=provider, model=model, operation=operation, prompt=(prompt or "").strip(),
            images=images, mask=region, aspect_ratio=aspect_ratio, resolution=resolution,
            n=int(batch_count), quality=quality, background=background,
            outpaint=(int(outpaint_left), int(outpaint_top), int(outpaint_right),
                      int(outpaint_bottom)),
            mask_mode=mask_mode, crop_padding=float(crop_padding), feather_px=int(feather),
            width=int(width or 0), height=int(height or 0),
            timeout=float(timeout), max_retries=int(max_retries),
        )
        result, used_mask = studio.run(req, key)
        if not result.images:
            raise RuntimeError(f"{model} returned no image. " + " | ".join(result.info))

        image_t = _stack([_image_tensor(im) for im in result.images])
        n, h, w = image_t.shape[0], image_t.shape[1], image_t.shape[2]
        if background == "transparent" and any(im.mode in ("RGBA", "LA") for im in result.images):
            mask_t = _stack([_mask_tensor(im.convert("RGBA").getchannel("A"))
                             for im in result.images], mask=True)
        elif used_mask is not None:
            m = used_mask.convert("L")
            if m.size != (w, h):
                m = m.resize((w, h), Image.NEAREST)
            mask_t = _mask_tensor(m).repeat(n, 1, 1)
        else:
            mask_t = torch.zeros((n, h, w), dtype=torch.float32)
        if (mask_t.shape[1], mask_t.shape[2]) != (h, w):
            mask_t = _resize_exact(mask_t, w, h, mask=True)

        cost = f"${result.cost_usd:.4f}" if result.cost_usd is not None else "n/a"
        info = "\n".join(
            [f"model : {model} ({provider})",
             f"operation : {operation} ({len(images)} image(s)"
             + (", mask" if region is not None else "") + f") -> {n} x {w}x{h}"]
            + list(result.info)
            + [f"cost : {cost}"])
        print(f"[Luna Image Studio] {model} {operation}: {n} image(s) {w}x{h}, cost {cost}")
        return io.NodeOutput(image_t, mask_t, result.text or "", info)


NODE_CLASS_MAPPINGS = {"LunaImageStudio": LunaImageStudio}
NODE_DISPLAY_NAME_MAPPINGS = {"LunaImageStudio": "Luna Image Studio (API Key)"}
