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

# grok-imagine-image-pro is now an ALIAS of grok-imagine-image-quality on xAI's
# /v1/image-generation-models (checked 2026-10-04). Kept so saved workflows
# still validate. Edit input caps: image / 2.0 = 5, quality / pro = 3 (live).
_IMAGE_MODELS = ["grok-imagine-image-2.0", "grok-imagine-image-quality", "grok-imagine-image-pro", "grok-imagine-image"]
# 21:9 and 5:2 appended 2026-10-04 from xAI's image docs (docs-only, not probed).
_IMAGE_AR = ["1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9", "9:19.5", "19.5:9", "9:20", "20:9", "1:2", "2:1",
             "21:9", "5:2"]
_VIDEO_AR = ["auto", "16:9", "4:3", "3:2", "1:1", "2:3", "3:4", "9:16"]

# grok-imagine-video-1.5 is the current generation; the unversioned model is the
# older, cheaper one. 1.5 is the ONLY model that does reference-to-video, and the
# only one that reaches 1080p (text-to-video + image-to-video; reference-to-video
# is capped at 720p by xAI).
# grok-imagine-video-1.5-lite (2026-10) is the cheaper 1.5 tier ($0.02/s vs
# $0.08/s). LIVE-verified 2026-10-04: text- and image-to-video, 480p/720p/1080p,
# 1-15 s. It REJECTS reference_images ("not supported for this model"),
# last_frame/keyframes, edit and extend.
#
# Edit and extend are the reverse: xAI rejects BOTH 1.5 models there and only
# the classic grok-imagine-video accepts them (live, same day) — hence the
# separate, single-entry list for those two nodes.
_VIDEO_MODELS = ["grok-imagine-video-1.5", "grok-imagine-video-1.5-lite", "grok-imagine-video"]
_VIDEO_EDIT_MODELS = ["grok-imagine-video"]
_VIDEO_RES = ["480p", "720p", "1080p"]

# Full docs crawl + live probes, 2026-10-04 (scratch FINDINGS, ~$1.35):
#  - reference_images: up to 14 on 1.5 (8 accepted, 40 -> "Maximum allowed is 14").
#  - reference_audios: <= 3 {voice_id}, 1.5 only; tag them <AUDIO_n> in the prompt.
#    Caller-supplied audio URLs are partner-only (403). Custom voice ids work too.
#  - last_frame and keyframes (<= 4 {image, timestamp_s}, strictly inside the clip,
#    1/3 s grid): 1.5 only; any reference / last_frame / keyframes request at 1080p
#    is a 400. An `image` sent WITH any of those is the pinned first frame <IMAGE_0>.
#  - generate_audio=false strips the audio track (ffprobe-checked, 1.5 and lite).
#  - video edit/extend: classic model only; extend duration 2-10 (default 6).
#  - poll statuses: pending | done | failed | expired; done can carry an empty url
#    when moderation withheld it (video.respect_moderation=false).
#  - aspect_ratio "auto" is a 422 on video — send null / omit.
#  - seed is not an API field (silently ignored); it stays as a re-run trigger.
#  - cost: usage.cost_in_usd_ticks on image responses and video polls, 1e10 = $1.
_MAX_REFS = 14
_VOICES = ["ara", "eve", "leo", "rex", "sal", "carina", "zagan", "helix", "orion", "luna",
           "iris", "altair", "zenith", "perseus", "helios", "lux", "kepler", "rigel", "cosmo",
           "celeste", "ursa", "sirius", "lumen", "castor", "naksh", "atlas"]
_VOICE_OPTS = ["none"] + _VOICES


def _check_video_resolution(model: str, resolution: str, mode: str = "") -> str:
    """Reject resolutions the chosen video model can't do, with a clear message."""
    if resolution == "1080p":
        if not model.startswith("grok-imagine-video-1.5"):
            raise ValueError("1080p requires grok-imagine-video-1.5 or -1.5-lite.")
        if mode in ("reference", "frames"):
            raise ValueError("Reference / last_frame / keyframes requests are capped at "
                             "720p by xAI (1080p is text- and image-to-video only).")
    return resolution


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


