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
        # 1. Remove complete <think>...</think> blocks (opening + closing present).
        cleaned = _THINK_BLOCK_RE.sub("", cleaned)
        # 2. Orphan closing </think>: the opening tag was pre-filled into the
        #    PROMPT (LM Studio / the chat template injects "<think>" right before
        #    generation when thinking is on), so the model's response looks like
        #    "[reasoning]</think>[answer]" — no opening tag in the output. Keep
        #    only the text AFTER the last </think>; everything before is reasoning.
        if _THINK_CLOSE_RE.search(cleaned):
            cleaned = _THINK_CLOSE_RE.split(cleaned)[-1]
        # 3. Orphan opening <think> with no close (reasoning got truncated, e.g.
        #    max_tokens ran out mid-think). Drop the tag and any leading reasoning.
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
        cleaned = _truncate_after_json(cleaned)
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


def _truncate_after_json(text: str) -> str:
    """Drop anything trailing after the first balanced JSON object/array.

    Some models append stray closing braces (e.g. "...}}\\n}}") or commentary
    after an otherwise-valid object. raw_decode parses only the first value and
    reports where it ends, so we keep exactly that substring — preserving the
    model's original (compact) formatting. No-op unless the text starts with a
    JSON value and parses cleanly.
    """
    s = text.lstrip()
    if not s or s[0] not in "{[":
        return text
    try:
        _obj, end = json.JSONDecoder().raw_decode(s)
    except ValueError:
        return text
    return s[:end]


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


# Positive/negative boundary markers. These are DELIMITERS ONLY — every match is
# stripped, so no '[POSITIVE]' / 'Negative prompt:' residue ever reaches output.
_POS_MARKER_RE = re.compile(
    r'(?is)^\s*(?:\[\s*positive\s*\]|positive(?:\s+prompt)?\s*[:\-])\s*'
)
# The bracket form '[NEGATIVE]' is distinctive enough to match anywhere (incl.
# inline). The bare-word form ('Negative:' / 'Negative prompt:') is line-anchored
# so we never trip on the word "negative" inside prompt content (e.g. "negative
# space"). Either way a trailing newline is consumed so it leaves no residue.
_NEG_MARKER_RE = re.compile(
    r'(?i)(?:\[\s*negative\s*\]|(?:^|\n)[ \t]*negative(?:\s+prompt)?\s*[:\-])[ \t]*\n?'
)


def _strip_pos_marker(s: str) -> str:
    """Remove a leading [POSITIVE] / 'Positive:' marker so it never leaks out."""
    return _POS_MARKER_RE.sub("", s or "", count=1).strip()


def _try_json_pos_neg(text: str):
    """Parse {"positive": ..., "negative": ...} (optionally code-fenced). None if not JSON."""
    import json
    s = re.sub(r'^```(?:json)?|```$', '', (text or "").strip(), flags=re.IGNORECASE).strip()
    if not (s.startswith("{") and "positive" in s.lower()):
        return None
    try:
        obj = json.loads(s)
    except Exception:
        return None
    if isinstance(obj, dict):
        pos = obj.get("positive") or obj.get("Positive") or ""
        neg = obj.get("negative") or obj.get("Negative") or ""
        if isinstance(pos, str) and pos.strip():
            return pos.strip(), (neg.strip() if isinstance(neg, str) else "")
    return None


def split_positive_negative(text: str, do_split: bool) -> tuple[str, str]:
    """Split model output into (positive, negative) — robust to a dropped delimiter.

    When do_split is True, tries in order: JSON {positive, negative}; labelled
    markers ([POSITIVE]/[NEGATIVE], 'Negative prompt:', 'NEGATIVE:'); the legacy
    '|' pipe. ALL markers/labels are stripped, so the returned strings contain no
    bracket or label residue. If nothing matches (or do_split is False), the whole
    text goes to positive and negative is empty — the prompt is never lost.
    """
    text = (text or "").strip()
    if not text:
        return "", ""
    if not do_split:
        return _strip_pos_marker(text), ""

    # 1) JSON object
    parsed = _try_json_pos_neg(text)
    if parsed:
        return parsed

    # 2) Negative marker / label — split on the first one, strip both markers
    m = _NEG_MARKER_RE.search(text)
    if m:
        positive = _strip_pos_marker(text[:m.start()])
        negative = text[m.end():].strip()
        if positive:  # only accept if real positive content precedes the marker
            return positive, negative

    # 3) Legacy pipe
    if "|" in text:
        pos, _, neg = text.partition("|")
        return _strip_pos_marker(pos), neg.strip()

    # 4) Fallback — the whole thing is the positive prompt
    return _strip_pos_marker(text), ""


def normalize_prompt_separator(text: str) -> str:
    """Normalize labeled positive/negative sections to pipe-separated format.

    Some models (e.g. Grok) output labeled sections like:
        Positive prompt: beautiful woman...
        Negative prompt: lowres, worst quality...
    instead of the pipe format required by Prompt Splitter nodes:
        beautiful woman...|lowres, worst quality...

    Only handles structurally labeled sections — no content-based heuristics,
    since negative prompt content varies entirely by system prompt. No-op if
    | is already present or no recognizable negative section label is found.
    """
    if "|" in text:
        return text

    parts = _NEGATIVE_SECTION_RE.split(text, maxsplit=1)
    if len(parts) != 2:
        return text

    positive = _POSITIVE_LABEL_STRIP_RE.sub("", parts[0]).strip()
    negative = parts[1].strip()
    if not positive or not negative:
        return text

    return f"{positive}|{negative}"


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