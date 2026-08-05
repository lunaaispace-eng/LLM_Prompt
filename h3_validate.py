"""Format checker for MiniMax H3 prompts.

The LLM writes the whole document; this checks it. Two formats exist and must
never be mixed:

  3-field  (T2VA / I2VA / FL2VA / L2VA) -- optional alignment line, then
           integrated_multimodal_description, overall_soundscape, non_diegetic_music
  6-section (ref2va)                    -- subject_definitions, summary,
           retention_analysis, detailed_description, overall_soundscape,
           non_diegetic_music

Reports findings; repairs nothing except the `S.SS` value inside an alignment
line, and only when the real frame count is known. Everything else is left
alone on purpose -- silent repair hides preset problems.
"""

from __future__ import annotations

import re

FPS = 24.0

THREE_FIELD = ["integrated_multimodal_description", "overall_soundscape", "non_diegetic_music"]
SIX_SECTION = [
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
]

_SHOT_RE = re.compile(r"\[Shot\s+(\d+)\]([^\n]*)")
_TS_RE = re.compile(r"At\s+(\d{2}):(\d{2})\.(\d{3})")
_SSS_RE = re.compile(r"(\d+\.\d{2})-second mark")
_LABEL_USE_RE = re.compile(r"<(Subject|Picture|Video|Audio)\s+(\d+)>")
_ALIGN_I2VA = "For the target video, at 0.00 seconds into the target video,"
_ALIGN_FL_L = "How the reference pictures align with the target video"


def effective_seconds(frames: int) -> float:
    """Frame count -> clip duration. The workflow snaps to a 17k+5 grid at 24 fps."""
    return round(int(frames) / FPS, 2)


def _sections_present(text: str, names: list[str]) -> list[str]:
    found = []
    for n in names:
        if re.search(rf"^\s*{re.escape(n)}\s*:", text, re.MULTILINE):
            found.append(n)
    return found


def validate_h3(text: str, frames: int | None = None) -> tuple[str, list[str]]:
    """Check an H3 prompt. Returns (possibly repaired text, findings)."""
    findings: list[str] = []
    if not text or not text.strip():
        return text, ["empty output"]

    three = _sections_present(text, THREE_FIELD)
    six = _sections_present(text, SIX_SECTION)
    six_only = [s for s in six if s not in THREE_FIELD]
    three_only = [s for s in three if s not in SIX_SECTION]

    # ---- which format, and is it mixed --------------------------------
    if six_only and three_only:
        findings.append(
            "FORMAT MIXED: contains both `integrated_multimodal_description` "
            f"and the six-section fields ({', '.join(six_only)}). Pick one."
        )
        expected = SIX_SECTION
    elif six_only:
        expected = SIX_SECTION
    else:
        expected = THREE_FIELD

    present = _sections_present(text, expected)

    # Not an H3 prompt at all -- a vision/analysis stage, most likely. Say that
    # once instead of listing every H3 section as missing.
    if not present and "[Shot" not in text:
        return text, [
            "this is not an H3 prompt (no H3 sections, no [Shot N] markers). "
            "If this node is a vision or analysis stage, set validate=off on it."
        ]

    missing = [s for s in expected if s not in present]
    if missing:
        findings.append(f"missing section(s): {', '.join(missing)}")

    # order
    positions = [(text.index(s), s) for s in present]
    if positions != sorted(positions):
        findings.append(
            "sections out of order — expected " + " -> ".join(expected)
        )

    # ---- alignment line -----------------------------------------------
    first_line = text.strip().splitlines()[0].strip()
    has_align = first_line.startswith(_ALIGN_I2VA) or first_line.startswith(_ALIGN_FL_L)
    if expected is SIX_SECTION:
        if has_align:
            findings.append("ref2va must not carry a keyframe alignment line")
    elif has_align and frames:
        want = f"{effective_seconds(frames):.2f}"
        # `0.00` is Picture 1's opening mark in an FL2VA line and is never the
        # duration — only the end-of-clip marks are checked against S.SS.
        found = _SSS_RE.findall(first_line)
        wrong = [v for v in found if v not in (want, "0.00")]
        if wrong:
            def _fix(mo):
                v = mo.group(1)
                return mo.group(0) if v == "0.00" else f"{want}-second mark"
            repaired = _SSS_RE.sub(_fix, first_line)
            text = text.replace(first_line, repaired, 1)
            findings.append(
                f"S.SS repaired: {', '.join(wrong)} -> {want} "
                f"({frames} frames at {FPS:g} fps)"
            )

    # ---- shots and timestamps ------------------------------------------
    # Scan the body field only. `[Shot N]` also appears inside alignment lines
    # ("(from [Shot 1])") and inside ref2va's retention_analysis
    # ("(appears in [Shot 1], [Shot 3])"), and those are references, not shots.
    body = text
    for field in ("integrated_multimodal_description", "detailed_description"):
        m = re.search(rf"^\s*{field}\s*:", text, re.MULTILINE)
        if m:
            body = text[m.end():]
            break
    shots = _SHOT_RE.findall(body)
    if not shots:
        findings.append("no [Shot N] markers found")
    else:
        nums = [int(n) for n, _ in shots]
        if nums != list(range(1, len(nums) + 1)):
            findings.append(f"shot numbering is {nums}, expected 1..{len(nums)}")

        times: list[float] = []
        for idx, (num, tail) in enumerate(shots):
            m = _TS_RE.search(tail)
            if idx == 0:
                if m:
                    findings.append("[Shot 1] carries a timestamp; it must not")
                continue
            if not m:
                findings.append(f"[Shot {num}] has no cut timestamp")
                continue
            mm, ss, ms = (int(g) for g in m.groups())
            times.append(mm * 60 + ss + ms / 1000.0)

        for a, b in zip(times, times[1:]):
            if b <= a:
                findings.append(f"cut times not increasing: {a:.3f} then {b:.3f}")
                break

        if frames and times:
            limit = effective_seconds(frames)
            over = [t for t in times if t >= limit]
            if over:
                findings.append(
                    f"cut time(s) at or past the end of the clip "
                    f"({', '.join(f'{t:.3f}' for t in over)} vs {limit:.2f}s)"
                )

    # ---- labels ---------------------------------------------------------
    used = {(k, int(n)) for k, n in _LABEL_USE_RE.findall(text)}
    if expected is SIX_SECTION:
        defs_block = text.split("summary:")[0]
        defined = {(k, int(n)) for k, n in _LABEL_USE_RE.findall(defs_block)}
        undefined = sorted(used - defined)
        if undefined:
            findings.append(
                "label(s) used but never defined in subject_definitions: "
                + ", ".join(f"<{k} {n}>" for k, n in undefined)
            )
    else:
        stray = sorted(x for x in used if x[0] in ("Subject", "Video", "Audio"))
        if stray:
            findings.append(
                "keyframe formats only use <Picture N>; found "
                + ", ".join(f"<{k} {n}>" for k, n in stray)
            )

    # ---- dialogue tags ---------------------------------------------------
    if text.count("<d>") != text.count("</d>"):
        findings.append(
            f"unbalanced dialogue tags: {text.count('<d>')} <d> vs {text.count('</d>')} </d>"
        )

    return text, findings


def format_log(text: str, frames: int | None, findings: list[str]) -> str:
    """Human-readable report for the node's `log` output."""
    head = "[H3 validate] "
    if frames:
        head += f"{frames} frames = {effective_seconds(frames):.2f}s. "
    if not findings:
        return head + "OK — no findings."
    return head + f"{len(findings)} finding(s):\n" + "\n".join(f"  - {f}" for f in findings)
