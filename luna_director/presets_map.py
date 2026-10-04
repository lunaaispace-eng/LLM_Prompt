"""Target image model + operation -> the writer preset title (Director plan, A4; S10).

One server-side place for the default writer preset (R-7). The preset is picked from the chosen
*target model*, never from the request text, and the user can override it per tab. Keys are model
id prefixes; the longest matching prefix wins. Stage B adds the package targets ("krea2",
"qwen-edit", "flux-klein") together with their preset files.

Stdlib only; ComfyUI-free (Global Constraints).
"""
from __future__ import annotations

EDIT_REWRITE: dict[str, str] = {
    "gpt-image": "Edit Rewrite - GPT Image",
    "chatgpt-image": "Edit Rewrite - GPT Image",
    "gemini": "Edit Rewrite - Gemini Image",
    "nano-banana": "Edit Rewrite - Gemini Image",
    "grok-imagine": "Edit Rewrite - Grok Imagine",
}

GENERATE: dict[str, str] = {
    "gpt-image": "Generate - GPT Image",
    "chatgpt-image": "Generate - GPT Image",
    "gemini": "Generate - Gemini",
    "nano-banana": "Generate - Gemini",
    "grok-imagine": "Generate - Grok",
}

GENERATE_OPERATIONS = ("generate", "compose")


def preset_for_target(target_model: str, operation: str = "edit") -> str | None:
    """The preset title for `target_model` (longest prefix), or None when no entry matches.
    `generate` / `compose` read GENERATE; every other operation reads EDIT_REWRITE."""
    table = GENERATE if operation in GENERATE_OPERATIONS else EDIT_REWRITE
    model = (target_model or "").strip().lower()
    best: tuple[str, str] | None = None
    for prefix, title in table.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, title)
    return best[1] if best else None
