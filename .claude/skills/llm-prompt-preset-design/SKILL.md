---
name: llm-prompt-preset-design
description: Use when creating, editing, or debugging a prompts/*.md system-prompt preset in the LLM_Prompt pack, or when a preset produces a wrong output pattern.
---

# LLM_Prompt preset design canon

Each `prompts/*.md` file is a system prompt. YAML `title:` frontmatter is the dropdown label;
without it the filename becomes the label.

## Core rule
**Few-shot examples dominate prose rules, for every model tested (local Q4 through frontier).**
When a preset produces a wrong output pattern, fix the *example* that demonstrates the wrong
pattern first. A rule that contradicts an example loses. Don't reach for "add another rule" as
the first fix.

## Preset-specific exceptions — do not "normalise" these away
- **Ideogram JSON presets** (`Ideogram4 Architect v4.md`, `Ideogram_Prompt.md`): `split_output`
  OFF (the `[POSITIVE]/[NEGATIVE]` marker contract would corrupt the JSON) but thinking ON
  (structured generation + spatial bbox arithmetic is the one task where a scratchpad
  measurably helps). This is the opposite of the node's general "thinking off" default.
- `Ideogram4 Architect v4.md` has its own "MULTI-SUBJECT & INTERACTION" block (one element per
  person, SEPARATED vs ENTANGLED layout, contact point as its own element, identity-contrast
  axes). It is a deliberately different mechanism from the prose INTERACTION block — don't port
  the prose wording into it.

## Node-side behaviour that affects presets
- The canvas/aspect block is **AR-only by explicit reversal**: just
  `CANVAS FORMAT: <ratio> <orientation> (WxH)`. An earlier version injected per-aspect
  framing/lens/"avoid" language that fought the user's prompt; it was stripped.
  `_classify_canvas_shape` / `_CANVAS_PROFILES` are dead code — don't resurrect the injected
  framing without Peti asking.
- The prose **INTERACTION & CONTACT** block is gated: it fires only for prose prompts when ≥2
  subjects touch or contact an object. If a two-person scene looks wrong, check whether the
  block actually fired before assuming a wording problem.
- **Bbox routing:** Grok places boxes accurately; Gemini / Gemma / Qwen3-VL do not (regardless
  of y-first format correctness). Route spatial / bbox-heavy generation to Grok specifically.
  Bbox placement accuracy and bbox format-honoring are two separate findings — don't conflate.
- SAM boxes are irrelevant to Ideogram t2i and largely irrelevant to i2i (the LLM ignores
  injected boxes). The SAM/bbox input workflow was removed from both LLM nodes.

## Ownership
Codex's clone owns most `prompts/*.md` edits — see `llm-prompt-git-sync`.
