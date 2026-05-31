"""Grok Imagine nodes (BYO API key) — image & video generation via xAI directly.

These mirror ComfyUI's partner "Grok" nodes but call the xAI API with YOUR OWN
key (https://api.x.ai/v1) instead of routing through ComfyUI's credit-billed
proxy. No credits, no comfy_api auth — just your XAI_API_KEY from env or a .env
file at the ComfyUI root (same secure pattern as the LLM Prompt (API) node;
the key is never stored in the workflow).

Node IDs are suffixed "(API Key)" so they coexist with the stock partner nodes.

Endpoints (all POST unless noted), base https://api.x.ai/v1, Bearer auth:
  /images/generations         text -> image(s)
  /images/edits               image(s) + prompt -> image(s)
  /videos/generations         text/image -> {request_id}
  /videos/edits               video + prompt -> {request_id}
  /videos/extensions          video + prompt -> {request_id}
  /videos/{request_id}  (GET) poll -> {status, video:{url,...}}
"""

from __future__ import annotations

import base64
import io
import json
import os
import time
import urllib.request
import urllib.error
from pathlib import Path

import numpy as np
import torch
from PIL import Image

XAI_BASE_URL = "https://api.x.ai/v1"
_KEY_NAMES = ("XAI_API_KEY", "GROK_API_KEY")

_IMAGE_MODELS = ["grok-imagine-image-quality", "grok-imagine-image-pro", "grok-imagine-image"]
_IMAGE_AR = ["1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9", "9:19.5", "19.5:9", "9:20", "20:9", "1:2", "2:1"]
_VIDEO_AR = ["auto", "16:9", "4:3", "3:2", "1:1", "2:3", "3:4", "9:16"]


# ---------------------------------------------------------------------------
# Key resolution (env -> .env at ComfyUI root -> .env beside this node)
# ---------------------------------------------------------------------------

def _comfyui_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _load_env_file_keys() -> dict[str, str]:
    keys: dict[str, str] = {}
    for path in (_comfyui_root() / ".env", Path(__file__).resolve().parent / ".env"):
        try:
            if not path.is_file():
                continue
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v and k not in keys:
                    keys[k] = v
        except Exception:
            continue
    return keys


def _resolve_xai_key() -> str:
    for name in _KEY_NAMES:
        v = os.environ.get(name)
        if v and v.strip():
            return v.strip()
    file_keys = _load_env_file_keys()
    for name in _KEY_NAMES:
        if file_keys.get(name):
            return file_keys[name].strip()
    raise RuntimeError(
        "No xAI API key found. Set XAI_API_KEY (or GROK_API_KEY) as an environment "
        f"variable before launching ComfyUI, or add it to a .env file at {_comfyui_root() / '.env'}:\n"
        "    XAI_API_KEY=xai-...\n"
        "The key is read only from env/.env — never stored in the workflow."
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _request(method: str, path: str, key: str, payload: dict | None, timeout: float) -> dict:
    url = XAI_BASE_URL + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"xAI HTTP {e.code} on {method} {path}: {body[:600] or e.reason}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach {url}: {e.reason}") from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Non-JSON response from {url}: {raw[:600]}") from None


def _post(path: str, key: str, payload: dict, timeout: float = 120.0) -> dict:
    return _request("POST", path, key, payload, timeout)


def _get(path: str, key: str, timeout: float = 60.0) -> dict:
    return _request("GET", path, key, None, timeout)


def _download_bytes(url: str, timeout: float = 300.0) -> bytes:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---------------------------------------------------------------------------
# Tensor / image helpers
# ---------------------------------------------------------------------------

def _tensor_to_data_uri(tensor: torch.Tensor) -> str:
    """[H,W,C] (or [1,H,W,C]) float 0-1 tensor -> data:image/png;base64 URI."""
    t = tensor
    if t.ndim == 4:
        t = t[0]
    arr = (t.clamp(0, 1) * 255.0).to(torch.uint8).cpu().numpy()
    buf = io.BytesIO()
    Image.fromarray(arr, mode="RGB").save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{b64}"


