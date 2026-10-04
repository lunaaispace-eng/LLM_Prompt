from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCaps:
    provider: str
    model: str
    max_inputs: int
    native_mask: bool
    transparent: bool
    free_size: bool  # single source of the "2.x free size" rule


# OpenAI max_inputs is 16, the UI cap. Live (2026-10-04) the API accepted 101
# input images on the 2.x models (17 on 1.x, the most tried); documented ~16.
_O = "openai"
_G = "gemini"
_X = "xai"

_CAPS: dict[str, ModelCaps] = {c.model: c for c in [
    ModelCaps(_O, "gpt-image-2.5", 16, True, True, True),   # flare + sunburst
    ModelCaps(_O, "gpt-image-2", 16, True, False, True),    # no transparent background
    ModelCaps(_O, "gpt-image-1", 16, True, True, False),    # 1.x, 1-mini, 1.5: fixed sizes
    ModelCaps(_O, "chatgpt-image-latest", 16, True, True, False),  # fixed sizes like 1.x
    ModelCaps(_G, "gemini-", 14, False, False, False),      # 3.x family
    ModelCaps(_G, "gemini-2.5-flash-image", 3, False, False, False),
    ModelCaps(_G, "nano-banana", 14, False, False, False),  # nano-banana-pro etc.
    ModelCaps(_X, "grok-imagine-image", 5, False, False, False),   # image / 2.0
    ModelCaps(_X, "grok-imagine-image-quality", 3, False, False, False),
    ModelCaps(_X, "grok-imagine-image-pro", 3, False, False, False),
]}


def provider_for(model: str) -> str:
    if model.startswith(("gpt-", "chatgpt-")):
        return "openai"
    if model.startswith(("gemini-", "nano-banana")):
        return "gemini"
    if model.startswith("grok-"):
        return "xai"
    raise ValueError(f"unknown model: {model!r}")


def caps_for(model: str) -> ModelCaps:
    provider_for(model)  # raises ValueError for unknown families
    best = None
    for prefix, caps in _CAPS.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, caps)
    if best is None:
        raise ValueError(f"no capabilities for model: {model!r}")
    c = best[1]
    return ModelCaps(c.provider, model, c.max_inputs, c.native_mask, c.transparent, c.free_size)