def _download_bytes(url: str, key: str | None = None, timeout: float = 300.0) -> bytes:
    """Download an asset URL. xAI's CDN 403s the default urllib User-Agent, so
    send a browser-like UA; if still forbidden, retry with the Bearer key."""
    def _attempt(with_auth: bool) -> bytes:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "Mozilla/5.0 (ComfyUI LLM_Prompt Grok node)")
        req.add_header("Accept", "*/*")
        if with_auth and key:
            req.add_header("Authorization", f"Bearer {key}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    try:
        return _attempt(False)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) and key:
            return _attempt(True)
        raise


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
            frames.append(_bytes_to_image_tensor(_download_bytes(item["url"], key)))
    if not frames:
        raise RuntimeError("xAI response contained no usable image url/b64.")
    print(f"[Grok Imagine] {len(frames)} image(s), cost {_cost_str(payload)}")
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


def _cost_str(payload: dict) -> str:
    ticks = ((payload or {}).get("usage") or {}).get("cost_in_usd_ticks")
    return f"${ticks / 1e10:.4f}" if isinstance(ticks, (int, float)) else "n/a"


def _poll_video(request_id: str, key: str, poll_timeout: float, max_wait: float = 1800.0):
    """Poll GET /videos/{id} until done. Returns the video url.

    Terminal states per xAI: done | failed | expired. A `done` with no url means
    moderation withheld the clip — that is terminal too, not a reason to keep
    polling (the old loop spun for 30 min on both `expired` and that case).
    """
    start = time.time()
    while True:
        status = _get(f"/videos/{request_id}", key, timeout=poll_timeout)
        video = status.get("video") or {}
        st = (status.get("status") or "").lower()
        err = status.get("error") or {}
        if video.get("url"):
            print(f"[Grok Imagine] video {request_id[:12]} done, "
                  f"{video.get('duration', '?')} s, cost {_cost_str(status)}")
            return video["url"]
        if st == "done":
            raise RuntimeError(f"xAI video job {request_id} finished without a video "
                               "(withheld by moderation"
                               + (f": {err.get('message')}" if err.get("message") else "") + ").")
        if st in ("failed", "error", "expired", "canceled", "cancelled"):
            raise RuntimeError(f"xAI video job {request_id} {st}"
                               + (f" [{err.get('code')}]: {err.get('message')}" if err else "."))
        if time.time() - start > max_wait:
            raise RuntimeError(f"xAI video job {request_id} timed out after {max_wait:.0f}s (last status={st or 'unknown'}).")
        time.sleep(5.0)


def _video_output(url: str, key: str | None = None):
    from comfy_api.latest import InputImpl
    return InputImpl.VideoFromFile(io.BytesIO(_download_bytes(url, key)))


# ---------------------------------------------------------------------------
# Nodes (classic V1 API — no partner/credit machinery)
# ---------------------------------------------------------------------------

_SEED = ("INT", {"default": 0, "min": 0, "max": 2147483647, "control_after_generate": True,
                 "tooltip": "Re-run trigger; results are nondeterministic regardless of seed."})
_PROMPT = ("STRING", {"multiline": True, "default": "", "tooltip": "Text prompt."})

# Optional connectable size inputs (e.g. from Resolution-Master). When both are
# wired (>0) they pick the nearest supported aspect_ratio from the dims; image
# nodes additionally resize the result to exactly width x height.
_WIDTH_IN = ("INT", {"default": 0, "min": 0, "max": 32768, "forceInput": True,
                     "tooltip": "Optional. Width from a size node (e.g. Resolution-Master). "
                                "If width & height are both wired, they pick the nearest "
                                "aspect_ratio (and resize image output to exactly this size)."})
_HEIGHT_IN = ("INT", {"default": 0, "min": 0, "max": 32768, "forceInput": True,
                      "tooltip": "Optional. Height from a size node. See width."})