def _iter_images(image: torch.Tensor):
    """Yield individual [H,W,C] frames from an IMAGE batch tensor."""
    if image is None:
        return
    if image.ndim == 4:
        for i in range(image.shape[0]):
            yield image[i]
    else:
        yield image


def _bytes_to_image_tensor(data: bytes) -> torch.Tensor:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    arr = np.asarray(img, dtype=np.float32) / 255.0
    return torch.from_numpy(arr)[None, ...]  # [1,H,W,C]


def _images_from_response(payload: dict, key: str) -> torch.Tensor:
    data = payload.get("data") or []
    if not data:
        raise RuntimeError(f"No image data in xAI response: {json.dumps(payload)[:400]}")
    frames = []
    for item in data:
        if item.get("b64_json"):
            frames.append(_bytes_to_image_tensor(base64.b64decode(item["b64_json"])))
        elif item.get("url"):
            frames.append(_bytes_to_image_tensor(_download_bytes(item["url"])))
    if not frames:
        raise RuntimeError("xAI response contained no usable image url/b64.")
    return torch.cat(frames, dim=0)


def _video_to_data_uri(video) -> str:
    """Read a ComfyUI VIDEO input into a data:video/mp4;base64 URI."""
    src = video.get_stream_source()
    if hasattr(src, "read"):
        raw = src.read()
    else:
        raw = Path(str(src)).read_bytes()
    if len(raw) > 50 * 1024 * 1024:
        raise ValueError(f"Video size ({len(raw)/1024/1024:.1f}MB) exceeds xAI's 50MB limit.")
    b64 = base64.b64encode(raw).decode("utf-8")
    return f"data:video/mp4;base64,{b64}"


def _poll_video(request_id: str, key: str, poll_timeout: float, max_wait: float = 1800.0):
    """Poll GET /videos/{id} until a video URL is returned. Returns the video url."""
    terminal_fail = {"failed", "error", "canceled", "cancelled"}
    start = time.time()
    while True:
        status = _get(f"/videos/{request_id}", key, timeout=poll_timeout)
        video = status.get("video") or {}
        st = (status.get("status") or "").lower()
        if video.get("url"):
            return video["url"]
        if st in terminal_fail:
            raise RuntimeError(f"xAI video job {request_id} failed (status={st}).")
        if time.time() - start > max_wait:
            raise RuntimeError(f"xAI video job {request_id} timed out after {max_wait:.0f}s (last status={st or 'unknown'}).")
        time.sleep(5.0)


def _video_output(url: str):
    from comfy_api.latest import InputImpl
    return InputImpl.VideoFromFile(io.BytesIO(_download_bytes(url)))


# ---------------------------------------------------------------------------
# Nodes (classic V1 API — no partner/credit machinery)
# ---------------------------------------------------------------------------

_SEED = ("INT", {"default": 0, "min": 0, "max": 2147483647, "control_after_generate": True,
                 "tooltip": "Re-run trigger; results are nondeterministic regardless of seed."})
_PROMPT = ("STRING", {"multiline": True, "default": "", "tooltip": "Text prompt."})


class GrokImageAPINode:
    CATEGORY = "Luna/Grok"
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "generate"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model": (_IMAGE_MODELS,),
            "prompt": _PROMPT,
            "aspect_ratio": (_IMAGE_AR,),
            "number_of_images": ("INT", {"default": 1, "min": 1, "max": 10}),
            "resolution": (["1K", "2K"],),
            "seed": _SEED,
            "timeout_seconds": ("INT", {"default": 120, "min": 10, "max": 600}),
        }}

    def generate(self, model, prompt, aspect_ratio, number_of_images, resolution, seed, timeout_seconds):
        if not prompt.strip():
            raise ValueError("Prompt is required.")
        key = _resolve_xai_key()
        resp = _post("/images/generations", key, {
            "model": model, "prompt": prompt, "aspect_ratio": aspect_ratio,
            "n": int(number_of_images), "seed": int(seed),
            "response_format": "url", "resolution": resolution.lower(),
        }, timeout=float(timeout_seconds))
        return (_images_from_response(resp, key),)


