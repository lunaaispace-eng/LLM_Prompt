"""Studio orchestrator: validation, region editing (native mask or crop-composite), outpaint.

Pixels outside the region always stay the original's: every inpaint/outpaint result is
composited back onto the original, whichever provider made it (SPEC › 5.5).
"""
from __future__ import annotations

import dataclasses

from PIL import Image

from .capabilities import caps_for, provider_for
from .masks import composite, expand_box, fit_mask, mask_bbox, outpaint_canvas
from .providers import gemini as _gemini
from .providers import openai as _openai
from .providers import xai as _xai
from .sizes import nearest_aspect
from .types import EditRequest, EditResult

CROP_PREFIX = "Edit only the marked region of the first image: "
OUTPAINT_PREFIX = ("Extend this image into the grey border areas, continuing the scene "
                   "seamlessly; keep the existing content unchanged: ")
_OPERATIONS = ("generate", "edit", "compose", "inpaint", "outpaint")
# Aspect ratios each provider accepts, for wired width/height on Gemini / Grok.
_ASPECTS = {"gemini": _gemini.ASPECTS, "xai": _xai.ASPECTS}


def _dispatch(provider: str, req: EditRequest, key: str, mask: Image.Image | None = None) -> EditResult:
    # Looked up at call time so tests can patch luna_imaging.providers.<name>.run.
    if provider == "openai":
        return _openai.run(req, key, mask=mask)
    if provider == "gemini":
        return _gemini.run(req, key)
    if provider == "xai":
        return _xai.run(req, key)
    raise ValueError(f"unknown provider: {provider!r}")


def _validate(req: EditRequest, max_inputs: int) -> None:
    op = req.operation
    n_img = len(req.images)
    if op not in _OPERATIONS:
        raise ValueError(f"unknown operation: {op!r}")
    if op == "generate" and n_img:
        raise ValueError("generate takes no images - use edit or compose")
    if op in ("edit", "compose", "inpaint", "outpaint") and not n_img:
        raise ValueError(f"{op} needs an image")
    if op == "inpaint" and req.mask is None:
        raise ValueError("inpaint needs a mask")
    if op == "outpaint":
        margins = tuple(int(v) for v in req.outpaint)
        if len(margins) != 4 or any(v < 0 for v in margins):
            raise ValueError("outpaint margins must be four values >= 0 (left, top, right, bottom)")
        if not any(margins):
            raise ValueError("outpaint needs a margin > 0")
    if n_img > max_inputs:
        raise ValueError(f"{req.model} takes at most {max_inputs} images")


def _whole_image(req: EditRequest, provider: str) -> tuple[EditRequest, list[str]]:
    """generate / edit / compose with wired width x height on Gemini / Grok and aspect_ratio
    auto: send the provider's nearest aspect ratio (the caller resizes the output to width x
    height). OpenAI takes width x height through openai_size instead."""
    w, h = int(req.width or 0), int(req.height or 0)
    if w <= 0 or h <= 0 or provider not in _ASPECTS or req.aspect_ratio != "auto":
        return req, []
    if provider == "xai" and len(req.images) == 1:
        return req, []  # xAI takes a custom aspect only with more than one input image
    ar = nearest_aspect(w, h, _ASPECTS[provider])
    return dataclasses.replace(req, aspect_ratio=ar), [f"width/height {w}x{h} -> aspect_ratio {ar}"]


def region_plan(model: str, operation: str, mask: Image.Image, mask_mode: str = "auto",
                crop_padding: float = 0.25) -> tuple[str, tuple[int, int, int, int]]:
    """The one native / crop / outpaint decision for a region edit (inpaint / outpaint).

    `mask` is the L mask at the size of the image to edit (for outpaint: the grown canvas, mask
    255 on the new area, as `outpaint_canvas` returns it). Returns (mode, box):
      native   - the model takes the whole frame plus the mask; box = the whole frame
      outpaint - no native mask: the whole grown canvas with its grey borders; box = whole frame
      crop     - no native mask: the padded mask box is cropped and sent; box = that box
    `run` and the Director writer's legend both call this, so they cannot drift (R-13).
    Raises ValueError("mask is empty").
    """
    bbox = mask_bbox(mask)
    if bbox is None:
        raise ValueError("mask is empty")
    full = (0, 0, mask.width, mask.height)
    if caps_for(model).native_mask and mask_mode in ("auto", "native"):
        return "native", full
    if operation == "outpaint":
        # The whole canvas, so the model sees all of the original it has to continue.
        return "outpaint", full
    return "crop", expand_box(bbox, crop_padding, mask.size)


def run(req: EditRequest, key: str) -> tuple[EditResult, Image.Image | None]:
    """Run one studio request. Returns (result, the mask actually used, full output size; None
    for generate/edit/compose). The caller's `req` is never mutated."""
    caps = caps_for(req.model)
    provider = provider_for(req.model)
    _validate(req, caps.max_inputs)

    if req.operation in ("generate", "edit", "compose"):
        sent, notes = _whole_image(req, provider)
        result = _dispatch(provider, sent, key)
        if notes:
            result = dataclasses.replace(result, info=list(result.info) + notes)
        return result, None

    notes: list[str] = []
    if req.width or req.height:
        keeps = ("the original size" if req.operation == "inpaint"
                 else "the original plus the outpaint margins")
        notes.append(f"width/height ignored for {req.operation} - the output keeps {keeps}")
    if req.operation == "outpaint":
        base, mask = outpaint_canvas(req.images[0], tuple(int(v) for v in req.outpaint))
        feather = 0  # hard edge: the original area stays exact, no grey fill leaks in
    else:
        base = req.images[0]
        mask = fit_mask(req.mask, base.size)
        feather = req.feather_px
    mode, box = region_plan(req.model, req.operation, mask, req.mask_mode, req.crop_padding)
    refs = list(req.images[1:])

    native = mode == "native"
    if req.mask_mode == "native" and not caps.native_mask:
        notes.append(f"mask_mode native: {req.model} has no native mask - used crop")

    if native:
        sent_img, prompt, sent_mask = base, req.prompt, mask
    elif mode == "outpaint":
        sent_img, prompt, sent_mask = base, OUTPAINT_PREFIX + req.prompt, None
    else:
        sent_img, prompt, sent_mask = base.crop(box), CROP_PREFIX + req.prompt, None

    # Always the size of the image actually sent (OpenAI snaps it in openai_size, keeping the
    # aspect); the other providers follow the sent image's aspect.
    changes = {"images": [sent_img] + refs, "prompt": prompt, "mask": None,
               "width": sent_img.width, "height": sent_img.height, "aspect_ratio": "auto"}
    # OpenAI with "auto" may return the masked area transparent (RGB black), so region edits ask
    # for an opaque patch; the other providers have no background control.
    bg = "opaque" if provider == "openai" else "auto"
    if req.background == "transparent":
        # The patch is composited onto the original, so its alpha could not survive anyway.
        notes.append(f"transparent background is not applied to region edits - sent '{bg}'")
        changes["background"] = bg
    elif provider == "openai":
        changes["background"] = bg
    sent = dataclasses.replace(req, **changes)

    result = _dispatch(provider, sent, key, mask=sent_mask)
    images = [composite(base, patch, box, mask, feather) for patch in result.images]
    info = list(result.info) + notes + [f"mode : {'native' if native else 'crop'}"]
    return dataclasses.replace(result, images=images, info=info), mask
