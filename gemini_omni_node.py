"""Gemini Omni Video (API Key) — Google's Omni video model on your own key.

Omni is NOT a generateContent model. Every call to it on generateContent 400s
with "This model only supports Interactions API." (live, 2026-10-04), so this
node speaks the Interactions API directly:

    POST https://generativelanguage.googleapis.com/v1beta/interactions
    {"model": ..., "input": ..., "response_format": {"type": "video", ...}}

Facts established by live probe on 2026-10-04 (not from the docs, which were
wrong about `delivery`):
  - The call is synchronous: one 200 with status "completed", ~10-20 s at 360p.
  - The video sits in steps[-1].content[0] as {type: video, mime_type, data}.
  - `delivery` is "inline" or "uri". The docs' "base64" is a 400.
  - aspect_ratio is 16:9 or 9:16 only; resolution 360p / 720p / 1080p / 4k.
  - Validation is strict: unknown keys 400, and there is NO duration control —
    every clip came back 10.0 s, 24 fps, H.264 + AAC.
  - Omni does NOT output still images. response_format {"type": "image"} is
    accepted and still returns a 10 s mp4. For a still, take a frame.
  - Cost: 360p 10 s = 19,310 video tokens (~$0.34); 720p = 57,920 (~$1.01).

Also live, same day:
  - Image-to-video: `input` = [{type: image, data, mime_type}, {type: text}] works.
  - Edit via previous_interaction_id (the interaction_id output of an earlier
    run) works — the stateful path, no upload.
  - Extend REJECTS previous_interaction_id ("Only user input is allowed when
    video task is set.") and rejects aspect_ratio ("Aspect ratio cannot be set
    in response format for extend task."), so extend = uploaded clip only.
  - Edit or extend of an UPLOADED clip came back `content_blocked` ("The prompt
    contains sensitive words...") for harmless prompts on Peti's key. Google
    documents uploaded-video editing as unavailable in the EEA, Switzerland and
    the UK; this is most likely that restriction surfacing under a misleading
    message. Not proven — the upload path is kept for keys outside the region.

Docs-only: max 3 reference images; one edit/extend video of at most 10 s as a
Files-API URI (never base64); extend appends 3-10 s up to 40 s total.

Key from GEMINI_API_KEY via the same env/.env resolver as the other nodes —
never a widget, never in the workflow.
"""

from __future__ import annotations

import base64
import io as _io
import json
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import torch
from PIL import Image

from comfy_api.latest import io

from .llm_prompt_api_node import _resolve_api_key, _comfyui_root

_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Both appear on ListModels for this key with generateContent listed — which is
# wrong; both reject generateContent. 1.1 first: it is the GA model.
OMNI_MODELS = ["gemini-omni-1.1-flash", "gemini-omni-flash-preview"]
ASPECT_RATIOS = ["16:9", "9:16"]
RESOLUTIONS = ["360p", "720p", "1080p", "4k"]
MODES = ["generate", "edit video", "extend video"]
DELIVERY = ["auto", "inline", "uri"]
MAX_REFS = 3


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _request(method: str, url: str, key: str, payload: dict | None = None,
             timeout: float = 600.0, raw: bool = False):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("x-goog-api-key", key)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:800]
        raise RuntimeError(f"Gemini Omni HTTP {e.code}: {detail}") from None
    return body if raw else json.loads(body.decode("utf-8"))


def _wait_file_active(key: str, name: str, timeout: float) -> dict:
    """Poll GET files/{id} until ACTIVE. `name` is "files/<id>"."""
    deadline = time.monotonic() + timeout
    while True:
        meta = _request("GET", f"{_BASE}/{name}", key, timeout=60)
        state = meta.get("state")
        if state == "ACTIVE":
            return meta
        if state == "FAILED":
            raise RuntimeError(f"Gemini could not process {name}.")
        if time.monotonic() > deadline:
            raise TimeoutError(f"Timed out waiting for {name} (state={state}).")
        time.sleep(2.0)


def _file_name_from_uri(uri: str) -> str:
    """'https://.../v1beta/files/abc' or 'files/abc' -> 'files/abc'."""
    tail = uri.split("/files/", 1)[-1] if "/files/" in uri else uri.split("files/", 1)[-1]
    return "files/" + tail.split("?", 1)[0].split(":", 1)[0]


# ---------------------------------------------------------------------------
# Inputs / outputs
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
    """Autogrow dict (numeric slot order) or a plain IMAGE batch -> frames."""
    if src is None:
        return []
    if isinstance(src, dict):
        def _slot(name):
            tail = str(name).rsplit("_", 1)[-1]
            return int(tail) if tail.isdigit() else 0
        frames = []
        for _, v in sorted(src.items(), key=lambda kv: _slot(kv[0])):
            frames.extend(_iter_images(v))
        return frames
    return list(_iter_images(src))


