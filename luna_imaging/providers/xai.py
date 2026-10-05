"""Grok (xAI) image provider. No ComfyUI / torch dependency."""
from __future__ import annotations

import base64
import io
import urllib.error
import urllib.request

from PIL import Image

from ..capabilities import caps_for
from ..cost import xai_cost
from ..http import ProviderError, post_json, with_retries
from ..types import EditRequest, EditResult

BASE_URL = "https://api.x.ai/v1"
RESOLUTIONS = ["1K", "1.5K", "2K"]   # the resolution values the image API takes, smallest first
_RESOLUTIONS = {r.lower() for r in RESOLUTIONS}
_NO_QUALITY_MODELS = ("grok-imagine-image-quality", "grok-imagine-image-pro")
# Copied from grok_imagine_nodes._IMAGE_AR: the aspect_ratio values the image API takes.
ASPECTS = ["1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9", "9:19.5", "19.5:9", "9:20", "20:9",
           "1:2", "2:1", "21:9", "5:2"]
_UA = "Mozilla/5.0 (ComfyUI LLM_Prompt Grok node)"


def qualities_for(model: str) -> list[str]:
    """The quality values `model` takes, cheapest first ("auto" aside); none on quality / pro."""
    return [] if model in _NO_QUALITY_MODELS else ["low", "medium"]


def _png_data_uri(img: Image.Image) -> str:
    img = img if img.mode in ("RGB", "RGBA") else img.convert("RGBA" if "A" in img.getbands() else "RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _download(url: str, key: str, timeout: float) -> bytes:
    """Fetch an image URL from the response. As grok_imagine_nodes._download_bytes: xAI's CDN
    403s urllib's default User-Agent, so send a browser-like one; on 401/403 retry once with
    the Bearer key. Failures raise ProviderError (the generation itself was already billed)."""
    def attempt(with_auth: bool) -> bytes:
        r = urllib.request.Request(url, method="GET")
        r.add_header("User-Agent", _UA)
        r.add_header("Accept", "*/*")
        if with_auth and key:
            r.add_header("Authorization", f"Bearer {key}")
        with urllib.request.urlopen(r, timeout=timeout) as resp:  # noqa: S310 - url from xAI
            return resp.read()

    try:
        try:
            return attempt(False)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and key:
                return attempt(True)
            raise
    except urllib.error.HTTPError as e:
        raise ProviderError(f"xAI image download failed: HTTP {e.code} (the generation was "
                            f"already billed)", status=e.code, billed=True) from None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise ProviderError(f"xAI image download failed: {reason} (the generation was "
                            f"already billed)", billed=True) from None


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
    elif res and res != "auto":  # auto: omitted silently, it is the API default
        info.append(f"resolution {req.resolution!r} not supported by Grok (1K/1.5K/2K); omitted")

    aspect = req.aspect_ratio
    if aspect and aspect != "auto":
        if n_in == 1:
            info.append(f"aspect ratio {aspect} not sent: xAI allows a custom aspect only with more than one input image")
        else:
            body["aspect_ratio"] = aspect

    quality = req.quality or "auto"
    if quality in ("low", "medium") and req.model not in _NO_QUALITY_MODELS:
        body["quality"] = quality
    elif quality != "auto":
        why = (f"{req.model} has no quality field" if req.model in _NO_QUALITY_MODELS
               else "Grok takes low / medium")
        info.append(f"quality {quality!r} not sent: {why}")

    # No seed: it is never sent (Ruling R8) - the node's seed only re-runs it.

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
            raw = _download(item["url"], key, req.timeout)
        if raw is None:
            continue
        try:
            im = Image.open(io.BytesIO(raw))
            im.load()
        except (OSError, ValueError, Image.DecompressionBombError) as e:
            raise ProviderError(f"xAI returned an image Pillow cannot read: {e}") from None
        images.append(im.convert("RGBA"))
        if item.get("revised_prompt"):
            texts.append(str(item["revised_prompt"]))
    if not images:
        raise ProviderError(f"No image data in xAI response: {str(payload)[:400]}")

    usage = payload.get("usage") or {}
    return EditResult(images=images, text="\n".join(texts), cost_usd=xai_cost(payload),
                      info=info, usage=dict(usage))
