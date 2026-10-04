"""Provider size rules. No ComfyUI / torch dependency."""
from __future__ import annotations

import math

from .capabilities import caps_for

# Target pixel budgets for the free-size (2.x) models. 4K is the API's
# 8,294,400-pixel ceiling (3840x2160).
RESOLUTIONS = {"auto": None, "1K": 1024 * 1024, "2K": 2048 * 2048, "4K": 3840 * 2160}

_FIXED_SIZES = [(1024, 1024), (1536, 1024), (1024, 1536)]
_MIN_PIX, _MAX_PIX, _MAX_EDGE = 655_360, 8_294_400, 3840


def _snap16(v: float) -> int:
    return max(16, int(round(v / 16.0)) * 16)


def _fit_free(w: float, h: float) -> tuple[int, int]:
    """Clamp an arbitrary target into the 2.x rules: /16, edge <= 3840,
    aspect 1:3..3:1, pixel count inside the documented window."""
    ar = max(1 / 3, min(3.0, w / h))
    pix = max(_MIN_PIX, min(_MAX_PIX, w * h))
    w = math.sqrt(pix * ar)
    h = w / ar
    scale = min(1.0, _MAX_EDGE / max(w, h))
    w, h = _snap16(w * scale), _snap16(h * scale)
    # Rounding can step over either bound. Live: 1000x600 snapped to 1040x624
    # (648,960 px) and 400'd "below the current minimum pixel budget".
    while w * h > _MAX_PIX:
        w, h = (w - 16, h) if w >= h else (w, h - 16)
    while w * h < _MIN_PIX:
        w, h = (w, h + 16) if w >= h else (w + 16, h)
    return w, h


def _ar_float(ar: str) -> float:
    a, b = ar.split(":")
    return float(a) / float(b)


def nearest_aspect(width: int, height: int, choices) -> str:
    """The "a:b" entry of `choices` closest to width:height (log distance, so 2:1 and 1:2 are
    equally far from 1:1). Non-ratio entries such as "auto" are skipped."""
    r = float(width) / float(height)
    ratios = [c for c in choices if ":" in c]
    return min(ratios, key=lambda c: abs(math.log(_ar_float(c) / r)))


def _k_value(resolution: str) -> float | None:
    """The number in an "<n>K" label (1.5 for "1.5K"), or None."""
    try:
        return float(str(resolution).upper().rstrip("K"))
    except ValueError:
        return None


def _budget(resolution: str, notes) -> int:
    """Pixel budget for a resolution label. RESOLUTIONS keys map as they always have; any other
    label (the Studio also offers 0.5K / 1.5K) takes the next known budget up - 0.5K -> 1K
    (below the API's 655,360 px minimum anyway), 1.5K -> 2K - or 4K above that, with a note."""
    if resolution in RESOLUTIONS:
        return RESOLUTIONS[resolution] or RESOLUTIONS["1K"]
    want = _k_value(resolution)
    known = [k for k in RESOLUTIONS if RESOLUTIONS[k]]  # "1K", "2K", "4K"
    up = [k for k in known if want is not None and _k_value(k) >= want]
    used = up[0] if up else ("1K" if want is None else known[-1])
    notes.append(f"resolution {resolution} is not an OpenAI budget - used {used}")
    return RESOLUTIONS[used]


def openai_size(model, aspect_ratio, resolution, width, height, notes) -> str:
    """Widgets (or wired width/height) -> the API's `size` string."""
    wired = bool(width and height and width > 0 and height > 0)
    if caps_for(model).free_size:
        if wired:
            w, h = _fit_free(float(width), float(height))
            if (w, h) != (int(width), int(height)):
                # Every caller resizes the result back to width x height: the GPT Image node
                # and the Studio node (generate / edit / compose) resize the output; region
                # edits resize the patch to the crop / image box before compositing.
                notes.append(f"{width}x{height} -> {w}x{h} (API rules; output resized back)")
            return f"{w}x{h}"
        if aspect_ratio == "auto" and resolution == "auto":
            return "auto"
        pix = _budget(resolution, notes)
        ar = 1.0 if aspect_ratio == "auto" else _ar_float(aspect_ratio)
        w, h = _fit_free(math.sqrt(pix * ar), math.sqrt(pix / ar))
        return f"{w}x{h}"
    # 1.x / chatgpt-image-latest: three fixed sizes.
    if not wired and aspect_ratio == "auto":
        return "auto"
    ar = (float(width) / float(height)) if wired else _ar_float(aspect_ratio)
    w, h = min(_FIXED_SIZES, key=lambda s: abs(math.log((s[0] / s[1]) / ar)))
    if resolution not in ("auto", "1K"):
        notes.append(f"{model} has fixed sizes only - {resolution} ignored")
    return f"{w}x{h}"