def _ar_value(ar: str) -> float:
    a, b = ar.split(":")
    return float(a) / float(b)


def _nearest_ar(width: int, height: int, choices) -> str:
    """Pick the aspect_ratio string from `choices` closest to width:height."""
    r = width / height
    numeric = [c for c in choices if ":" in c]
    return min(numeric, key=lambda c: abs(r - _ar_value(c)))


def _resize_batch_exact(image: torch.Tensor, width: int, height: int) -> torch.Tensor:
    """Resize an IMAGE batch [B,H,W,C] (float 0-1) to exactly width x height."""
    import torch.nn.functional as F
    x = image.permute(0, 3, 1, 2)  # [B,C,H,W]
    x = F.interpolate(x, size=(height, width), mode="bicubic", align_corners=False)
    return x.permute(0, 2, 3, 1).clamp(0, 1).contiguous()


class GrokImageAPINode:
    CATEGORY = "Luna/Grok"
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "generate"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model": (_IMAGE_MODELS,),
            "prompt": _PROMPT,
            "aspect_ratio": (_IMAGE_AR + ["auto"], {
                "tooltip": "'auto' (the API default) lets the model pick."}),
            "number_of_images": ("INT", {"default": 1, "min": 1, "max": 10}),
            "resolution": (["1K", "1.5K", "2K"], {
                "tooltip": "1.5K added with Image 2.0 (Aug 2026). 2.0 price by quality "
                           "x resolution: low $0.04/0.05/0.06, medium $0.06/0.07/0.08."}),
            "quality": (["default", "low", "medium", "auto"], {
                "tooltip": "'default' sends nothing (= auto). -quality/-pro do not take it. "
                           "Note: -quality and -pro retire 2026-11-02 and are then served "
                           "as 2.0 at quality=low."}),
            "seed": _SEED,
            "timeout_seconds": ("INT", {"default": 120, "min": 10, "max": 600}),
        }, "optional": {
            "width": _WIDTH_IN,
            "height": _HEIGHT_IN,
        }}

    def generate(self, model, prompt, aspect_ratio, number_of_images, resolution, seed,
                 timeout_seconds, quality="default", width=0, height=0):
        if not prompt.strip():
            raise ValueError("Prompt is required.")
        if width and height and width > 0 and height > 0:
            aspect_ratio = _nearest_ar(int(width), int(height), _IMAGE_AR)
        key = _resolve_xai_key()
        body = {
            "model": model, "prompt": prompt, "aspect_ratio": aspect_ratio,
            "n": int(number_of_images), "seed": int(seed),
            "response_format": "b64_json", "resolution": resolution.lower(),
        }
        # `quality`: documented for 2.0; grok-imagine-image also accepted it live
        # (2026-10-04). Not sent to -quality / -pro.
        if quality != "default":
            if model in ("grok-imagine-image-quality", "grok-imagine-image-pro"):
                print(f"[Grok Imagine] quality not sent - {model} has no quality field")
            else:
                body["quality"] = quality
        if aspect_ratio == "auto":
            body.pop("aspect_ratio")
        resp = _post("/images/generations", key, body, timeout=float(timeout_seconds))
        images = _images_from_response(resp, key)
        if width and height and width > 0 and height > 0:
            images = _resize_batch_exact(images, int(width), int(height))
        return (images,)


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
            "resolution": (["1K", "1.5K", "2K"],),
            "number_of_images": ("INT", {"default": 1, "min": 1, "max": 10}),
            "aspect_ratio": (["auto"] + _IMAGE_AR,),
            "seed": _SEED,
            "timeout_seconds": ("INT", {"default": 120, "min": 10, "max": 600}),
        }, "optional": {
            "width": _WIDTH_IN,
            "height": _HEIGHT_IN,
        }}

    def generate(self, model, image, prompt, resolution, number_of_images, aspect_ratio,
                 seed, timeout_seconds, width=0, height=0):
        if not prompt.strip():
            raise ValueError("Prompt is required.")
        frames = list(_iter_images(image))
        n_in = len(frames)
        # Per-model input caps, LIVE-verified 2026-10-04: image / image-2.0 take
        # 5 ("at most 5"), image-quality (and its alias -pro) take 3.
        max_in = 3 if model in ("grok-imagine-image-quality", "grok-imagine-image-pro") else 5
        if n_in > max_in:
            raise ValueError(f"{model} accepts at most {max_in} input images (got {n_in}).")
        wired = bool(width and height and width > 0 and height > 0)
        # xAI only allows a custom aspect_ratio when multiple inputs are connected;
        # with 1 input we leave it "auto" and rely on the exact resize below.
        if wired and n_in > 1:
            aspect_ratio = _nearest_ar(int(width), int(height), _IMAGE_AR)
        if aspect_ratio != "auto" and n_in == 1:
            raise ValueError("Custom aspect ratio is only allowed when multiple images are connected.")
        key = _resolve_xai_key()
        resp = _post("/images/edits", key, {
            "model": model,
            "images": [{"url": _tensor_to_data_uri(f)} for f in frames],
            "prompt": prompt, "resolution": resolution.lower(),
            "n": int(number_of_images), "seed": int(seed),
            "response_format": "b64_json",
            "aspect_ratio": None if aspect_ratio == "auto" else aspect_ratio,
        }, timeout=float(timeout_seconds))
        images = _images_from_response(resp, key)
        if wired:
            images = _resize_batch_exact(images, int(width), int(height))
        return (images,)


