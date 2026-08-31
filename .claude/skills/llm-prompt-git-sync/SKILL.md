---
name: llm-prompt-git-sync
description: Use when committing or pushing the LLM_Prompt repo, reconciling it with origin, or investigating whether origin history diverged. Two separate clones push to origin/main.
---

# LLM_Prompt git sync

This repo has **two clones that both push to `origin/main`**:

- **This clone** — `D:\Claude\comfy-nodes\LLM_Prompt`.
- **Codex's clone** — push source is `D:\Codex_ComfyUI\.publish\LLM_Prompt`. The plain
  `D:\Codex_ComfyUI\LLM_Prompt` is scratch; ignore it for sync checks.

Codex owns most `prompts/*.md` edits.

## Rules
1. **Always `git fetch origin` and reconcile before committing or pushing.** Origin has been
   force-rewritten in the past (a stale local backup clone with 90 unrecognised commits was
   found and deleted 2026-07-04).
2. **Never force-push.** It would clobber the other clone's work.
3. After pushing, bring `D:\Codex_ComfyUI\LLM_Prompt` up to date too — but first verify it has
   no uncommitted WIP before overwriting it.

## Deploying to the live install
The E: copy (`E:\ComfyUI-Easy-Install\ComfyUI\custom_nodes\LLM_Prompt`) is a manual copy.
Use the `comfy-sync` skill to deploy after edits.