def _png_b64(frame: torch.Tensor) -> str:
    arr = (frame.clamp(0, 1) * 255.0).to(torch.uint8).cpu().numpy()
    buf = _io.BytesIO()
    Image.fromarray(arr, mode="RGB").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _upload_video(key: str, video, timeout: float) -> tuple[str, str]:
    """Export a ComfyUI VIDEO to mp4, upload it to the Files API, wait for
    ACTIVE. Returns (file name, file uri). Uses the SDK's uploader — the same
    path gemini_video.py has used in production for the API node."""
    if not callable(getattr(video, "save_to", None)):
        raise ValueError("Connect a ComfyUI VIDEO (e.g. Load Video / Create Video).")
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=key)
    with tempfile.TemporaryDirectory(prefix="gemini_omni_") as d:
        path = Path(d) / "input.mp4"
        video.save_to(str(path), format="mp4", codec="h264")
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("Video export produced an empty file.")
        up = client.files.upload(file=str(path),
                                 config=types.UploadFileConfig(mime_type="video/mp4"))
    meta = _wait_file_active(key, up.name, timeout)
    return up.name, meta.get("uri") or up.uri


def _delete_file(key: str, name: str) -> None:
    try:
        _request("DELETE", f"{_BASE}/{name}", key, timeout=30)
    except Exception:
        print(f"[Gemini Omni] could not delete {name}; it expires under Files retention.")


def _extract_video(resp: dict, key: str, timeout: float) -> tuple[bytes, str]:
    """Return (mp4 bytes, how it was delivered)."""
    for step in reversed(resp.get("steps") or []):
        for item in (step.get("content") or []):
            if item.get("type") != "video":
                continue
            if item.get("data"):
                return base64.b64decode(item["data"]), "inline"
            uri = item.get("uri") or item.get("file_uri")
            if uri:
                name = _file_name_from_uri(uri)
                _wait_file_active(key, name, timeout)
                data = _request("GET", f"{_BASE}/{name}:download?alt=media", key,
                                timeout=timeout, raw=True)
                return data, "uri"
    status = resp.get("status")
    raise RuntimeError(f"No video in the Omni response (status={status}): "
                       f"{json.dumps(resp)[:600]}")


def _video_from_bytes(data: bytes):
    from comfy_api.latest import InputImpl
    return InputImpl.VideoFromFile(_io.BytesIO(data))


# ---------------------------------------------------------------------------
# The node
# ---------------------------------------------------------------------------