_GEN_AUDIO = ("BOOLEAN", {"default": True,
                          "tooltip": "Off strips the audio track (live-checked on 1.5 and lite)."})
_PROMPT_OPT = ("STRING", {"multiline": True, "default": "",
                          "tooltip": "Text prompt. Optional when an image, reference or frame "
                                     "is connected. Refer to inputs as <IMAGE_0>, <IMAGE_1>... "
                                     "and voices as <AUDIO_0>..."})


def _video_ar(aspect_ratio: str):
    # The string "auto" is a 422 on video; null lets the model choose.
    return None if aspect_ratio == "auto" else aspect_ratio


class GrokVideoAPINode:
    CATEGORY = "Luna/Grok"
    RETURN_TYPES = ("VIDEO",)
    FUNCTION = "generate"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model": (_VIDEO_MODELS, {"tooltip": "1.5 $0.08/s at 480p (~$0.25/s at 1080p); "
                                                 "1.5-lite ~$0.02/s 480p, $0.03/s 720p, "
                                                 "$0.14/s 1080p (measured); classic $0.05/s."}),
            "prompt": _PROMPT_OPT,
            "resolution": (_VIDEO_RES,),
            "aspect_ratio": (_VIDEO_AR,),
            "duration": ("INT", {"default": 6, "min": 1, "max": 15}),
            "seed": _SEED,
            "timeout_seconds": ("INT", {"default": 120, "min": 10, "max": 600}),
            # appended last so saved workflows keep their widget positions
            "generate_audio": _GEN_AUDIO,
        }, "optional": {"image": ("IMAGE",), "width": _WIDTH_IN, "height": _HEIGHT_IN}}

    def generate(self, model, prompt, resolution, aspect_ratio, duration, seed,
                 timeout_seconds, generate_audio=True, image=None, width=0, height=0):
        if not prompt.strip() and image is None:
            raise ValueError("Prompt is required for text-to-video.")
        if width and height and width > 0 and height > 0:
            aspect_ratio = _nearest_ar(int(width), int(height), _VIDEO_AR[1:])
        _check_video_resolution(model, resolution)
        key = _resolve_xai_key()
        body = {
            "model": model, "resolution": resolution,
            "duration": int(duration), "aspect_ratio": _video_ar(aspect_ratio),
            "generate_audio": bool(generate_audio),
        }
        if prompt.strip():
            body["prompt"] = prompt
        if image is not None:
            frames = list(_iter_images(image))
            if len(frames) != 1:
                raise ValueError("Only one input image is supported (use Reference-to-Video for more).")
            body["image"] = {"url": _tensor_to_data_uri(frames[0])}
        init = _post("/videos/generations", key, body, timeout=float(timeout_seconds))
        url = _poll_video(init["request_id"], key, poll_timeout=float(timeout_seconds))
        return (_video_output(url, key),)