class GrokImageEditAPINode:
    CATEGORY = "Luna/Grok"
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "generate"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model": (_IMAGE_MODELS,),
            "image": ("IMAGE",),
            "prompt": _PROMPT,
            "resolution": (["1K", "2K"],),
            "number_of_images": ("INT", {"default": 1, "min": 1, "max": 10}),
            "aspect_ratio": (["auto"] + _IMAGE_AR,),
            "seed": _SEED,
            "timeout_seconds": ("INT", {"default": 120, "min": 10, "max": 600}),
        }}

    def generate(self, model, image, prompt, resolution, number_of_images, aspect_ratio, seed, timeout_seconds):
        if not prompt.strip():
            raise ValueError("Prompt is required.")
        frames = list(_iter_images(image))
        n_in = len(frames)
        if model == "grok-imagine-image-pro" and n_in > 1:
            raise ValueError("The pro model supports only 1 input image.")
        if model != "grok-imagine-image-pro" and n_in > 3:
            raise ValueError("A maximum of 3 input images is supported.")
        if aspect_ratio != "auto" and n_in == 1:
            raise ValueError("Custom aspect ratio is only allowed when multiple images are connected.")
        key = _resolve_xai_key()
        resp = _post("/images/edits", key, {
            "model": model,
            "images": [{"url": _tensor_to_data_uri(f)} for f in frames],
            "prompt": prompt, "resolution": resolution.lower(),
            "n": int(number_of_images), "seed": int(seed),
            "response_format": "url",
            "aspect_ratio": None if aspect_ratio == "auto" else aspect_ratio,
        }, timeout=float(timeout_seconds))
        return (_images_from_response(resp, key),)


class GrokVideoAPINode:
    CATEGORY = "Luna/Grok"
    RETURN_TYPES = ("VIDEO",)
    FUNCTION = "generate"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model": (["grok-imagine-video"],),
            "prompt": _PROMPT,
            "resolution": (["480p", "720p"],),
            "aspect_ratio": (_VIDEO_AR,),
            "duration": ("INT", {"default": 6, "min": 1, "max": 15}),
            "seed": _SEED,
            "timeout_seconds": ("INT", {"default": 120, "min": 10, "max": 600}),
        }, "optional": {"image": ("IMAGE",)}}

    def generate(self, model, prompt, resolution, aspect_ratio, duration, seed, timeout_seconds, image=None):
        if not prompt.strip():
            raise ValueError("Prompt is required.")
        key = _resolve_xai_key()
        body = {
            "model": model, "prompt": prompt, "resolution": resolution,
            "duration": int(duration), "seed": int(seed),
            "aspect_ratio": None if aspect_ratio == "auto" else aspect_ratio,
        }
        if image is not None:
            frames = list(_iter_images(image))
            if len(frames) != 1:
                raise ValueError("Only one input image is supported.")
            body["image"] = {"url": _tensor_to_data_uri(frames[0])}
        init = _post("/videos/generations", key, body, timeout=float(timeout_seconds))
        url = _poll_video(init["request_id"], key, poll_timeout=float(timeout_seconds))
        return (_video_output(url),)