class GeminiOmniVideoNode(io.ComfyNode):
    """Text/image-to-video, video edit and video extend on Gemini Omni."""

    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="LunaGeminiOmniVideo",
            display_name="Gemini Omni Video (API Key)",
            category="Luna/LLM",
            description=(
                "Google Gemini Omni video generation, image-to-video, edit and "
                "extend with your own GEMINI_API_KEY, over the Interactions API. "
                "Omni outputs VIDEO only (10 s, 24 fps, with audio) — it has no "
                "image output and no duration control."
            ),
            inputs=[
                io.String.Input(
                    "prompt", multiline=True, default="",
                    tooltip="What to generate, or how to change the input video."),
                io.Combo.Input("model", options=OMNI_MODELS, default=OMNI_MODELS[0]),
                io.Combo.Input(
                    "mode", options=MODES, default="generate",
                    tooltip="generate: text or reference images -> video. "
                            "edit video: change the connected clip (<=10 s). "
                            "extend video: append 3-10 s to it (40 s cap). "
                            "Edit/extend are not offered in the EEA, CH or UK."),
                io.Combo.Input("aspect_ratio", options=ASPECT_RATIOS, default="16:9",
                               tooltip="Omni accepts only 16:9 and 9:16."),
                io.Combo.Input(
                    "resolution", options=RESOLUTIONS, default="720p",
                    tooltip="360p ~$0.34 per 10 s clip, 720p ~$1.01 (live token "
                            "counts). 1080p/4k cost more and go out as a file "
                            "download when delivery is auto."),
                io.Int.Input(
                    "seed", default=0, min=0, max=0x7FFFFFFF, control_after_generate=True,
                    tooltip="Re-run trigger only. Omni takes no seed field — its "
                            "validation rejects unknown keys."),
                io.Autogrow.Input(
                    "reference_images", optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        input=io.Image.Input("ref"),
                        prefix="reference_image_", min=0, max=MAX_REFS,
                    ),
                    tooltip="Optional. Up to 3 images: one = image-to-video (start "
                            "frame / subject), several = references. Sockets grow "
                            "as you connect them."),
                io.Video.Input(
                    "video", optional=True,
                    tooltip="Required for edit / extend. One clip, at most 10 s. "
                            "Uploaded to the Gemini Files API and deleted afterwards."),
                io.String.Input(
                    "previous_interaction_id", default="", optional=True,
                    tooltip="Optional. Wire the interaction_id output of an earlier "
                            "Omni node here to edit/extend THAT result server-side "
                            "(stateful) instead of uploading a video. Takes "
                            "priority over the video socket."),
                io.Combo.Input(
                    "delivery", options=DELIVERY, default="auto",
                    tooltip="How the video comes back. auto = inline for 360p/720p, "
                            "file download for 1080p/4k (Google advises file "
                            "delivery above ~4 MB)."),
                io.Int.Input("timeout", default=600, min=60, max=1800, step=30),
            ],
            outputs=[
                io.Video.Output("video", tooltip="The generated or edited clip."),
                io.String.Output("interaction_id", tooltip="Feed into another Omni "
                                 "node's previous_interaction_id to edit or extend "
                                 "this clip server-side."),
                io.String.Output("info", tooltip="Model, settings sent, usage, timing."),
            ],
        )

    @classmethod
    def execute(cls, prompt, model, mode, aspect_ratio, resolution, seed,
                reference_images=None, video=None, previous_interaction_id="",
                delivery="auto", timeout=600) -> io.NodeOutput:
        key = _resolve_api_key("Gemini", "")
        if not key:
            raise RuntimeError(
                "No Gemini API key found. Set GEMINI_API_KEY or add it to "
                f"{_comfyui_root() / '.env'} — it is never stored in the workflow.")
        prompt = (prompt or "").strip()
        refs = _collect_refs(reference_images)
        notes = []
        if len(refs) > MAX_REFS:
            notes.append(f"{len(refs)} reference images connected; only the first "
                         f"{MAX_REFS} are sent")
            refs = refs[:MAX_REFS]
        prev_id = (previous_interaction_id or "").strip()
        # Live 2026-10-04: stateful EXTEND is rejected — "Only user input is
        # allowed when video task is set." Stateful EDIT works (61 s at 360p).
        # So extend always needs the uploaded clip.
        if mode == "extend video" and prev_id:
            if video is None:
                raise ValueError("extend video cannot use previous_interaction_id "
                                 "(Google rejects it); connect the clip to `video`.")
            notes.append("previous_interaction_id ignored for extend (not supported)")
            prev_id = ""
        if mode != "generate" and video is None and not prev_id:
            raise ValueError(f"mode '{mode}' needs a VIDEO or a previous_interaction_id.")
        if mode == "generate" and video is not None:
            notes.append("video input ignored in generate mode")
        if not prompt and not refs and mode == "generate":
            raise ValueError("Prompt is required.")

        if delivery == "auto":
            delivery = "inline" if resolution in ("360p", "720p") else "uri"

        parts = []
        for f in refs:
            parts.append({"type": "image", "data": _png_b64(f), "mime_type": "image/png"})

        uploaded = None
        started = time.time()
        try:
            if mode != "generate" and not prev_id:
                uploaded, file_uri = _upload_video(key, video, float(timeout))
                parts.append({"type": "video", "uri": file_uri, "mime_type": "video/mp4"})
            if prompt:
                parts.append({"type": "text", "text": prompt})

            body = {
                "model": model,
                # A bare string is the documented text-only form; anything with
                # media is a list of typed parts.
                "input": parts if (refs or uploaded) else prompt,
                "response_format": {
                    "type": "video", "aspect_ratio": aspect_ratio,
                    "resolution": resolution, "delivery": delivery,
                },
            }
            if prev_id and mode != "generate":
                body["previous_interaction_id"] = prev_id
            if mode == "extend video":
                body["generation_config"] = {"video_config": {"task": "extend"}}
                # Live 400 otherwise: "Aspect ratio cannot be set in response
                # format for extend task." The clip keeps its own aspect.
                body["response_format"].pop("aspect_ratio")

            try:
                resp = _request("POST", f"{_BASE}/interactions", key, body,
                                timeout=float(timeout))
            except RuntimeError as e:
                if uploaded and "content_blocked" in str(e):
                    raise RuntimeError(
                        f"{e}\n\nNote: harmless prompts get this too when the input "
                        "is an UPLOADED clip — Google does not offer uploaded-video "
                        "edit/extend in the EEA, Switzerland or the UK. For edit, "
                        "wire an earlier Omni node's interaction_id into "
                        "previous_interaction_id instead.") from None
                raise
            data, how = _extract_video(resp, key, float(timeout))
        finally:
            if uploaded:
                _delete_file(key, uploaded)

        elapsed = time.time() - started
        usage = resp.get("usage") or {}
        info = "\n".join([
            f"model      : {model}",
            f"mode       : {mode}",
            f"format     : {aspect_ratio} {resolution} (delivery {how})",
            f"refs sent  : {len(refs)}",
            f"bytes      : {len(data):,}",
            f"usage      : {json.dumps(usage)[:300]}",
            f"elapsed    : {elapsed:.1f}s",
        ] + [f"note       : {n}" for n in notes])
        print(f"[Gemini Omni] {model} {mode} {resolution}: {len(data)/1e6:.1f} MB in {elapsed:.1f}s")
        return io.NodeOutput(_video_from_bytes(data), str(resp.get("id") or ""), info)


NODE_CLASS_MAPPINGS = {"LunaGeminiOmniVideo": GeminiOmniVideoNode}
NODE_DISPLAY_NAME_MAPPINGS = {"LunaGeminiOmniVideo": "Gemini Omni Video (API Key)"}