def _voice_refs(voice_1, voice_2, voice_3, custom_voice_ids) -> list:
    ids = [v for v in (voice_1, voice_2, voice_3) if v and v != "none"]
    ids += [v.strip() for v in (custom_voice_ids or "").split(",") if v.strip()]
    if len(ids) > 3:
        raise ValueError(f"xAI takes at most 3 voices per request (got {len(ids)}).")
    return [{"voice_id": v} for v in ids]


class GrokVideoReferenceAPINode:
    CATEGORY = "Luna/Grok"
    RETURN_TYPES = ("VIDEO",)
    FUNCTION = "generate"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model": (["grok-imagine-video-1.5"], {
                "tooltip": "Only 1.5 does reference-to-video; lite and classic are rejected."}),
            "prompt": _PROMPT_OPT,
            "resolution": (["480p", "720p"],),  # any reference request at 1080p is a 400
            "aspect_ratio": (_VIDEO_AR,),
            "duration": ("INT", {"default": 6, "min": 1, "max": 15}),
            "seed": _SEED,
            "timeout_seconds": ("INT", {"default": 120, "min": 10, "max": 600}),
            # appended last so saved workflows keep their widget positions
            "generate_audio": _GEN_AUDIO,
            "voice_1": (_VOICE_OPTS, {"tooltip": "Preset voice, referenced as <AUDIO_0>."}),
            "voice_2": (_VOICE_OPTS,),
            "voice_3": (_VOICE_OPTS,),
        }, "optional": {
            "reference_images": ("IMAGE", {"tooltip": "Up to 14 (live-verified), as <IMAGE_n>. "
                                                      "A batch is expanded in order. Each extra "
                                                      "image adds ~$0.01."}),
            "first_frame": ("IMAGE", {"tooltip": "Optional pinned first frame; it becomes "
                                                 "<IMAGE_0> and the references follow it."}),
            "custom_voice_ids": ("STRING", {"default": "", "tooltip":
                                 "Comma-separated custom voice ids (from /v1/custom-voices). "
                                 "Counted with the presets; 3 total."}),
            "width": _WIDTH_IN,
            "height": _HEIGHT_IN,
        }}

    def generate(self, model, prompt, resolution, aspect_ratio, duration, seed,
                 timeout_seconds, generate_audio=True, voice_1="none", voice_2="none",
                 voice_3="none", reference_images=None, first_frame=None,
                 custom_voice_ids="", width=0, height=0):
        if width and height and width > 0 and height > 0:
            aspect_ratio = _nearest_ar(int(width), int(height), _VIDEO_AR[1:])
        _check_video_resolution(model, resolution, mode="reference")
        frames = list(_iter_images(reference_images)) if reference_images is not None else []
        if len(frames) > _MAX_REFS:
            raise ValueError(f"xAI takes at most {_MAX_REFS} reference images (got {len(frames)}).")
        voices = _voice_refs(voice_1, voice_2, voice_3, custom_voice_ids)
        if not frames and not voices:
            raise ValueError("Connect at least one reference image or pick a voice.")
        key = _resolve_xai_key()
        body = {
            "model": model, "resolution": resolution, "duration": int(duration),
            "aspect_ratio": _video_ar(aspect_ratio), "generate_audio": bool(generate_audio),
        }
        if prompt.strip():
            body["prompt"] = prompt
        if frames:
            body["reference_images"] = [{"url": _tensor_to_data_uri(f)} for f in frames]
        if voices:
            body["reference_audios"] = voices
        if first_frame is not None:
            body["image"] = {"url": _tensor_to_data_uri(next(_iter_images(first_frame)))}
        init = _post("/videos/generations", key, body, timeout=float(timeout_seconds))
        url = _poll_video(init["request_id"], key, poll_timeout=float(timeout_seconds))
        return (_video_output(url, key),)


