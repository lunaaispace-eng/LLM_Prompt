"""Output cleaner for LLM responses.

Same interface as QwenVL-Mod's AILab_OutputCleaner â€” strips thinking tags,
code fences, planning paragraphs, role prefixes, JSON wrappers, and
chat template tokens from model output.
"""

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class OutputCleanConfig:
    mode: str = "prompt"
    strip_think: bool = True
    strip_code_fences: bool = True
    strip_role_prefixes: bool = True
    strip_json_wrappers: bool = True
    strip_leading_preamble: bool = True
    strip_planning: bool = True
    keep_first_paragraph_only: bool = False


_ROLE_PREFIX_RE = re.compile(r"^\s*(assistant|final|output|response|result|prompt)\s*:\s*", re.IGNORECASE)
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
_THINKING_LINE_RE = re.compile(
    r"(?im)^\s*("
    r"thinking(\s+process)?\s*[:\-]|"
    r"reasoning\s*[:\-]|"
    r"analysis\s*[:\-]|"
    r"draft\s+construction\s*[:\-]|"
    r"final\s+(review|check|polish|draft)\s*[:\-]|"
    r"refining\s+and\s+merging\s*[:\-]|"
    r"internal\s+sections?\s*[:\-]|"
    r"token\s+count\s+check\s*[:\-]|"
    r"section\s+order\s+check\s*[:\-]|"
    r"ready\s+to\s+output\.?"
    r").*$"
)
_NEGATIVE_SECTION_RE = re.compile(
    r"\n+\s*(?:\*{0,2}\s*)?(?:negative\s+prompt|negative\s+prompts?)\s*(?:\*{0,2}\s*)?[:\-]\s*",
    re.IGNORECASE,
)
_POSITIVE_LABEL_STRIP_RE = re.compile(
    r"^(?:\*{0,2}\s*)?(?:positive\s+prompt|positive\s+prompts?)\s*(?:\*{0,2}\s*)?[:\-]\s*",
    re.IGNORECASE,
)
# Tokens that reliably signal the start of an SDXL/SD negative prompt block.
# Used as a heuristic fallback when the model omits labels and pipes entirely.
_NEGATIVE_STARTER_RE = re.compile(
    r"^(?:lowres|worst[\s_]quality|low[\s_]quality|bad[\s_]anatomy|bad[\s_]hands|"
    r"ugly[,\s]|blurry[,\s]|deformed[,\s]|mutated?[,\s]|poorly[\s_]drawn|"
    r"extra[\s_](?:limbs|fingers|digit)|missing[\s_]fingers|"
    r"jpeg[\s_]artifacts|watermark[,\s]|signature[,\s]|text[,\s]|error[,\s])",
    re.IGNORECASE,
)

_PLANNING_RE = re.compile(
    r"(?is)\b("
    # First-person planning intent â€” "I should", "I need to", "I will", etc.
    # Only fires when "I" is the subject of a planning verb, not in prose.
    r"i\s+(should|need\s+to|must|will|want\s+to|am\s+going\s+to|have\s+to)\b|"
    # "Let's" as planning opener â€” "Let's go with", "Let's choose"
    # NOT "let" as a general verb ("let light filter", "let color breathe")
    r"let's\s+(go\s+with|choose|use|pick|aim|try|focus|start|begin)\b|"
    # "So I need to", "So I should"
    r"so\s+i\s+(need|should|must|have\s+to)\b|"
    # "I should focus on", "I'll focus on"
    r"i\s+(should|will|'ll)\s+focus\s+on\b|"
    # "Wait," as a self-correction opener (only at start of sentence)
    r"(?:^|\.\s+)wait[,:]"
    r")"
)


