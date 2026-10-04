"""Provider cost rules. No ComfyUI / torch dependency."""
from __future__ import annotations

# USD per 1M tokens: (text in, image in, image out). Batch would be 50%.
# From OpenAI's pricing page, 2026-10-04. Cached-input discounts are not
# applied (the Images API reports no cached split for these calls).
OPENAI_RATES = {
    "gpt-image-2.5": (5.0, 8.0, 30.0),
    "gpt-image-2": (5.0, 8.0, 30.0),
    "gpt-image-1.5": (5.0, 8.0, 32.0),
    "chatgpt-image-latest": (5.0, 8.0, 32.0),
    "gpt-image-1-mini": (2.0, 2.5, 8.0),
    "gpt-image-1": (5.0, 10.0, 40.0),
}

# Output image tokens of gpt-image-2.5 (flare and sunburst alike) at 1024x1024, per quality. Deterministic per
# size x quality (MEASURED live 2026-10-04, ~$1.3 pass; moved here from openai_image_node.py so there is one
# source). gpt-image-2 `high` used 7024 tokens at 1024, so this table is 2.5-only.
OPENAI_OUT_TOKENS_1024 = {"low": 196, "medium": 439, "high": 1756, "xhigh": 3122, "max": 7024}
_OUT_TOKEN_TABLES = {"gpt-image-2.5": OPENAI_OUT_TOKENS_1024}

# USD per image, by output size.
GEMINI_IMAGE_PRICES = {
    "gemini-3.1-flash-image": {"0.5K": .045, "1K": .067, "2K": .101, "4K": .151},
    "gemini-3.1-flash-lite-image": {"1K": .0336},
    "gemini-3-pro-image": {"1K": .134, "2K": .134, "4K": .24},
    "nano-banana-pro": {"1K": .134, "2K": .134, "4K": .24},
}


def _longest_prefix(table: dict, model: str):
    best, n = None, -1
    for k, v in table.items():
        if model.startswith(k) and len(k) > n:
            best, n = v, len(k)
    return best


def _tokens(usage: dict) -> tuple[int, int, int, int]:
    ind = usage.get("input_tokens_details") or {}
    outd = usage.get("output_tokens_details") or {}
    return (int(ind.get("text_tokens") or 0),
            int(ind.get("image_tokens") or 0),
            int(outd.get("image_tokens") or usage.get("output_tokens") or 0),
            int(outd.get("text_tokens") or 0))


def openai_cost(model: str, usage: dict) -> float | None:
    """Image-model cost in USD from `usage`; None when unknown. Output TEXT
    tokens (1.x only) are not priced: their rate is not on the image table."""
    rates = _longest_prefix(OPENAI_RATES, model)
    if not usage or not rates:
        return None
    t_in, i_in, i_out, _ = _tokens(usage)
    return (t_in * rates[0] + i_in * rates[1] + i_out * rates[2]) / 1e6


def openai_estimate(model: str, quality: str, size: str) -> float | None:
    """Pre-run output cost in USD of one image at `size` ("WxH"): the 1024x1024 token table scaled by pixel area
    (INFERRED). None ("after run") for a model without a table, quality `auto` or a size that is not WxH.
    Input tokens (prompt, reference images) are not included."""
    table = _longest_prefix(_OUT_TOKEN_TABLES, model)
    rates = _longest_prefix(OPENAI_RATES, model)
    if not table or not rates or quality not in table:
        return None
    try:
        w, h = (int(v) for v in str(size).lower().split("x"))
    except ValueError:
        return None
    if w <= 0 or h <= 0:
        return None
    return table[quality] * (w * h) / (1024 * 1024) * rates[2] / 1e6


def openai_cost_line(model: str, usage: dict) -> str:
    usd = openai_cost(model, usage)
    if usd is None:
        return "n/a"
    t_in, i_in, i_out, t_out = _tokens(usage)
    extra = f" (+{t_out} text-out tokens, unpriced)" if t_out else ""
    return (f"${usd:.4f}  [in {t_in} text + {i_in} image, out {i_out} image tokens]"
            + extra)


def xai_cost(payload: dict) -> float | None:
    ticks = ((payload or {}).get("usage") or {}).get("cost_in_usd_ticks")
    if ticks is None:
        return None
    return float(ticks) / 1e10


def gemini_cost(model: str, size: str, n_images: int) -> float | None:
    prices = _longest_prefix(GEMINI_IMAGE_PRICES, model)
    if not prices or size not in prices:
        return None
    return prices[size] * n_images