class GrokVideoFramesAPINode:
    """First frame / last frame / keyframes -> video (grok-imagine-video-1.5)."""
    CATEGORY = "Luna/Grok"
    RETURN_TYPES = ("VIDEO",)
    FUNCTION = "generate"

    @classmethod
    def INPUT_TYPES(cls):
        kf = {}
        for i in range(1, 5):
            kf[f"keyframe_{i}"] = ("IMAGE", {"tooltip": f"Keyframe {i}, placed at keyframe_{i}_time."})
            kf[f"keyframe_{i}_time"] = ("FLOAT", {"default": float(i), "min": 0.0, "max": 15.0,
                                                  "step": 0.333, "tooltip":
                                                  "Seconds. Must be strictly inside the clip; "
                                                  "snapped to the 1/3 s grid xAI uses."})
        return {"required": {
            "model": (["grok-imagine-video-1.5"], {"tooltip": "1.5 only - lite and classic reject "
                                                              "last_frame and keyframes."}),
            "prompt": _PROMPT_OPT,
            "resolution": (["480p", "720p"],),  # 1080p is a 400 with frames
            "aspect_ratio": (_VIDEO_AR,),
            "duration": ("INT", {"default": 6, "min": 1, "max": 15}),
            "generate_audio": _GEN_AUDIO,
            "seed": _SEED,
            "timeout_seconds": ("INT", {"default": 120, "min": 10, "max": 600}),
        }, "optional": {
            "first_frame": ("IMAGE", {"tooltip": "Pinned first frame. With last_frame the clip "
                                                 "interpolates between the two."}),
            "last_frame": ("IMAGE", {"tooltip": "Pinned final frame. Can be used alone."}),
            **kf,
            "reference_images": ("IMAGE", {"tooltip": "Optional extra references (up to 14)."}),
            "width": _WIDTH_IN,
            "height": _HEIGHT_IN,
        }}

    def generate(self, model, prompt, resolution, aspect_ratio, duration, seed,
                 timeout_seconds, generate_audio=True, first_frame=None, last_frame=None,
                 reference_images=None, width=0, height=0, **kw):
        if width and height and width > 0 and height > 0:
            aspect_ratio = _nearest_ar(int(width), int(height), _VIDEO_AR[1:])
        _check_video_resolution(model, resolution, mode="frames")
        dur = int(duration)
        keyframes = []
        for i in range(1, 5):
            img = kw.get(f"keyframe_{i}")
            if img is None:
                continue
            t = round(float(kw.get(f"keyframe_{i}_time", i)) * 3) / 3.0
            if not 0.0 < t < dur:
                raise ValueError(f"keyframe_{i}_time {t:.2f}s must be strictly inside the "
                                 f"{dur}s clip.")
            keyframes.append({"image": {"url": _tensor_to_data_uri(next(_iter_images(img)))},
                              "timestamp_s": round(t, 3)})
        if first_frame is None and last_frame is None and not keyframes:
            raise ValueError("Connect a first_frame, a last_frame or at least one keyframe.")
        key = _resolve_xai_key()
        body = {
            "model": model, "resolution": resolution, "duration": dur,
            "aspect_ratio": _video_ar(aspect_ratio), "generate_audio": bool(generate_audio),
        }
        if prompt.strip():
            body["prompt"] = prompt
        if first_frame is not None:
            body["image"] = {"url": _tensor_to_data_uri(next(_iter_images(first_frame)))}
        if last_frame is not None:
            body["last_frame"] = {"url": _tensor_to_data_uri(next(_iter_images(last_frame)))}
        if keyframes:
            body["keyframes"] = sorted(keyframes, key=lambda k: k["timestamp_s"])
        if reference_images is not None:
            refs = list(_iter_images(reference_images))
            if len(refs) > _MAX_REFS:
                raise ValueError(f"xAI takes at most {_MAX_REFS} reference images.")
            body["reference_images"] = [{"url": _tensor_to_data_uri(f)} for f in refs]
        init = _post("/videos/generations", key, body, timeout=float(timeout_seconds))
        url = _poll_video(init["request_id"], key, poll_timeout=float(timeout_seconds))
        return (_video_output(url, key),)


