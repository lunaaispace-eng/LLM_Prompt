"""Per-image resize state, ported from the Luna Asset Loader (ComfyUI-SaveSimple `asset_loader.py`).

The state schema (key names and the `{"all": {...}, "items": [...]}` shape) follows ComfyUI-Pixaroma's
LoadImageMini state (MIT), credited as the Asset Loader's docstring does. The code here is written against the
Asset Loader's semantics and imports nothing from it, so a state written for the Asset Loader gives the same
pixels: aspect first (crop or pad), then size, then snap; LANCZOS only; no upscale unless `allow_upscale`.

Director extras: a free crop anchor `{"x": fx, "y": fy}` (fractions 0..1), `plan_resize` (pure geometry, so
the browser never recomputes the aspect maths) and `ratio_presets`.

Stdlib + Pillow only; ComfyUI-free.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass

from PIL import Image

DEFAULT_ITEM = {
    "mode": "off",
    "max_mp": 2.0,
    "longest_side": 1024,
    "scale_factor": 1.0,
    "ratio": "",
    "ratio_action": "crop",
    "crop_anchor": "center",
    "pad_color": "#000000",
    "snap": 0,
    "allow_upscale": False,
}

MODES = ("off", "max_mp", "longest_side", "scale_factor")
RATIO_PRESETS = ("", "1:1", "2:3", "3:2", "9:16", "16:9")
DIRECTOR_RATIOS = ("4:3", "3:4", "21:9")


def ratio_presets(model_aspects=()) -> list[str]:
    """Source + presets, then the Director ratios, then the model's own aspects, de-duplicated in order."""
    out: list[str] = []
    for r in (*RATIO_PRESETS, *DIRECTOR_RATIOS, *model_aspects):
        if r not in out:
            out.append(r)
    return out


def parse_state(raw, count: int) -> list[dict]:
    """JSON string (or dict) -> one settled state dict per image. Never raises: a bad state string
    must not cost a render. `all` is laid over the defaults, then `items[i]` over that."""
    base = dict(DEFAULT_ITEM)
    items: list[dict] = []
    try:
        if isinstance(raw, dict):
            parsed = raw
        else:
            parsed = json.loads(raw) if isinstance(raw, (str, bytes)) and raw.strip() else {}
        if isinstance(parsed, dict):
            shared = parsed.get("all")
            if isinstance(shared, dict):
                base.update({k: v for k, v in shared.items() if k in DEFAULT_ITEM})
            listed = parsed.get("items")
            if isinstance(listed, list):
                items = [x if isinstance(x, dict) else {} for x in listed]
    except Exception:
        pass
    out = []
    for i in range(count):
        st = dict(base)
        if i < len(items):
            st.update({k: v for k, v in items[i].items() if k in DEFAULT_ITEM})
        out.append(st)
    return out


def snap(v: int, step: int) -> int:
    """Nearest multiple of `step`, at least one step; with no step, at least 1."""
    return max(step, round(v / step) * step) if step and step > 1 else max(1, v)


@dataclass(frozen=True)
class ResizePlan:
    crop_box: tuple | None
    pad_size: tuple | None
    pad_offset: tuple
    out_size: tuple
    changed: bool


def _target_ratio(st: dict):
    ratio = str(st.get("ratio") or "").strip()
    if not ratio or ":" not in ratio:
        return None
    try:
        rw, rh = (float(x) for x in ratio.split(":", 1))
        target = rw / rh
    except Exception:
        return None
    return target if math.isfinite(target) and target > 0 else None


def _anchor_offsets(anchor, w, h, nw, nh) -> tuple[int, int]:
    if isinstance(anchor, dict):
        try:
            fx = min(1.0, max(0.0, float(anchor.get("x", 0.5))))
            fy = min(1.0, max(0.0, float(anchor.get("y", 0.5))))
            if math.isfinite(fx) and math.isfinite(fy):
                return round(fx * (w - nw)), round(fy * (h - nh))
        except (TypeError, ValueError):
            pass
        anchor = "center"
    anchor = str(anchor or "center")
    left = 0 if "left" in anchor else (w - nw if "right" in anchor else (w - nw) // 2)
    top = 0 if "top" in anchor else (h - nh if "bottom" in anchor else (h - nh) // 2)
    return left, top


def plan_resize(w: int, h: int, st: dict) -> ResizePlan:
    """Pure geometry of `apply_state`: what it would crop / pad and the size it would return."""
    if st.get("mode") == "off" and not st.get("ratio") and not st.get("snap"):
        return ResizePlan(None, None, (0, 0), (w, h), False)

    crop_box = pad_size = None
    pad_offset = (0, 0)
    cw, ch = w, h
    target = _target_ratio(st)
    if target and abs(w / h - target) > 0.001:
        if st.get("ratio_action") == "pad":
            nw, nh = (w, round(w / target)) if w / h > target else (round(h * target), h)
            pad_size = (max(nw, w), max(nh, h))
            pad_offset = ((pad_size[0] - w) // 2, (pad_size[1] - h) // 2)
            cw, ch = pad_size
        else:
            nw, nh = (round(h * target), h) if w / h > target else (w, round(w / target))
            left, top = _anchor_offsets(st.get("crop_anchor"), w, h, nw, nh)
            crop_box = (left, top, left + nw, top + nh)
            cw, ch = nw, nh

    mode = str(st.get("mode") or "off")
    scale = 1.0
    if mode == "max_mp":
        budget = float(st.get("max_mp", 2.0)) * 1_000_000
        if budget > 0:
            scale = math.sqrt(budget / (cw * ch))
    elif mode == "longest_side":
        scale = float(st.get("longest_side", 1024)) / max(cw, ch)
    elif mode == "scale_factor":
        scale = float(st.get("scale_factor", 1.0))
    if not st.get("allow_upscale", False):
        scale = min(1.0, scale)

    step = int(st.get("snap") or 0)
    ow, oh = snap(round(cw * scale), step), snap(round(ch * scale), step)
    out = (max(1, ow), max(1, oh)) if (ow, oh) != (cw, ch) else (cw, ch)
    changed = crop_box is not None or pad_size is not None or out != (w, h)
    return ResizePlan(crop_box, pad_size, pad_offset, out, changed)


def apply_state(img: Image.Image, st: dict) -> Image.Image:
    """Conform aspect first, then size, then snap. Returns `img` itself when nothing is asked."""
    if st.get("mode") == "off" and not st.get("ratio") and not st.get("snap"):
        return img
    w, h = img.size
    plan = plan_resize(w, h, st)
    if plan.pad_size:
        try:
            canvas = Image.new("RGB", plan.pad_size, st.get("pad_color") or "#000000")
        except (ValueError, TypeError):
            canvas = Image.new("RGB", plan.pad_size, "#000000")
        canvas.paste(img, plan.pad_offset)
        img = canvas
    elif plan.crop_box:
        img = img.crop(plan.crop_box)
    if img.size != plan.out_size:
        img = img.resize(plan.out_size, Image.LANCZOS)
    return img
