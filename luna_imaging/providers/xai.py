"""Grok (xAI) image provider. No ComfyUI / torch dependency."""
from __future__ import annotations

import base64
import io
import urllib.request

from PIL import Image

from ..capabilities import caps_for
from ..cost import xai_cost
from ..http import ProviderError, post_json, with_retries
from ..types import EditRequest, EditResult

BASE_URL = "https://api.x.ai/v1"
_RESOLUTIONS = {"1k", "1.5k", "2k"}
_NO_QUALITY_MODELS = ("grok-imagine-image-quality", "grok-imagine-image-pro")


def _png_data_uri(img: Image.Image) -> str:
    img = img if img.mode in ("RGB", "RGBA") else img.convert("RGBA" if "A" in img.getbands() else "RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _download(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - url from xAI response
        return r.read()


def run(req: EditRequest, key: str) -> EditResult:
    caps = caps_for(req.model)
    n_in = len(req.images)
    if n_in > caps.max_inputs:
        raise ValueError(f"{req.model} accepts at most {caps.max_inputs} input images (got {n_in}).")

    info: list[str] = []
    body: dict = {
        "model": req.model,
        "prompt": req.prompt,
        "n": int(req.n),
        "response_format": "b64_json",
    }

    res = (req.resolution or "").lower()
    if res in _RESOLUTIONS:
        body["resolution"] = res
    elif res:
        info.append(f"resolution {req.resolution!r} not supported by Grok (1K/1.5K/2K); omitted")

    aspect = req.aspect_ratio
    if aspect and aspect != "auto":
        if n_in == 1:
            info.append(f"aspect ratio {aspect} not sent: xAI allows a custom aspect only with more than one input image")
        else:
            body["aspect_ratio"] = aspect

    if req.quality in ("low", "medium"):
        if req.model in _NO_QUALITY_MODELS:
            info.append(f"quality not sent: {req.model} has no quality field")
        else:
            body["quality"] = req.quality

    seed = (req.extra or {}).get("seed")
    if seed is not None:
        body["seed"] = int(seed)

    if n_in:
        url = f"{BASE_URL}/images/edits"
        body["images"] = [{"url": _png_data_uri(im)} for im in req.images]
    else:
        url = f"{BASE_URL}/images/generations"

    headers = {"Authorization": f"Bearer {key}"}
    payload = with_retries(lambda: post_json(url, headers, body, timeout=req.timeout), req.max_retries)

    data = payload.get("data") or []
    images: list[Image.Image] = []
    texts: list[str] = []
    for item in data:
        raw = None
        if item.get("b64_json"):
            raw = base64.b64decode(item["b64_json"])
        elif item.get("url"):
            raw = _download(item["url"], req.timeout)
        if raw is None:
            continue
        im = Image.open(io.BytesIO(raw))
        im.load()
        images.append(im.convert("RGBA"))
        if item.get("revised_prompt"):
            texts.append(str(item["revised_prompt"]))
    if not images:
        raise ProviderError(f"No image data in xAI response: {str(payload)[:400]}")

    usage = payload.get("usage") or {}
    return EditResult(images=images, text="\n".join(texts), cost_usd=xai_cost(payload),
                      info=info, usage=dict(usage))
