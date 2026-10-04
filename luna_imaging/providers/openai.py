"""OpenAI Images provider: /images/generations (JSON) and /images/edits (multipart).

Request rules are ported from openai_image_node.execute (live-verified 2026-10-04).
Only the provider request lives here; region logic belongs to the orchestrator.
"""
from __future__ import annotations

import base64
import io

from PIL import Image

from ..http import ProviderError, post_json, post_multipart, with_retries
from ..masks import openai_alpha_mask
from ..cost import openai_cost
from ..sizes import openai_size
from ..types import EditRequest, EditResult

_BASE = "https://api.openai.com/v1"
_FIDELITY_MODELS = ("gpt-image-1", "gpt-image-1.5", "chatgpt-image-latest")


def _png_bytes(img: Image.Image) -> bytes:
    has_alpha = img.mode in ("RGBA", "LA") or "transparency" in img.info
    buf = io.BytesIO()
    img.convert("RGBA" if has_alpha else "RGB").save(buf, format="PNG")
    return buf.getvalue()


def run(req: EditRequest, key: str, mask: Image.Image | None = None) -> EditResult:
    model = req.model
    notes: list[str] = []
    size = openai_size(model, req.aspect_ratio, req.resolution, req.width, req.height, notes)

    quality = req.quality
    if quality in ("xhigh", "max") and not model.startswith("gpt-image-2.5"):
        notes.append(f"{model} has no quality '{quality}' - sent 'high'")
        quality = "high"

    background = req.background
    output_format = req.extra.get("output_format", "png")
    if background == "transparent":
        if model == "gpt-image-2":
            notes.append("gpt-image-2 does not support transparent - sent 'auto'")
            background = "auto"
        elif output_format == "jpeg":
            notes.append("transparent needs png/webp - output_format switched to png")
            output_format = "png"

    params = {
        "model": model, "prompt": req.prompt, "n": int(req.n), "size": size,
        "quality": quality, "background": background,
        "output_format": output_format,
        "moderation": req.extra.get("moderation", "low"),
    }
    if output_format in ("jpeg", "webp"):
        params["output_compression"] = int(req.extra.get("output_compression", 100))

    headers = {"Authorization": f"Bearer {key}"}
    images = req.images
    if images:
        fidelity = req.extra.get("input_fidelity", "auto")
        if fidelity != "auto":
            if model in _FIDELITY_MODELS:
                params["input_fidelity"] = fidelity
            else:
                notes.append(f"input_fidelity not supported by {model} - not sent")
        files = [("image[]", f"ref_{i:02d}.png", _png_bytes(im)) for i, im in enumerate(images)]
        if mask is not None:
            files.append(("mask", "mask.png", openai_alpha_mask(mask, images[0].size)))
        fields = {k: str(v) for k, v in params.items()}
        resp = with_retries(
            lambda: post_multipart(f"{_BASE}/images/edits", headers, fields, files, req.timeout),
            req.max_retries)
    else:
        if mask is not None:
            notes.append("mask ignored - it needs a reference image to apply to")
        resp = with_retries(
            lambda: post_json(f"{_BASE}/images/generations", headers, params, req.timeout),
            req.max_retries)

    out = []
    for d in (resp.get("data") or []):
        if d.get("b64_json"):
            out.append(Image.open(io.BytesIO(base64.b64decode(d["b64_json"]))).convert("RGBA"))
    if not out:
        raise ProviderError("No image in the OpenAI response")
    usage = resp.get("usage") or {}
    return EditResult(images=out, cost_usd=openai_cost(model, usage),
                      info=notes + [f"size : {size}"], usage=usage)
