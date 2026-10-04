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
from .types import EditRequest, EditResult

CROP_PREFIX = "Edit only the marked region of the first image: "
_OPERATIONS = ("generate", "edit", "compose", "inpaint", "outpaint")


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


def run(req: EditRequest, key: str) -> tuple[EditResult, Image.Image | None]:
    """Run one studio request. Returns (result, the mask actually used, full output size; None
    for generate/edit/compose). The caller's `req` is never mutated."""
    caps = caps_for(req.model)
    provider = provider_for(req.model)
    _validate(req, caps.max_inputs)

    if req.operation in ("generate", "edit", "compose"):
        return _dispatch(provider, req, key), None

    notes: list[str] = []
    if req.operation == "outpaint":
        base, mask = outpaint_canvas(req.images[0], tuple(int(v) for v in req.outpaint))
        feather = 0  # hard edge: the original area stays exact, no grey fill leaks in
    else:
        base = req.images[0]
        mask = fit_mask(req.mask, base.size)
        feather = req.feather_px
    bbox = mask_bbox(mask)
    if bbox is None:
        raise ValueError("mask is empty")
    refs = list(req.images[1:])

    native = caps.native_mask and req.mask_mode in ("auto", "native")
    if req.mask_mode == "native" and not caps.native_mask:
        notes.append(f"mask_mode native: {req.model} has no native mask - used crop")

    if native:
        box = (0, 0, base.width, base.height)
        sent_img, prompt, sent_mask = base, req.prompt, mask
    else:
        box = expand_box(bbox, req.crop_padding, base.size)
        sent_img, prompt, sent_mask = base.crop(box), CROP_PREFIX + req.prompt, None

    changes = {"images": [sent_img] + refs, "prompt": prompt, "mask": None}
    if provider == "openai":
        if not req.width and not req.height:
            changes["width"], changes["height"] = sent_img.size  # snapped by openai_size, keeps the aspect
    elif not native:
        changes["aspect_ratio"] = "auto"  # follow the crop
    sent = dataclasses.replace(req, **changes)

    result = _dispatch(provider, sent, key, mask=sent_mask)
    images = [composite(base, patch, box, mask, feather) for patch in result.images]
    info = list(result.info) + notes + [f"mode : {'native' if native else 'crop'}"]
    return dataclasses.replace(result, images=images, info=info), mask