class GrokVideoReferenceAPINode:
    CATEGORY = "Luna/Grok"
    RETURN_TYPES = ("VIDEO",)
    FUNCTION = "generate"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model": (["grok-imagine-video"],),
            "prompt": _PROMPT,
            "reference_images": ("IMAGE",),
            "resolution": (["480p", "720p"],),
            "aspect_ratio": (_VIDEO_AR[1:],),  # no "auto"
            "duration": ("INT", {"default": 6, "min": 2, "max": 15}),
            "seed": _SEED,
            "timeout_seconds": ("INT", {"default": 120, "min": 10, "max": 600}),
        }}

    def generate(self, model, prompt, reference_images, resolution, aspect_ratio, duration, seed, timeout_seconds):
        if not prompt.strip():
            raise ValueError("Prompt is required.")
        frames = list(_iter_images(reference_images))[:7]
        if not frames:
            raise ValueError("At least one reference image is required.")
        key = _resolve_xai_key()
        init = _post("/videos/generations", key, {
            "model": model, "prompt": prompt,
            "reference_images": [{"url": _tensor_to_data_uri(f)} for f in frames],
            "resolution": resolution, "duration": int(duration),
            "aspect_ratio": aspect_ratio, "seed": int(seed),
        }, timeout=float(timeout_seconds))
        url = _poll_video(init["request_id"], key, poll_timeout=float(timeout_seconds))
        return (_video_output(url),)


class GrokVideoEditAPINode:
    CATEGORY = "Luna/Grok"
    RETURN_TYPES = ("VIDEO",)
    FUNCTION = "generate"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model": (["grok-imagine-video"],),
            "prompt": _PROMPT,
            "video": ("VIDEO",),
            "seed": _SEED,
            "timeout_seconds": ("INT", {"default": 120, "min": 10, "max": 600}),
        }}

    def generate(self, model, prompt, video, seed, timeout_seconds):
        if not prompt.strip():
            raise ValueError("Prompt is required.")
        key = _resolve_xai_key()
        init = _post("/videos/edits", key, {
            "model": model, "prompt": prompt,
            "video": {"url": _video_to_data_uri(video)}, "seed": int(seed),
        }, timeout=float(timeout_seconds))
        url = _poll_video(init["request_id"], key, poll_timeout=float(timeout_seconds))
        return (_video_output(url),)


class GrokVideoExtendAPINode:
    CATEGORY = "Luna/Grok"
    RETURN_TYPES = ("VIDEO",)
    FUNCTION = "generate"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model": (["grok-imagine-video"],),
            "prompt": _PROMPT,
            "video": ("VIDEO",),
            "duration": ("INT", {"default": 8, "min": 2, "max": 15}),
            "seed": _SEED,
            "timeout_seconds": ("INT", {"default": 120, "min": 10, "max": 600}),
        }}

    def generate(self, model, prompt, video, duration, seed, timeout_seconds):
        if not prompt.strip():
            raise ValueError("Prompt is required.")
        key = _resolve_xai_key()
        init = _post("/videos/extensions", key, {
            "model": model, "prompt": prompt,
            "video": {"url": _video_to_data_uri(video)}, "duration": int(duration),
        }, timeout=float(timeout_seconds))
        url = _poll_video(init["request_id"], key, poll_timeout=float(timeout_seconds))
        return (_video_output(url),)


NODE_CLASS_MAPPINGS = {
    "GrokImageAPINode": GrokImageAPINode,
    "GrokImageEditAPINode": GrokImageEditAPINode,
    "GrokVideoAPINode": GrokVideoAPINode,
    "GrokVideoReferenceAPINode": GrokVideoReferenceAPINode,
    "GrokVideoEditAPINode": GrokVideoEditAPINode,
    "GrokVideoExtendAPINode": GrokVideoExtendAPINode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "GrokImageAPINode": "Grok Image (API Key)",
    "GrokImageEditAPINode": "Grok Image Edit (API Key)",
    "GrokVideoAPINode": "Grok Video (API Key)",
    "GrokVideoReferenceAPINode": "Grok Reference-to-Video (API Key)",
    "GrokVideoEditAPINode": "Grok Video Edit (API Key)",
    "GrokVideoExtendAPINode": "Grok Video Extend (API Key)",
}