def clean_model_output(text: str, config: OutputCleanConfig | None = None) -> str:
    if not text:
        return ""

    cfg = config or OutputCleanConfig()
    cleaned = (text or "").strip()

    cleaned = _IM_TOKEN_RE.sub("", cleaned).strip()

    if cfg.strip_think:
        cleaned = _THINK_BLOCK_RE.sub("", cleaned)
        cleaned = _THINK_CLOSE_RE.sub("", cleaned)
        if _THINK_OPEN_RE.search(cleaned):
            cleaned = _THINK_OPEN_RE.sub("", cleaned)
            parts = re.split(r"\n\s*\n", cleaned, maxsplit=1)
            if len(parts) == 2:
                cleaned = parts[1]
        cleaned = cleaned.strip()
        cleaned = _THINKING_LINE_RE.sub("", cleaned).strip()

    cleaned = _IM_TOKEN_RE.sub("", cleaned).strip()

    if cfg.strip_code_fences and "```" in cleaned:
        lines = [ln for ln in cleaned.splitlines() if not _CODE_FENCE_RE.match(ln)]
        cleaned = "\n".join(lines).strip()

    if cfg.strip_json_wrappers:
        maybe = _extract_from_json(cleaned, mode=cfg.mode)
        if maybe is not None:
            cleaned = maybe.strip()

    if cfg.strip_leading_preamble:
        cleaned = _drop_preamble(cleaned).strip()

    if cfg.strip_planning and cfg.mode == "prompt":
        without_planning = _strip_planning_paragraphs(cleaned)
        if without_planning:
            cleaned = without_planning

    if cfg.strip_role_prefixes:
        lines = cleaned.splitlines()
        if lines:
            lines[0] = _ROLE_PREFIX_RE.sub("", lines[0])
        cleaned = "\n".join(lines).strip()

    cleaned = _MARKER_RE.sub("", cleaned).strip()

    if cfg.keep_first_paragraph_only:
        parts = re.split(r"\n\s*\n", cleaned, maxsplit=1)
        cleaned = parts[0].strip()

    return cleaned


def _extract_from_json(text: str, mode: str) -> str | None:
    candidate = text.strip()
    if not candidate:
        return None
    if not (candidate.startswith("{") and candidate.endswith("}")):
        return None
    try:
        payload = json.loads(candidate)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None

    if mode == "prompt":
        preferred_keys = ["prompt", "final", "output", "text", "content"]
    else:
        preferred_keys = ["text", "content", "output", "final"]

    for key in preferred_keys:
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


def normalize_prompt_separator(text: str) -> str:
    """Normalize positive/negative prompt output to pipe-separated format.

    Models like Grok often ignore the pipe instruction and instead output:
      - Labeled sections:  "Positive prompt: ...\nNegative prompt: ..."
      - Plain paragraphs:  "beautiful woman...\n\nlowres, bad quality..."
      - Single newline:    "beautiful woman...\nlowres, bad quality..."

    Strategy (tried in order, stops at first match):
      1. Pipe already present → no-op.
      2. Labeled negative section ("Negative prompt:", "**Negative:**", etc.)
      3. Double-newline split where the second paragraph starts with a
         well-known negative-prompt keyword (lowres, bad anatomy, etc.).
      4. Single-newline split with the same keyword heuristic.
    """
    if "|" in text:
        return text

    # --- Strategy 2: labeled sections ---
    parts = _NEGATIVE_SECTION_RE.split(text, maxsplit=1)
    if len(parts) == 2:
        positive = _POSITIVE_LABEL_STRIP_RE.sub("", parts[0]).strip()
        negative = parts[1].strip()
        if positive and negative:
            return f"{positive}|{negative}"

    # --- Strategy 3: double-newline split + keyword heuristic ---
    paras = re.split(r"\n\s*\n", text.strip(), maxsplit=1)
    if len(paras) == 2:
        pos, neg = paras[0].strip(), paras[1].strip()
        # Strip any remaining positive label from the first paragraph
        pos = _POSITIVE_LABEL_STRIP_RE.sub("", pos).strip()
        if pos and neg and _NEGATIVE_STARTER_RE.match(neg):
            return f"{pos}|{neg}"

    # --- Strategy 4: single-newline split + keyword heuristic ---
    lines = text.strip().splitlines()
    if len(lines) == 2:
        pos, neg = lines[0].strip(), lines[1].strip()
        pos = _POSITIVE_LABEL_STRIP_RE.sub("", pos).strip()
        if pos and neg and _NEGATIVE_STARTER_RE.match(neg):
            return f"{pos}|{neg}"

    return text


def _strip_planning_paragraphs(text: str) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", (text or "").strip()) if p.strip()]
    if not paragraphs:
        return ""
    kept: list[str] = []
    dropping = True
    for p in paragraphs:
        is_planning = bool(_PLANNING_RE.search(p))
        if dropping and is_planning:
            continue
        dropping = False
        kept.append(p)
    if not kept:
        return text.strip()
    return "\n\n".join(kept).strip()