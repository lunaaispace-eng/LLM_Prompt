"""Gemini image provider over REST (no SDK). Does ONLY the provider request."""
from __future__ import annotations

import base64
import io

from PIL import Image

from ..capabilities import caps_for
from ..cost import gemini_cost
from ..http import ProviderError, post_json, with_retries
from ..types import EditRequest, EditResult

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Copied from gemini_image_node.CAPS (accepted duplication): imageSize values per
# model family, longest-prefix match. Uppercase K is mandatory.
_SIZES = {
    "gemini-3-pro-image": ["1K", "2K", "4K"],
    "nano-banana-pro": ["1K", "2K", "4K"],
    "gemini-3.1-flash-lite-image": ["1K"],
    "gemini-3.1-flash-image": ["0.5K", "1K", "2K", "4K"],
    "gemini-2.5-flash-image": ["1K"],
}
_DEFAULT_SIZES = ["1K", "2K", "4K"]
_DEFAULT_PRICE_SIZE = "1K"

# Copied from gemini_image_node.ASPECT_RATIOS (without "auto"): the aspectRatio values the API takes.
ASPECTS = ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]

_SAFETY = [{"category": c, "threshold": "BLOCK_NONE"} for c in (
    "HARM_CATEGORY_HARASSMENT",
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
)]


def _sizes_for(model: str) -> list[str]:
    best, n = _DEFAULT_SIZES, -1
    for prefix, sizes in _SIZES.items():
        if model.startswith(prefix) and len(prefix) > n:
            best, n = sizes, len(prefix)
    return best


def _k(size: str) -> float:
    try:
        return float(size.upper().rstrip("K"))
    except ValueError:
        return 0.0


def _clamp_size(size: str, sizes: list[str]) -> str:
    """The largest supported size <= the request, else the smallest supported one."""
    below = [s for s in sizes if _k(s) <= _k(size)]
    return max(below, key=_k) if below else min(sizes, key=_k)


def _png_b64(img: Image.Image) -> str:
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _split(payload: dict) -> tuple[list[Image.Image], list[str]]:
    images, texts = [], []
    cands = payload.get("candidates") or []
    content = (cands[0].get("content") if cands else None) or {}
    for part in content.get("parts") or []:
        if part.get("thought"):
            continue
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            raw = base64.b64decode(inline["data"])
            images.append(Image.open(io.BytesIO(raw)).convert("RGBA"))
        elif part.get("text"):
            texts.append(part["text"])
    return images, texts


def _reason(payload: dict, texts: list[str]) -> str:
    bits = []
    block = (payload.get("promptFeedback") or {}).get("blockReason")
    if block:
        bits.append(f"blockReason={block}")
    for cand in payload.get("candidates") or []:
        if cand.get("finishReason"):
            bits.append(f"finishReason={cand['finishReason']}")
            break
    msg = "No image returned (" + (", ".join(bits) or "no reason given") + ")"
    if texts:
        msg += ". " + " ".join(texts)[:300]
    return msg


def run(req: EditRequest, key: str) -> EditResult:
    caps = caps_for(req.model)
    if len(req.images) > caps.max_inputs:
        raise ValueError(
            f"{req.model} takes at most {caps.max_inputs} reference images, "
            f"got {len(req.images)}")

    info: list[str] = []
    sizes = _sizes_for(req.model)
    image_config: dict = {}
    if req.aspect_ratio and req.aspect_ratio != "auto":
        image_config["aspectRatio"] = req.aspect_ratio
    size = None
    if req.resolution and req.resolution != "auto":
        size = req.resolution
        if size not in sizes:
            fallback = _clamp_size(size, sizes)
            info.append(f"{req.model} does not accept {size} - clamped to {fallback}")
            size = fallback
        image_config["imageSize"] = size

    gen_config: dict = {"responseModalities": ["TEXT", "IMAGE"]}
    if image_config:
        gen_config["imageConfig"] = image_config

    parts: list[dict] = [
        {"inline_data": {"mime_type": "image/png", "data": _png_b64(im)}}
        for im in req.images
    ]
    if req.prompt and req.prompt.strip():
        parts.append({"text": req.prompt.strip()})
    if not parts:
        raise ValueError("nothing to send: empty prompt and no images")

    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": gen_config,
        "safetySettings": _SAFETY,
    }
    url = URL.format(model=req.model)
    headers = {"x-goog-api-key": key}

    result = EditResult(info=info)
    texts_all: list[str] = []
    n = max(1, int(req.n))
    for _ in range(n):
        # One image per call. When a later call fails, the images already returned were
        # billed: keep them and say why the batch stopped. Raise only when nothing came back.
        try:
            payload = with_retries(
                lambda: post_json(url, headers, body, req.timeout), req.max_retries)
        except ProviderError as e:
            if not result.images:
                raise
            info.append(f"stopped after {len(result.images)} of {n} image(s): {e}")
            break
        images, texts = _split(payload)
        if not images:
            if not result.images:
                raise ProviderError(_reason(payload, texts))
            info.append(f"stopped after {len(result.images)} of {n} image(s): "
                        f"{_reason(payload, texts)}")
            break
        result.images.extend(images)
        texts_all.extend(t.strip() for t in texts if t.strip())
        usage = payload.get("usageMetadata")
        if usage:
            result.usage = usage

    result.text = "\n".join(texts_all)
    price_size = size or _DEFAULT_PRICE_SIZE
    if size is None:
        info.append(f"size not set - priced at {price_size}")
    result.cost_usd = gemini_cost(req.model, price_size, len(result.images))
    return result