class GrokVideoEditAPINode:
    CATEGORY = "Luna/Grok"
    RETURN_TYPES = ("VIDEO",)
    FUNCTION = "generate"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model": (_VIDEO_EDIT_MODELS, {"tooltip": "Only the classic model accepts edits."}),
            "prompt": _PROMPT,
            "video": ("VIDEO", {"tooltip": "mp4. Output keeps its duration (capped at 8.7 s) "
                                           "and aspect ratio, at most 720p."}),
            "seed": _SEED,
            "timeout_seconds": ("INT", {"default": 120, "min": 10, "max": 600}),
        }}

    def generate(self, model, prompt, video, seed, timeout_seconds):
        if not prompt.strip():
            raise ValueError("Prompt is required.")
        key = _resolve_xai_key()
        init = _post("/videos/edits", key, {
            "model": model, "prompt": prompt,
            "video": {"url": _video_to_data_uri(video)},
        }, timeout=float(timeout_seconds))
        url = _poll_video(init["request_id"], key, poll_timeout=float(timeout_seconds))
        return (_video_output(url, key),)


class GrokVideoExtendAPINode:
    CATEGORY = "Luna/Grok"
    RETURN_TYPES = ("VIDEO",)
    FUNCTION = "generate"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model": (_VIDEO_EDIT_MODELS, {"tooltip": "Only the classic model accepts extensions."}),
            "prompt": _PROMPT,
            "video": ("VIDEO",),
            # Live: 2-10 ("Duration must be between 2 and 10 seconds"), and it is
            # the length ADDED, not the total. The old 2-15 / default 8 failed
            # asynchronously above 10.
            "duration": ("INT", {"default": 6, "min": 2, "max": 10,
                                 "tooltip": "Seconds to ADD (2-10)."}),
            "seed": _SEED,
            "timeout_seconds": ("INT", {"default": 120, "min": 10, "max": 600}),
        }}

    def generate(self, model, prompt, video, duration, seed, timeout_seconds):
        if not prompt.strip():
            raise ValueError("Prompt is required.")
        key = _resolve_xai_key()
        init = _post("/videos/extensions", key, {
            "model": model, "prompt": prompt,
            "video": {"url": _video_to_data_uri(video)},
            "duration": max(2, min(10, int(duration))),
        }, timeout=float(timeout_seconds))
        url = _poll_video(init["request_id"], key, poll_timeout=float(timeout_seconds))
        return (_video_output(url, key),)


NODE_CLASS_MAPPINGS = {
    "GrokImageAPINode": GrokImageAPINode,
    "GrokImageEditAPINode": GrokImageEditAPINode,
    "GrokVideoAPINode": GrokVideoAPINode,
    "GrokVideoReferenceAPINode": GrokVideoReferenceAPINode,
    "GrokVideoFramesAPINode": GrokVideoFramesAPINode,
    "GrokVideoEditAPINode": GrokVideoEditAPINode,
    "GrokVideoExtendAPINode": GrokVideoExtendAPINode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "GrokImageAPINode": "Grok Image (API Key)",
    "GrokImageEditAPINode": "Grok Image Edit (API Key)",
    "GrokVideoAPINode": "Grok Video (API Key)",
    "GrokVideoReferenceAPINode": "Grok Reference-to-Video (API Key)",
    "GrokVideoFramesAPINode": "Grok Video Frames (API Key)",
    "GrokVideoEditAPINode": "Grok Video Edit (API Key)",
    "GrokVideoExtendAPINode": "Grok Video Extend (API Key)",
}
