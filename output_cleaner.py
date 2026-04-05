"""Output cleaner for LLM responses.

Strips thinking tags, code fences, planning paragraphs, role prefixes,
JSON wrappers, and chat template tokens from model output.
"""

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CleanConfig:
    mode: str = "prompt"  # "prompt" or "text"
    strip_think: bool = True
    strip_code_fences: bool = True
    strip_role_prefixes: bool = True
    strip_json_wrappers: bool = True
    strip_leading_preamble: bool = True
    strip_planning: bool = True


_ROLE_PREFIX_RE = re.compile(
    r"^\s*(assistant|final|output|response|result|prompt)\s*:\s*", re.IGNORECASE
)
_CODE_FENCE_RE = re.compile(r"^\s*```[\w-]*\s*$", re.IGNORECASE)
_THINK_BLOCK_RE = re.compile(r"<think[^>]*>.*?</think>", flags=re.IGNORECASE | re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think[^>]*>", flags=re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r"</think\s*>", flags=re.IGNORECASE)
_MARKER_RE = re.compile(
    r"(?im)^\s*(final|final answer|answer|output|result|prompt)\s*[:\-]\s*",
)
_IM_TOKEN_RE = re.compile(
    r"(?i)<\|?im_(start|end)\|?>|<im_(start|end)>|<\|endoftext\|>",
)
_PLANNING_RE = re.compile(
    r"(?is)\b("
    r"i\s+(should|need|must|will|want|am\s+going\s+to|have\s+to)\b|"
    r"let's\b|"
    r"first\b|next\b|then\b|"
    r"wait\b|"
    r"so\s+i\s+need\s+to\b|"
    r"i\s+should\s+focus\s+on\b"
    r")"
)


def clean_output(text: str, config: CleanConfig | None = None) -> str:
    """Clean LLM output by removing artifacts, thinking, planning, etc."""
    if not text:
        return ""

    cfg = config or CleanConfig()
    cleaned = text.strip()

    # Remove chat template tokens
    cleaned = _IM_TOKEN_RE.sub("", cleaned).strip()

    # Strip <think>...</think> blocks
    if cfg.strip_think:
        cleaned = _THINK_BLOCK_RE.sub("", cleaned)
        cleaned = _THINK_CLOSE_RE.sub("", cleaned)
        if _THINK_OPEN_RE.search(cleaned):
            cleaned = _THINK_OPEN_RE.sub("", cleaned)
            parts = re.split(r"\n\s*\n", cleaned, maxsplit=1)
            if len(parts) == 2:
                cleaned = parts[1]
        cleaned = cleaned.strip()

    cleaned = _IM_TOKEN_RE.sub("", cleaned).strip()

    # Strip code fences
    if cfg.strip_code_fences and "```" in cleaned:
        lines = [ln for ln in cleaned.splitlines() if not _CODE_FENCE_RE.match(ln)]
        cleaned = "\n".join(lines).strip()

    # Extract text from JSON wrappers
    if cfg.strip_json_wrappers:
        extracted = _extract_from_json(cleaned, mode=cfg.mode)
        if extracted is not None:
            cleaned = extracted.strip()

    # Drop "Here's the prompt:" style preamble
    if cfg.strip_leading_preamble:
        cleaned = _drop_preamble(cleaned).strip()

    # Strip planning paragraphs (prompt mode only)
    if cfg.strip_planning and cfg.mode == "prompt":
        without_planning = _strip_planning_paragraphs(cleaned)
        if without_planning:
            cleaned = without_planning

    # Strip role prefixes like "Assistant: ..."
    if cfg.strip_role_prefixes:
        lines = cleaned.splitlines()
        if lines:
            lines[0] = _ROLE_PREFIX_RE.sub("", lines[0])
        cleaned = "\n".join(lines).strip()

    cleaned = _MARKER_RE.sub("", cleaned).strip()
    return cleaned


def _extract_from_json(text: str, mode: str) -> str | None:
    candidate = text.strip()
    if not candidate or not (candidate.startswith("{") and candidate.endswith("}")):
        return None
    try:
        payload = json.loads(candidate)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None

    keys = (
        ["prompt", "final", "output", "text", "content"]
        if mode == "prompt"
        else ["text", "content", "output", "final"]
    )
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _drop_preamble(text: str) -> str:
    lines = text.splitlines()
    if not lines:
        return text
    keep_from = 0
    for i, ln in enumerate(lines[:20]):
        if _MARKER_RE.match(ln):
            keep_from = i
        if re.search(r"(?i)\bhere(?:'s| is)\b", ln) and i < 6:
            keep_from = max(keep_from, i + 1)
    return "\n".join(lines[keep_from:]).strip()


def _strip_planning_paragraphs(text: str) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    if not paragraphs:
        return ""
    kept = []
    dropping = True
    for p in paragraphs:
        if dropping and _PLANNING_RE.search(p):
            continue
        dropping = False
        kept.append(p)
    if not kept:
        return text.strip()
    return "\n\n".join(kept).strip()
