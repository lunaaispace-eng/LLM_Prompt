
"""LLM Prompt â€” ComfyUI node for prompt generation via GGUF vision models.

Built on the proven QwenVL-Mod GGUF inference code. Adds:
  - System prompt presets from .md files in prompts/ folder
  - User prompt (multiline text input)
  - Style input (STRING from external nodes)
  - Width/height aspect ratio auto-detection
  - Output format toggle (text/json)
"""

import base64
import gc
import inspect
import logging
import os
import re
import time
from io import BytesIO
from pathlib import Path

import numpy as np
import torch
from llama_cpp import Llama
from PIL import Image

import folder_paths
from comfy_api.latest import io

# Bundled output cleaner â€” no external dependencies
from .output_cleaner import OutputCleanConfig, clean_model_output, normalize_prompt_separator, split_positive_negative

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NODE_DIR = Path(__file__).parent
PROMPTS_DIR = NODE_DIR / "prompts"

# No-think ChatML template for Qwen (text-only). This replicates the manual
# LM Studio template edit that reliably disables thinking: it pre-fills an EMPTY
# <think></think> block on the assistant turn, so the model continues straight
# to the answer with no reasoning. We pass this to llama-cpp ourselves when
# disable_thinking is on, so thinking-off works regardless of whether the GGUF's
# embedded template exposes `enable_thinking` or whether chat_template_kwargs is
# supported by the installed llama-cpp-python build. Qwen uses ChatML tokens
# (<|im_start|> / <|im_end|>); content is plain text on this path (no vision).
_QWEN_NO_THINK_CHATML = (
    "{%- for message in messages %}"
    "{{- '<|im_start|>' + message['role'] + '\\n' + message['content'] + '<|im_end|>\\n' }}"
    "{%- endfor %}"
    "{%- if add_generation_prompt %}"
    "{{- '<|im_start|>assistant\\n<think>\\n\\n</think>\\n\\n' }}"
    "{%- endif %}"
)
# Aspect ratio mapping: (w_ratio, h_ratio) -> label
_ASPECT_LABELS = {
    (1, 1):   "1:1 square",
    (4, 5):   "4:5 portrait",
    (5, 4):   "5:4 landscape",
    (3, 4):   "3:4 portrait",
    (4, 3):   "4:3 landscape",
    (2, 3):   "2:3 tall portrait",
    (3, 2):   "3:2 photographic",
    (9, 16):  "9:16 vertical",
    (16, 9):  "16:9 cinematic wide",
    (21, 9):  "21:9 panoramic",
    (9, 21):  "9:21 ultra tall",
}


def _detect_aspect_ratio(width: int, height: int) -> str:
    """Convert pixel dimensions to a composition-intent aspect ratio label.

    Kept for back-compat. New code should call _build_canvas_profile() which
    returns a richer multi-line profile that guides the LLM toward composition,
    framing, depth, and camera choices appropriate for the canvas shape.
    """
    from math import gcd
    g = gcd(width, height)
    w_r, h_r = width // g, height // g

    if (w_r, h_r) in _ASPECT_LABELS:
        return _ASPECT_LABELS[(w_r, h_r)]

    actual = width / height
    best_label = None
    best_diff = float("inf")
    for (wr, hr), label in _ASPECT_LABELS.items():
        diff = abs(actual - wr / hr)
        if diff < best_diff:
            best_diff = diff
            best_label = label

    if best_diff < 0.05:
        return best_label

    if width > height:
        return f"{w_r}:{h_r} landscape"
    elif height > width:
        return f"{w_r}:{h_r} portrait"
    return f"{w_r}:{h_r} square"


def _classify_canvas_shape(width: int, height: int) -> str:
    """Bucket the canvas into a shape category that drives composition rules.

    Buckets (by aspect ratio = width / height):
      ultra_tall   : <= 0.50    (9:21 and taller)
      tall         : 0.51-0.70  (9:16, 2:3, 3:4 vertical)
      portrait     : 0.71-0.95  (4:5, slight portrait)
      square       : 0.95-1.05  (1:1 and near-square)
      landscape    : 1.05-1.40  (5:4, 4:3, 3:2 photographic)
      cinematic    : 1.41-1.90  (16:9 and similar widescreen)
      ultra_wide   : >= 1.91    (21:9, 32:9, anamorphic)
    """
    if width <= 0 or height <= 0:
        return "unknown"
    ratio = width / height
    if ratio <= 0.50:
        return "ultra_tall"
    if ratio <= 0.70:
        return "tall"
    if ratio <= 0.95:
        return "portrait"
    if ratio <= 1.05:
        return "square"
    if ratio <= 1.40:
        return "landscape"
    if ratio <= 1.90:
        return "cinematic"
    return "ultra_wide"


# Composition profiles per shape — model-independent guidance.
# These describe what to DO with the canvas shape, not what to do for any
# specific generator (SD/SDXL/Flux/Wan/LTX). Detail density is intentionally
# left out because it depends on the target model and is the user's call.
_CANVAS_PROFILES = {
    "ultra_tall": {
        "framing": "extreme vertical — full-body or head-to-toe, subject dominates the column",
        "composition": "strong vertical emphasis, subject centered horizontally, layered vertical depth",
        "camera": "85-135mm portrait lens feel, shallow depth of field, compressed perspective",
        "depth": "shallow — clean separation between subject and background",
        "avoid": "panoramic vistas, wide horizontal sweeps, group compositions, environmental establishing shots",
    },
    "tall": {
        "framing": "half-body to full-body portrait, subject takes most of the frame height",
        "composition": "vertical emphasis, subject anchored center or rule-of-thirds vertical",
        "camera": "85mm portrait lens feel, shallow to medium depth of field",
        "depth": "shallow — background softly blurred, subject in sharp focus",
        "avoid": "wide environmental shots, panoramic landscapes, lateral camera motion",
    },
    "portrait": {
        "framing": "close-up to medium portrait, subject prominent with limited environment",
        "composition": "subject-forward, modest vertical bias",
        "camera": "50-85mm normal-to-portrait lens feel, moderate depth of field",
        "depth": "moderate — subject sharp, environment softened but recognizable",
        "avoid": "wide vistas, complex multi-subject scenes",
    },
    "square": {
        "framing": "tight centered subject, minimal environment, headshot or product-style framing",
        "composition": "centered, symmetrical balance, single focal point",
        "camera": "50-85mm normal lens feel, controlled depth",
        "depth": "moderate to shallow — subject is the entire focus",
        "avoid": "off-center subjects, panoramic compositions, sprawling environments",
    },
    "landscape": {
        "framing": "medium shot, subject within scene, room for environmental context",
        "composition": "rule-of-thirds, subject can be centered or offset, balanced horizontal flow",
        "camera": "35-50mm normal lens feel, moderate depth of field",
        "depth": "moderate — subject and environment both rendered with clarity",
        "avoid": "extreme verticality, towering compositions, claustrophobic framing",
    },
    "cinematic": {
        "framing": "medium to wide shot, subject within environment, cinematic establishing feel",
        "composition": "rule-of-thirds horizontal, leading lines, layered foreground-midground-background",
        "camera": "24-35mm wide lens feel, deeper depth of field, slight perspective compression",
        "depth": "deep to moderate — environment matters as much as subject",
        "avoid": "tight vertical framing, towering portrait composition, single-element close-ups",
    },
    "ultra_wide": {
        "framing": "wide environmental shot, panoramic sweep, subject occupies a portion of the horizontal expanse",
        "composition": "strong horizontal flow, panoramic balance, lateral leading lines, anamorphic feel",
        "camera": "21-24mm wide lens or anamorphic 2.39:1 cinematic, deep depth of field",
        "depth": "deep — full environmental rendering, foreground to far background",
        "avoid": "vertical subject emphasis, head-to-toe portraits, tight close-ups, centered single-subject framing",
    },
}


def _build_canvas_profile(width: int, height: int) -> str:
    """Build a rich composition profile string from canvas dimensions.

    Returns a multi-line block suitable for injecting into the LLM user prompt.
    Pure aspect-ratio-driven — no assumptions about target diffusion model or
    detail density (those are user-controlled via the user_prompt text).
    """
    if width <= 0 or height <= 0:
        return ""

    label = _detect_aspect_ratio(width, height)
    shape = _classify_canvas_shape(width, height)
    profile = _CANVAS_PROFILES.get(shape)
    if not profile:
        return f"CANVAS FORMAT:\n{label} ({width}x{height})"

    return (
        f"CANVAS FORMAT:\n"
        f"- Aspect: {label} ({width}x{height})\n"
        f"- Framing: {profile['framing']}\n"
        f"- Composition: {profile['composition']}\n"
        f"- Camera: {profile['camera']}\n"
        f"- Depth: {profile['depth']}\n"
        f"- Avoid: {profile['avoid']}"
    )

# ---------------------------------------------------------------------------
# System prompt loading from .md files
# ---------------------------------------------------------------------------

def load_system_prompts() -> dict[str, str]:
    """Load all .md files from prompts/ folder.

    Each file becomes a preset. Optional YAML frontmatter (between --- markers)
    can set: title: Display Name Override
    """
    prompts = {}
    if not PROMPTS_DIR.exists():
        PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        return prompts

    for md_file in sorted(PROMPTS_DIR.glob("*.md")):
        try:
            text = md_file.read_text(encoding="utf-8").strip()
        except Exception:
            continue

        if not text:
            continue

        title = md_file.stem.replace("_", " ").title()
        content = text

        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
        if match:
            try:
                import yaml
                meta = yaml.safe_load(match.group(1))
                if isinstance(meta, dict):
                    title = meta.get("title", title)
                content = match.group(2).strip()
            except Exception:
                content = text

        if content:
            prompts[title] = content

    return prompts


SYSTEM_PROMPTS = load_system_prompts()

# ---------------------------------------------------------------------------
# Model folder scanner â€” replaces static gguf_models.json catalog
# ---------------------------------------------------------------------------

# LLM model folder: ComfyUI/models/LLM
# Scans recursively for .gguf files, auto-detects mmproj companions.
# mmproj detection: any .gguf file in the same folder whose name contains "mmproj".

def _get_llm_folders() -> list[Path]:
    """Return all registered LLM folders from ComfyUI folder_paths.

    Covers multiple registered paths (e.g. two installs pointing to different drives)
    and falls back to models/LLM if nothing is registered.
    """
    folders: list[Path] = []
    try:
        if hasattr(folder_paths, "get_folder_paths"):
            for key in ("llm", "LLM", "llm_gguf", "LLM_gguf"):
                for d in folder_paths.get_folder_paths(key):
                    p = Path(d)
                    if p not in folders:
                        folders.append(p)
    except Exception:
        pass
    if not folders:
        folders.append(Path(folder_paths.models_dir) / "LLM")
    return folders


_LAST_SCAN_SIGNATURE: tuple | None = None


def _scan_llm_folder() -> dict[str, dict]:
    """Scan all registered LLM folders recursively and return a dict of display_name -> model info.

    For each .gguf found that is NOT an mmproj file:
      - display_name = "<top-level-folder>/<filename>" so ComfyUI groups models
        into one collapsible folder per source (bare filename if in the LLM root)
      - full_path    = absolute path to the model file
      - mmproj_path  = absolute path to companion mmproj if found in same folder, else None

    Files inside hidden / .cache directories are skipped.

    mmproj detection rules (in order):
      1. Any .gguf in the same folder whose name contains "mmproj" (case-insensitive)
      2. If multiple mmproj files exist, prefer the one whose name most closely matches
         the model file name (longest common prefix).
    """
    llm_folders = _get_llm_folders()
    existing = [f for f in llm_folders if f.exists()]
    if not existing:
        print(f"[LLM_Prompt] No LLM folders found. Checked: {[str(f) for f in llm_folders]}")
        return {}

    models: dict[str, dict] = {}
    seen_names: set[str] = set()

    # Walk all registered folders, keeping each file's root so we can derive its
    # top-level subfolder for clean display grouping.
    all_ggufs: list[tuple[Path, Path]] = []
    for llm_folder in existing:
        for gguf_file in sorted(llm_folder.rglob("*.gguf")):
            all_ggufs.append((llm_folder, gguf_file))

    for llm_root, gguf_file in all_ggufs:
        name_lower = gguf_file.name.lower()

        # Skip mmproj files â€” they are companions, not models
        if "mmproj" in name_lower:
            continue

        # Path relative to the LLM root. Skip anything inside hidden / .cache
        # directories (HuggingFace download caches, etc.) so they don't pollute
        # the dropdown.
        try:
            rel = gguf_file.relative_to(llm_root)
        except ValueError:
            rel = Path(gguf_file.name)
        if any(part.startswith(".") for part in rel.parts):
            continue

        # Find mmproj companions in the same folder
        folder = gguf_file.parent
        mmproj_candidates = [
            f for f in folder.glob("*.gguf")
            if "mmproj" in f.name.lower()
        ]

        # Detect text-only models that should NOT be paired with mmproj.
        # SuperGemma fine-tunes drop the vision encoder weights, so auto-pairing
        # a Gemma 4 mmproj from the same folder causes the projector to load
        # against mismatched weights and hang.
        #
        # Other uncensored Gemma 4 variants (e.g. "heretic" abliterated) DO
        # support vision — only their refusal direction is removed. The
        # discriminator is the "supergemma" name, not just "uncensored".
        is_text_only = (
            "supergemma" in name_lower
            and "vision" not in name_lower
        )

        mmproj_path = None
        if mmproj_candidates and not is_text_only:
            if len(mmproj_candidates) == 1:
                mmproj_path = mmproj_candidates[0]
            else:
                # Pick the one with the longest common prefix with the model filename
                model_stem = gguf_file.stem.lower()
                best = max(
                    mmproj_candidates,
                    key=lambda f: len(
                        next(
                            (model_stem[:i] for i in range(len(model_stem), 0, -1)
                             if f.stem.lower().startswith(model_stem[:i])),
                            ""
                        )
                    )
                )
                mmproj_path = best

        # Build display name: group by the TOP-LEVEL subfolder under the LLM root
        # so ComfyUI renders one clean, collapsible folder per source. Deep HF
        # nesting (e.g. mradermacher/Repo-GGUF/model.gguf) collapses to the top
        # folder (mradermacher/model.gguf). Files directly in the LLM root show
        # as a bare filename.
        if len(rel.parts) > 1:
            top = rel.parts[0]
            display = f"{top}/{gguf_file.name}"
        else:
            top = ""
            display = gguf_file.name
        # Rare collision (same filename within the same top folder): also fold in
        # the immediate parent dir to keep the key unique.
        if display in seen_names:
            disamb = f"{gguf_file.parent.name}/{gguf_file.name}"
            display = f"{top}/{disamb}" if top else disamb
        seen_names.add(display)

        models[display] = {
            "full_path": str(gguf_file),
            "mmproj_path": str(mmproj_path) if mmproj_path else None,
        }

    # ComfyUI calls INPUT_TYPES multiple times per graph validation, so the scan
    # runs repeatedly. Only print when the result set actually changes — avoids
    # spamming "Found N model(s)" five times per workflow run.
    global _LAST_SCAN_SIGNATURE
    signature = (len(models), tuple(sorted(models.keys())), tuple(str(f) for f in existing))
    if signature != _LAST_SCAN_SIGNATURE:
        _LAST_SCAN_SIGNATURE = signature
        if models:
            print(f"[LLM_Prompt] Found {len(models)} model(s) across {len(existing)} LLM folder(s): {[str(f) for f in existing]}")
        else:
            print(f"[LLM_Prompt] No GGUF models found. Scanned: {[str(f) for f in existing]}")

    return models


# Scanned at startup â€” refreshed on each INPUT_TYPES call so new models appear
# without restarting ComfyUI.
_SCANNED_MODELS: dict[str, dict] = {}


def _refresh_model_list() -> dict[str, dict]:
    global _SCANNED_MODELS
    _SCANNED_MODELS = _scan_llm_folder()
    return _SCANNED_MODELS


_refresh_model_list()

# ---------------------------------------------------------------------------
# Helpers (same as QwenVL-Mod)
# ---------------------------------------------------------------------------

def _filter_kwargs_for_callable(fn, kwargs: dict) -> dict:
    try:
        sig = inspect.signature(fn)
    except Exception:
        return dict(kwargs)
    params = list(sig.parameters.values())
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params):
        return dict(kwargs)
    allowed = {
        p.name for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return {k: v for k, v in kwargs.items() if k in allowed}


def _tensor_to_base64_png(tensor) -> str | None:
    if tensor is None:
        return None
    if tensor.ndim == 4:
        tensor = tensor[0]
    array = (tensor * 255).clamp(0, 255).to(torch.uint8).cpu().numpy()
    pil_img = Image.fromarray(array, mode="RGB")
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _sample_video_frames(video, frame_count: int):
    if video is None:
        return []
    if video.ndim != 4:
        return [video]
    total = int(video.shape[0])
    frame_count = max(int(frame_count), 1)
    if total <= frame_count:
        return [video[i] for i in range(total)]
    idx = np.linspace(0, total - 1, frame_count, dtype=int)
    return [video[i] for i in idx]


def _pick_device(device_choice: str) -> str:
    if device_choice == "auto":
        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
        return "cpu"
    if device_choice.startswith("cuda") and torch.cuda.is_available():
        return "cuda"
    if device_choice == "mps" and getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


# ---------------------------------------------------------------------------
# Model resolution â€” from scanned folder list
# ---------------------------------------------------------------------------

def _resolve_model_paths(model_name: str) -> tuple[Path, Path | None]:
    """Return (model_path, mmproj_path | None) for a display name from the scanned list."""
    entry = _SCANNED_MODELS.get(model_name)
    # Backward-compat: older workflows saved the bare filename (before the
    # top-folder grouping). If the exact display key isn't found, fall back to
    # matching by basename so those workflows still resolve.
    if not entry:
        base = model_name.split("/")[-1]
        for disp, info in _SCANNED_MODELS.items():
            if disp.split("/")[-1] == base or Path(info["full_path"]).name == base:
                entry = info
                break
    if not entry:
        raise FileNotFoundError(
            f"[LLM_Prompt] Model not found in scan: {model_name}. "
            f"Check that the file exists in your LLM folder."
        )
    model_path = Path(entry["full_path"])
    mmproj_path = Path(entry["mmproj_path"]) if entry.get("mmproj_path") else None
    return model_path, mmproj_path


def _resolve_model_settings(name_lower: str) -> dict | None:
    """Return the correct sampling settings for a known model family, or None.

    Source of truth: the official LM Studio model.yaml recommendations
    (hub/models/.../model.yaml -> config.operation.fields). Values target
    NON-thinking prompt generation (thinking is disabled separately, since it
    is never useful for prompt writing and wastes tokens).

    This is applied SERVER-SIDE in generate() so the right values reach the
    model regardless of the frontend JS preset's timing (which only fires on
    manual dropdown clicks, not on workflow load / node creation / refresh).

    Detection is by the model's display name (filename), which is reliable and
    available before load — unlike self.loaded_model_name_lower which lags one
    run behind on a model switch.
    """
    n = name_lower or ""

    # Gemma 3 / 4 (all sizes incl. e2b/e4b/26b-a4b/31b):
    #   YAML: temperature 1.0, top_k 64, top_p 0.95. No min_p / presence / rep.
    if "gemma" in n:
        return {
            "temperature": 1.0, "top_p": 0.95, "top_k": 64,
            "min_p": 0.0, "presence_penalty": 0.0, "repetition_penalty": 1.0,
        }

    # Qwen 3-VL / 2.5-VL (vision): these run through llama-cpp's vision handler,
    # which does NOT reliably apply presence_penalty — so the aggressive Qwen
    # text preset (min_p 0, presence 1.5) left nothing to stop repetition and
    # abliterated VL builds spiralled into endless "no X, no Y" loops. Use a
    # loop-resistant profile instead: a min_p floor plus a real repeat_penalty
    # (the lever that actually works on the vision path), and no presence_penalty.
    if "qwen" in n and "vl" in n:
        return {
            "temperature": 0.7, "top_p": 0.9, "top_k": 20,
            "min_p": 0.05, "presence_penalty": 0.0, "repetition_penalty": 1.1,
        }

    # Qwen 3.6 MoE "a3b" variant: YAML uses a lower temperature (0.6).
    if "qwen" in n and "a3b" in n:
        return {
            "temperature": 0.6, "top_p": 0.95, "top_k": 20,
            "min_p": 0.0, "presence_penalty": 0.0, "repetition_penalty": 1.05,
        }

    # Qwen 3.0 / 3.5 / 3.6 (dense, text):
    #   Unsloth non-thinking recommendation — temp 0.7, top_p 0.8, top_k 20,
    #   min_p 0, presence_penalty 1.5. Add a mild repeat_penalty (1.05) as a
    #   loop guard since presence_penalty is not always honored by the build.
    if "qwen" in n:
        return {
            "temperature": 0.7, "top_p": 0.8, "top_k": 20,
            "min_p": 0.0, "presence_penalty": 1.5, "repetition_penalty": 1.05,
        }

    return None


def _looks_like_reasoning(text: str) -> bool:
    """Detect if model output is reasoning/planning instead of a final prompt.

    Catches: numbered lists, markdown bold headers, bullet analysis,
    first-person planning, Qwen 3.5 raw thinking headers, and known
    terminator artifacts.
    """
    if not text:
        return False

    # Qwen 3.5 raw thinking headers â€” emitted without <think> tags
    # e.g. "Thinking Process:", "1. **Analyze the Inputs:**"
    if re.search(r"(?im)^\s*thinking\s+process\s*:", text):
        return True
    if re.search(r"(?im)^\s*analysis\s*:", text):
        return True
    if re.search(r"(?im)^\s*draft\s+(construction|output)\s*:", text):
        return True
    if re.search(r"(?im)^\s*final\s+(review|check|polish|draft)\s*:", text):
        return True
    if re.search(r"(?im)^\s*internal\s+section", text):
        return True

    # Qwen 3.5 reasoning self-talk patterns
    if re.search(r"(?im)^\s*\d+\.\s+\*{0,2}(analyze|deconstruct|draft|refine|merge|review|check|polish|verify)\b", text):
        return True

    # Known Qwen 3.5 thinking terminator artifact leaking through
    # Appears as a bare "cw" or "cw\n" at the very end of output
    if re.search(r"(?m)\bcw\s*$", text.strip()):
        return True

    # Standard planning first-person — "I should", "I need to", etc.
    # "I will" alone is too broad (cinematic prose: "The camera will pan...")
    # so require "I will" at a sentence boundary or line start.
    if re.search(r"(?i)\b(i\s+(should|need\s+to|must|am\s+going\s+to|have\s+to))\b", text):
        return True
    # Reasoning self-correction openers — only strong planning words, not "first/next/then"
    # (those appear constantly in cinematic prose: "First, the camera...", "Then the light...")
    if re.search(r"(?im)^\s*(okay[,.:]|wait[,.:]|let me\b)", text):
        return True

    # Numbered analysis steps — only flag if the numbered items look like analysis tasks,
    # not just any sentence starting with a number (scene lists, steps in a description).
    # Pattern: "1. **Verb**" or "1. Verb the ..." where verb is an analysis word.
    numbered = len(re.findall(
        r"(?m)^\s*\d+\.\s+\*{0,2}(analyze|deconstruct|draft|refine|merge|review|check|polish|verify|assess|consider|plan)\b",
        text, re.IGNORECASE
    ))
    if numbered >= 2:
        return True

    # Markdown bold headers that look like structured analysis sections
    # e.g. "**Subject:**", "**Lighting:**", "**Camera:**" — three or more in a row
    # Two bold headers alone is too sensitive (a prompt might legitimately bold two terms)
    bold_headers = len(re.findall(r"(?m)\*{1,2}[A-Za-z][^*]+\*{1,2}\s*:", text))
    if bold_headers >= 3:
        return True

    # Bullet point analysis — three or more bullets strongly suggests structured reasoning
    bullets = len(re.findall(r"(?m)^\s*[-*]\s+", text))
    if bullets >= 3:
        return True

    return False


def _strip_think_blocks(text: str) -> str:
    """Strip thinking content from model output.

    Mirrors VRGDG's approach: split on </think> and take whatever comes after.
    Bulletproof against unclosed tags, malformed think blocks, and post-think
    notes. Then peel off labeled draft markers like "Final Prompt:" if present.
    """
    cleaned = str(text or "")

    # Strip explicit reasoning headers at the start (some models emit them raw, no tags)
    cleaned = re.sub(r"(?is)^\s*thinking process\s*:\s*", "", cleaned)
    cleaned = re.sub(r"(?is)^\s*reasoning\s*:\s*", "", cleaned)
    cleaned = re.sub(r"(?is)^\s*analysis\s*:\s*", "", cleaned)
    cleaned = re.sub(r"(?is)^\s*\*+\s*thinking process\s*:?\s*\*+\s*", "", cleaned)
    cleaned = re.sub(r"(?is)^\s*\*+\s*reasoning\s*:?\s*\*+\s*", "", cleaned)
    cleaned = re.sub(r"(?is)^\s*\*+\s*analysis\s*:?\s*\*+\s*", "", cleaned)

    # Strip leading single-char artifacts (",", ".", ";", "|") from chat template
    # control tokens that decoded as a literal punctuation mark at output start.
    # These leak from Qwen and Gemma chat templates on some GGUF builds.
    cleaned = re.sub(r"^[,;.|]\s*", "", cleaned)

    # Strip analytical meta-commentary that small models append. Pattern: a
    # numbered bold header containing an analytical keyword (Analyze, Review,
    # Check, Final, Format, Constraint, Verify, Polish, Refine), and everything
    # after it. Examples we want to strip:
    #   "4. **Final Review against Constraints:**\n  * Format: ..."
    #   "1. **Analyze the Request:**\nUser Prompt: ..."
    # The analytical keyword is the discriminator — natural prompt content very
    # rarely uses numbered+bold+analytical-word formatting.
    analytical_pattern = (
        r"\d+\.\s+\*\*[^*\n]*"
        r"(?:analyz|review|check|constraint|format|verify|polish|refine|final|breakdown|deconstruct)"
        r"[^*\n]*\*\*.*$"
    )
    # Strip the analytical block when it follows a blank line (trailing meta)
    cleaned = re.sub(
        r"\n\s*\n\s*" + analytical_pattern,
        "", cleaned, flags=re.IGNORECASE | re.DOTALL,
    )
    # Or when it starts at the very beginning of output (whole output is meta)
    cleaned = re.sub(
        r"^\s*" + analytical_pattern,
        "", cleaned, flags=re.IGNORECASE | re.DOTALL,
    )

    # The key trick: if </think> exists anywhere, take only what comes after it.
    # Handles unclosed tags, nested tags, anything.
    if re.search(r"</think>", cleaned, flags=re.IGNORECASE):
        cleaned = re.split(r"</think>", cleaned, flags=re.IGNORECASE)[-1]
    # Mop up any lingering <think>...</think> pairs and stray <think> openers
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<think>", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()

    # Gemma 4 uses a different thinking format: <|channel>thought\n...<channel|>
    # Same trick — split on the closing marker, take what's after. Then handle
    # tokenizer-decode artifacts: <|channel>thought sometimes decodes as ",se_thought"
    # or similar garbled bytes when the special tokens aren't registered properly.
    if re.search(r"<channel\|?>", cleaned, flags=re.IGNORECASE):
        cleaned = re.split(r"<channel\|?>", cleaned, flags=re.IGNORECASE)[-1]
    # Strip any leading thought-block opener that may have decoded as garbage bytes
    # before "_thought" (e.g. ",se_thought", "_thought", "|>thought"). The underscore
    # before "thought" is the discriminator — natural prose never has "_thought".
    # Allow up to 20 chars of garbage before it, and don't require any separator after
    # — the leaked output can be glued directly to the next word ("_thoughtbeautiful").
    # The leading "_" before "thought" is the discriminator — natural prose never has
    # "_thought" so this is safe to match without a word boundary, even when glued to
    # the next word ("_thoughtbeautiful" → "beautiful").
    cleaned = re.sub(r"^[^\n]{0,20}?_thought\s*\n?", "", cleaned, flags=re.IGNORECASE)
    # Also catch bare "thought\n" at the very start (when garbage prefix got stripped earlier)
    cleaned = re.sub(r"^thought\s*\n", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()

    # If the model labels its final answer, extract just that part
    labeled_patterns = [
        r"(?is)(?:\*?\s*final prompt\s*:\*?)(.+)$",
        r"(?is)(?:\*?\s*revised prompt\s*:\*?)(.+)$",
        r"(?is)(?:\*?\s*final plan\s*:\*?)(.+)$",
        r"(?is)(?:\*?\s*draft\s*:\*?)(.+)$",
    ]
    for pat in labeled_patterns:
        m = re.search(pat, cleaned, flags=re.IGNORECASE)
        if m:
            candidate = m.group(1).strip()
            if candidate:
                cleaned = candidate
                break

    # Gemma 4 / SuperGemma control token artifacts
    cleaned = re.sub(r"_?\s*<\|?channel\|?>\s*(?:thought|analysis|reasoning)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</?thought>", "", cleaned, flags=re.IGNORECASE)
    # SuperGemma loop runaway tokens
    for _marker in ("thought_turn", "turn_turn"):
        if _marker in cleaned:
            cleaned = cleaned.split(_marker)[0]
    # SuperGemma language prefix artifacts
    cleaned = re.sub(r"^나의 답변은 다음과 같습니다\.\s*", "", cleaned)
    cleaned = re.sub(r"^回答[:：]\s*", "", cleaned)
    cleaned = re.sub(r"^(?:Assistant|Answer):\s*", "", cleaned, flags=re.IGNORECASE)

    return cleaned.strip()


# ---------------------------------------------------------------------------
# The Node â€” built on QwenVL-Mod's proven inference code
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Media encoding helpers (image / video / audio -> OpenAI-style content dicts)
# ---------------------------------------------------------------------------

ESTIMATED_TOKENS_PER_IMAGE = 258   # approx visual-token cost per frame
MAX_VIDEO_FRAMES = 30
MAX_AUDIO_DURATION_SECONDS = 60


def _image_tensor_to_content(image: torch.Tensor) -> dict:
    """A single ComfyUI image tensor -> an OpenAI-compatible image_url dict.

    Encodes JPEG quality 95 (much smaller base64 than PNG; the vision encoder
    downsamples anyway, so lossless PNG just wastes context bytes + encode time).
    Accepts [H,W,3] or [N,H,W,3] (first frame used for the 4-D case).
    """
    if image.ndim == 4:
        image = image[0]
    arr = (image.cpu().numpy() * 255).clip(0, 255).astype(np.uint8)
    pil = Image.fromarray(arr, mode="RGB")
    buf = BytesIO()
    pil.save(buf, format="JPEG", quality=95)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


def _video_tensor_to_contents(video: torch.Tensor, target_fps: float = 1.0,
                              source_fps: float = 30.0, n_ctx: int = 8192) -> list[dict]:
    """A [F,H,W,3] video tensor -> list of image_url dicts, FPS-subsampled.

    ComfyUI image batches carry no FPS metadata, so source_fps is assumed 30.
    Frame count is derived from duration*target_fps, capped at MAX_VIDEO_FRAMES,
    and further reduced if it would blow past ~70% of the context window
    (each frame costs ~ESTIMATED_TOKENS_PER_IMAGE tokens).
    """
    total = int(video.shape[0])
    if total == 0:
        return []
    duration = total / source_fps
    count = min(max(1, int(duration * target_fps)), MAX_VIDEO_FRAMES, total)
    estimated = count * ESTIMATED_TOKENS_PER_IMAGE
    if estimated > n_ctx * 0.7:
        old = count
        count = max(1, min(old, int((n_ctx * 0.5) / ESTIMATED_TOKENS_PER_IMAGE)))
        logger.warning("[LLM_Prompt] Video frames reduced %d -> %d to fit context (%d tokens)",
                       old, count, n_ctx)
    idx = np.linspace(0, total - 1, count, dtype=int)
    logger.info("[LLM_Prompt] Video: %.1fs, %d frames total, sampling %d at %.1f fps",
                duration, total, count, target_fps)
    return [_image_tensor_to_content(video[i]) for i in idx]


def _audio_to_content(audio: dict) -> dict:
    """A ComfyUI AUDIO dict -> an OpenAI-compatible audio_url dict.

    Resampled to 16 kHz mono WAV (the format the Gemma-4 audio projector wants).
    Raises ValueError past MAX_AUDIO_DURATION_SECONDS. soundfile / torchaudio are
    imported lazily so they are only required when audio is actually connected.
    """
    import soundfile as sf
    import torchaudio

    waveform = audio["waveform"]
    sample_rate = int(audio["sample_rate"])
    if waveform.ndim == 3:           # [B, C, T] -> [C, T]
        waveform = waveform.squeeze(0)
    if waveform.shape[0] > 1:        # mixdown to mono
        waveform = waveform.mean(dim=0, keepdim=True)
    if sample_rate != 16000:
        waveform = torchaudio.transforms.Resample(sample_rate, 16000)(waveform)

    duration = waveform.shape[-1] / 16000
    if duration > MAX_AUDIO_DURATION_SECONDS:
        raise ValueError(
            f"Audio duration ({duration:.1f}s) exceeds the {MAX_AUDIO_DURATION_SECONDS}s "
            "maximum. Trim the audio input."
        )

    audio_np = waveform.squeeze(0).cpu().numpy()
    buf = BytesIO()
    sf.write(buf, audio_np, 16000, format="WAV")
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return {"type": "audio_url", "audio_url": {"url": f"data:audio/wav;base64,{b64}"}}


def _comfy_unload_caches() -> None:
    """Ask ComfyUI's model manager to release VRAM so our unload cooperates with
    the rest of the graph (mirrors the Duffy nodes' unload integration)."""
    try:
        import comfy.model_management as mm
        mm.unload_all_models()
        mm.soft_empty_cache()
    except Exception as e:  # pragma: no cover - depends on ComfyUI runtime
        logger.debug("[LLM_Prompt] ComfyUI cache cleanup skipped: %s", e)


# ---------------------------------------------------------------------------
# Model-family adapters - encapsulate vision-handler choice + thinking control
# ---------------------------------------------------------------------------
# Each adapter answers, for its model family, "which llama-cpp vision handler
# classes do I try, in order?". The per-handler constructor differences (which
# thinking kwarg, whether image-token budgets / vision-id are accepted) live in
# _build_vision_handler(), keyed on the handler class name - because those kwargs
# are a property of the handler, not the family. The node stays generic and just
# dispatches by model filename via pick_adapter(). This is the single-node
# equivalent of the three separate Duffy nodes, minus the duplication and the
# "wrong handler for wrong model" hazard.


def _import_handler(name: str):
    """Return a llama-cpp chat-handler class by name, or None if unavailable."""
    try:
        import llama_cpp.llama_chat_format as fmt
        return getattr(fmt, name, None)
    except Exception:
        return None


def _build_vision_handler(name: str, mmproj_path, disable_thinking: bool,
                          preserve_thinking: bool, image_min_tokens: int,
                          image_max_tokens: int):
    """Construct one vision chat handler by class name with the right kwargs.

    Returns the handler instance, or None if the class is missing or construction
    fails (e.g. the latest Gemma-4 QAT mmproj that breaks Gemma4ChatHandler) so
    the caller can fall through to the next candidate.
    """
    cls = _import_handler(name)
    if cls is None:
        return None

    kwargs = {"clip_model_path": str(mmproj_path), "verbose": False}
    if name == "Gemma4ChatHandler":
        kwargs["enable_thinking"] = not disable_thinking
    elif name == "Qwen35ChatHandler":
        kwargs.update(
            enable_thinking=not disable_thinking,
            preserve_thinking=preserve_thinking,
            add_vision_id=True,
            image_min_tokens=image_min_tokens,
            image_max_tokens=image_max_tokens,
        )
    elif name in ("Qwen3VLChatHandler", "Qwen25VLChatHandler"):
        kwargs.update(
            force_reasoning=not disable_thinking,
            add_vision_id=True,
            image_min_tokens=image_min_tokens,
            image_max_tokens=image_max_tokens,
        )
    # Gemma3ChatHandler / Llava16ChatHandler take neither thinking nor image-token
    # kwargs -> base kwargs only.

    try:
        return cls(**kwargs)
    except Exception as e:
        logger.warning("[LLM_Prompt] %s construction failed: %s", name, e)
        return None


class ModelAdapter:
    """Base adapter: generic Qwen-VL-style fallback chain, no audio."""
    family = "generic"
    supports_audio = False

    @staticmethod
    def matches(name_lower: str) -> bool:
        return False

    def candidates(self, name_lower: str) -> list[str]:
        return ["Qwen3VLChatHandler", "Qwen25VLChatHandler", "Llava16ChatHandler"]


class GemmaAdapter(ModelAdapter):
    family = "gemma"
    supports_audio = True   # Gemma-4 mmproj can carry an audio projector

    @staticmethod
    def matches(name_lower: str) -> bool:
        return "gemma" in name_lower and "supergemma" not in name_lower

    def candidates(self, name_lower: str) -> list[str]:
        # Gemma-3 has no Gemma4 handler; newer / unspecified Gemma -> try Gemma4
        # first, then fall back to Gemma3 (covers the QAT-mmproj breakage you hit).
        if "gemma-3" in name_lower or "gemma3" in name_lower or "gemma_3" in name_lower:
            return ["Gemma3ChatHandler", "Llava16ChatHandler"]
        return ["Gemma4ChatHandler", "Gemma3ChatHandler", "Llava16ChatHandler"]


class Qwen35Adapter(ModelAdapter):
    family = "qwen35"

    @staticmethod
    def matches(name_lower: str) -> bool:
        return "qwen" in name_lower and any(
            t in name_lower for t in
            ("qwen3.5", "qwen3-5", "qwen35", "qwen3.6", "qwen3-6", "qwen36")
        )

    def candidates(self, name_lower: str) -> list[str]:
        return ["Qwen35ChatHandler", "Qwen3VLChatHandler",
                "Qwen25VLChatHandler", "Llava16ChatHandler"]


class Qwen3VLAdapter(ModelAdapter):
    family = "qwen3vl"

    @staticmethod
    def matches(name_lower: str) -> bool:
        return "qwen" in name_lower and "vl" in name_lower

    def candidates(self, name_lower: str) -> list[str]:
        return ["Qwen3VLChatHandler", "Qwen25VLChatHandler", "Llava16ChatHandler"]


# Order matters: most specific families first. Qwen 3.5/3.6 must win over the
# generic Qwen-VL matcher so a "qwen3.5-vl" model gets Qwen35ChatHandler.
_ADAPTERS: list[ModelAdapter] = [
    GemmaAdapter(),
    Qwen35Adapter(),
    Qwen3VLAdapter(),
]


def pick_adapter(name_lower: str) -> ModelAdapter:
    for adapter in _ADAPTERS:
        if adapter.matches(name_lower):
            return adapter
    return ModelAdapter()


# ---------------------------------------------------------------------------
# Inference runner - holds the loaded model (module-level state, since the V3
# node below is stateless / classmethod-based and cannot keep state on self).
# ---------------------------------------------------------------------------

class _LLMRunner:
    """Prompt generation runner using local GGUF VL models.

    Holds the cached llama-cpp model + chat handler between executions. The V3
    LLMPromptNode delegates to a single module-level instance of this class.
    """

    def __init__(self):
        self.llm = None
        self.chat_handler = None
        self.current_signature = None
        self.loaded_model_name_lower = ""

    # ------------------------------------------------------------------
    # VRAM cleanup (same as QwenVL-Mod)
    # ------------------------------------------------------------------

    def clear(self):
        if self.chat_handler is not None:
            try:
                if hasattr(self.chat_handler, 'close'):
                    self.chat_handler.close()
                elif hasattr(self.chat_handler, '__del__'):
                    self.chat_handler.__del__()
            except Exception:
                pass
            self.chat_handler = None

        if self.llm is not None:
            try:
                if hasattr(self.llm, 'close'):
                    self.llm.close()
                del self.llm
            except Exception:
                pass
            self.llm = None

        self.current_signature = None
        self.loaded_model_name_lower = ""
        gc.collect()

        # Let ComfyUI's model manager release its own VRAM too, so unloading here
        # cooperates with the rest of the graph instead of fighting it.
        _comfy_unload_caches()

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()

    # ------------------------------------------------------------------
    # Model loading (same as QwenVL-Mod GGUF)
    # ------------------------------------------------------------------

    def _load_model(self, model_name: str, device: str, n_ctx: int = 32768, n_gpu_layers: int = -1,
                    disable_thinking: bool = True, want_vision: bool = True,
                    preserve_thinking: bool = False, image_min_tokens: int = 1024,
                    image_max_tokens: int = 4096):
        # Resolve paths directly from the scanned folder list â€” no JSON catalog needed
        model_path, mmproj_path = _resolve_model_paths(model_name)

        if not model_path.exists():
            raise FileNotFoundError(f"[LLM_Prompt] Model file missing: {model_path}")
        if mmproj_path and not mmproj_path.exists():
            print(f"[LLM_Prompt] Warning: mmproj not found at {mmproj_path}, running text-only.")
            mmproj_path = None

        m_name_lower_sig = model_path.name.lower()
        is_vl = "vl" in m_name_lower_sig
        # The no-think template targets NON-VL Qwen 3.5/3.6 (Qwen35ChatHandler
        # does not suppress thinking). VL models are EXCLUDED: their vision
        # handler (Qwen3VLChatHandler / Qwen25VLChatHandler) applies
        # force_reasoning=False, which already disables thinking and makes them
        # work well — so we never strip their handler or touch their template.
        is_qwen_35_36 = bool(
            re.search(r"qwen3\.?5|qwen3-5|qwen35|qwen3\.?6|qwen3-6|qwen36", m_name_lower_sig)
        ) and not is_vl

        # Force text-only (when no image) ONLY for non-VL Qwen 3.5/3.6, so the
        # no-think template can engage. EVERY OTHER model keeps its normal
        # loading â€” crucially, VL models keep their vision handler even with no
        # image connected (that's how Qwen-VL works fast and without thinking).
        if not want_vision and is_qwen_35_36 and mmproj_path is not None:
            print("[LLM_Prompt] Qwen 3.5/3.6 text prompt â€” loading text-only so the no-think template engages.")
            mmproj_path = None

        device_kind = _pick_device(device)
        # CPU always 0; on GPU use the user-supplied value (-1 = all layers, N = partial offload)
        effective_gpu_layers = 0 if device_kind == "cpu" else n_gpu_layers
        has_mmproj = mmproj_path is not None

        use_qwen_no_think = disable_thinking and not has_mmproj and is_qwen_35_36

        signature = {
            "model_path": str(model_path),
            "mmproj_path": str(mmproj_path) if has_mmproj else "",
            "n_gpu_layers": effective_gpu_layers,
            "n_ctx": n_ctx,
            "device_kind": device_kind,
            "want_vision": bool(want_vision and mmproj_path is not None),
            # Changing the no-think template requires a reload, so it's part of
            # the load signature.
            "qwen_no_think": use_qwen_no_think,
            # Thinking + image-token budgets are now baked into the vision chat
            # handler at construction time, so changing any of them must reload.
            "disable_thinking": bool(disable_thinking),
            "preserve_thinking": bool(preserve_thinking),
            "image_min_tokens": int(image_min_tokens),
            "image_max_tokens": int(image_max_tokens),
        }
        if self.llm is not None and self.current_signature == signature:
            return

        self.clear()
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            time.sleep(0.1)

        # Load vision handler â€” handler selection order matches JamePeng 0.3.39 API
        # Load the vision handler via the family adapter. The adapter supplies an
        # ordered list of candidate handler classes; we build the first one that
        # succeeds. This replaces the old inline if/elif chain and adds:
        #   - Gemma4ChatHandler, with graceful fallback to Gemma3 for the QAT
        #     mmproj builds that break it,
        #   - handler-level thinking control (enable_thinking / force_reasoning),
        #   - user-controlled image_min/max_tokens.
        self.chat_handler = None
        if has_mmproj:
            adapter = pick_adapter(model_path.name.lower())
            for cand in adapter.candidates(model_path.name.lower()):
                handler = _build_vision_handler(
                    cand, mmproj_path, disable_thinking, preserve_thinking,
                    image_min_tokens, image_max_tokens,
                )
                if handler is not None:
                    self.chat_handler = handler
                    print(
                        f"[LLM_Prompt] Vision handler: {cand} (family={adapter.family}, "
                        f"thinking={'on' if not disable_thinking else 'off'}, "
                        f"img_tokens={image_min_tokens}-{image_max_tokens})"
                    )
                    break

            if self.chat_handler is None:
                # Every candidate failed (missing handler classes, or the latest
                # Gemma-4 QAT mmproj that breaks Gemma4ChatHandler). Fall back to
                # text-only rather than hard-failing, so the node still produces a
                # prompt from the text inputs.
                print(
                    "[LLM_Prompt] Warning: no vision handler could be built for "
                    f"{model_path.name}; running text-only (images will be ignored)."
                )
                has_mmproj = False

        # Load LLM
        llm_kwargs = {
            "model_path": str(model_path),
            "n_ctx": n_ctx,
            "n_gpu_layers": effective_gpu_layers,
            "n_batch": 512,
            # swa_full=True forces a full-size SWA cache. It enables cross-run
            # prompt caching at significant VRAM cost. We do one-shot inference
            # per run and never reuse the cache — so this just wastes VRAM and
            # causes 26B Gemma models to thrash. Default (False) is much lighter.
            # ref: https://github.com/ggml-org/llama.cpp/pull/13194
            "swa_full": False,
            "verbose": False,
            "pool_size": 4194304,
            "top_k": 0,
            "flash_attn": True,
            # chat_template_kwargs does NOT belong here â€” no effect on Llama.__init__.
            # Passed at inference time via create_chat_completion instead.
        }
        if has_mmproj and self.chat_handler is not None:
            # image_min/max_tokens belong on the chat handler, not here.
            llm_kwargs["chat_handler"] = self.chat_handler
        else:
            # Text-only path: set chat_format only for models whose GGUF does NOT embed a
            # usable Jinja2 template, or where the embedded template needs overriding.
            #
            # Qwen 3.5 and Qwen 3.6: DO NOT set chat_format â€” let llama.cpp use the
            # embedded GGUF template. That is the only path where enable_thinking=False
            # (passed at inference time) is actually processed correctly.
            #
            # Gemma 3/4: embedded template works, but "gemma3" format is still needed
            # because older GGUF builds may not embed it.
            m_name_lower = model_path.name.lower()
            if "gemma" in m_name_lower:
                # Try "gemma3" first (newer llama-cpp-python). If unavailable, DO
                # NOT fall back to the legacy "gemma" handler — it is the Gemma 1/2
                # template and is incompatible with Gemma 4's <|channel> / channel
                # thought-block tokens. The model would produce garbage output
                # like ",[]" because the chat structure is wrong.
                #
                # Instead, leave chat_format unset so llama-cpp-python uses the
                # GGUF's embedded Jinja2 template — which is correct for Gemma 4
                # GGUFs built after Google's April 11 chat template fix.
                try:
                    from llama_cpp.llama_chat_format import _CHAT_HANDLERS  # type: ignore
                    if "gemma3" in _CHAT_HANDLERS:
                        llm_kwargs["chat_format"] = "gemma3"
                    # else: no override — embedded GGUF template
                except Exception:
                    pass  # no override — embedded GGUF template
            elif "llama-3" in m_name_lower:
                llm_kwargs["chat_format"] = "llama-3"
            elif "qwen" in m_name_lower:
                # Qwen text-only. When disable_thinking is on, install our custom
                # no-think ChatML template (pre-fills an empty <think></think>),
                # which forces thinking off regardless of what the embedded
                # template does or whether chat_template_kwargs is supported.
                # This replicates the manual LM Studio template edit that works
                # reliably. Fail-safe: any error falls back to the embedded
                # template (current behavior).
                if use_qwen_no_think:
                    try:
                        from llama_cpp.llama_chat_format import Jinja2ChatFormatter
                        formatter = Jinja2ChatFormatter(
                            template=_QWEN_NO_THINK_CHATML,
                            eos_token="<|im_end|>",
                            bos_token="",
                        )
                        self.chat_handler = formatter.to_chat_handler()
                        llm_kwargs["chat_handler"] = self.chat_handler
                        print("[LLM_Prompt] Qwen: using custom NO-THINK ChatML template (thinking hard-disabled).")
                    except Exception as e:
                        print(
                            f"[LLM_Prompt] Could not install no-think template ({e}); "
                            "falling back to embedded template + enable_thinking flag."
                        )
                # else: embedded template (thinking allowed, or controlled via kwargs)

        print(
            f"[LLM_Prompt] Loading: {model_path.name} "
            f"(device={device_kind}, gpu_layers={effective_gpu_layers}, ctx={n_ctx})"
        )

        llm_kwargs_filtered = _filter_kwargs_for_callable(
            getattr(Llama, "__init__", Llama), llm_kwargs
        )

        if has_mmproj and self.chat_handler and "chat_handler" not in llm_kwargs_filtered:
            print("[LLM_Prompt] Warning: Llama() doesn't accept chat_handler. Images will be ignored.")

        self.llm = Llama(**llm_kwargs_filtered)
        self.current_signature = signature
        self.loaded_model_name_lower = model_path.name.lower()
        print(f"[LLM_Prompt] Model loaded: {model_path.name}")

    # ------------------------------------------------------------------
    # Inference (same create_chat_completion as QwenVL-Mod)
    # ------------------------------------------------------------------

    def _invoke(
        self,
        system_prompt: str,
        user_prompt: str,
        media_content: list[dict],
        max_tokens: int,
        temperature: float,
        top_p: float,
        repetition_penalty: float,
        seed: int,
        top_k: int = 30,
        min_p: float = 0.05,
        disable_thinking: bool = True,
        presence_penalty: float = 0.0,
        frequency_penalty: float = 0.0,
        mirostat_mode: int = 0,
        mirostat_tau: float = 5.0,
        mirostat_eta: float = 0.1,
    ) -> str:
        """Run inference.

        media_content is a pre-built list of OpenAI-style content dicts
        (image_url / audio_url), already ordered and labelled by the caller, or
        an empty list for text-only inference.
        """
        if media_content and self.chat_handler is not None:
            content = [{"type": "text", "text": user_prompt}] + list(media_content)
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ]
        else:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]

        start = time.perf_counter()
        completion_kwargs = {
            "messages": messages,
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "top_p": float(top_p),
            "top_k": int(top_k),
            "min_p": float(min_p),
            "repeat_penalty": float(repetition_penalty),
            "seed": int(seed),
        }
        # presence_penalty is the key Qwen non-thinking setting (Unsloth: 1.5)
        # to stop repetition/looping. Only send when non-zero so we don't
        # disturb models that don't want it.
        if abs(float(presence_penalty)) > 1e-6:
            completion_kwargs["presence_penalty"] = float(presence_penalty)
        # frequency_penalty: only send when set, same reasoning as presence_penalty.
        if abs(float(frequency_penalty)) > 1e-6:
            completion_kwargs["frequency_penalty"] = float(frequency_penalty)
        # Mirostat: only engage when a mode is selected (0 = off). When on it
        # overrides top_k/top_p sampling with adaptive perplexity targeting.
        if int(mirostat_mode) != 0:
            completion_kwargs["mirostat_mode"] = int(mirostat_mode)
            completion_kwargs["mirostat_tau"] = float(mirostat_tau)
            completion_kwargs["mirostat_eta"] = float(mirostat_eta)
        # Thinking-mode kill switch via chat template. The /no_think control
        # token in the user prompt only works on some Qwen GGUF builds — passing
        # enable_thinking=False at the chat-template level is more reliable
        # because it bypasses the model's training entirely and renders the chat
        # template WITHOUT the thinking infrastructure.
        # (Approach borrowed from lihaoyun6/ComfyUI-llama-cpp_vlm.)
        #
        # Applies to Qwen 3/3.5/3.6 AND Gemma 4 — both expose an `enable_thinking`
        # Jinja variable (confirmed via LM Studio model.yaml customFields). When
        # the active path uses a hardcoded chat_format handler instead of the
        # embedded Jinja template (e.g. Gemma with chat_format="gemma3"), the
        # kwarg is simply ignored — harmless. Stray thinking is still stripped
        # from the output by _strip_think_blocks() as the safety net.
        name_lower = self.loaded_model_name_lower or ""
        if disable_thinking and ("qwen" in name_lower or "gemma" in name_lower):
            completion_kwargs["chat_template_kwargs"] = {"enable_thinking": False}

        completion_kwargs = _filter_kwargs_for_callable(
            self.llm.create_chat_completion, completion_kwargs
        )
        # Gemma-family models lose their natural EOS (</s>) at load time because
        # llama.cpp's EOG-list logic conflicts with <|tool_response>. Without an
        # explicit stop list they generate until max_tokens. Add Gemma stops back.
        # Qwen/other models keep llama.cpp's default EOS handling (no stop list).
        if "gemma" in (self.loaded_model_name_lower or ""):
            # SuperGemma uncensored fine-tunes emit "thought_turn" / "turn_turn"
            # as literal text in runaway loops (the fine-tune broke clean EOS
            # behavior). Add them as string stops so the model halts when those
            # appear, before they consume max_tokens worth of garbage.
            completion_kwargs["stop"] = [
                "<end_of_turn>", "<eos>", "</s>",
                "thought_turn", "turn_turn",
            ]
        result = self.llm.create_chat_completion(**completion_kwargs)
        elapsed = max(time.perf_counter() - start, 1e-6)

        usage = result.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        finish_reason = (result.get("choices") or [{}])[0].get("finish_reason", "unknown")
        if isinstance(completion_tokens, int) and completion_tokens > 0:
            tok_s = completion_tokens / elapsed
            print(
                f"[LLM_Prompt] Tokens: prompt={prompt_tokens}, "
                f"completion={completion_tokens}, "
                f"time={elapsed:.2f}s, speed={tok_s:.2f} tok/s, "
                f"finish={finish_reason}"
            )
        if finish_reason == "length":
            print(
                f"[LLM_Prompt] âš  Output TRUNCATED â€” hit max_tokens={max_tokens}. "
                f"Increase max_tokens to get complete output."
            )

        raw = str((result.get("choices") or [{}])[0].get("message", {}).get("content", "") or "")
        # Strip think blocks here, before any caller sees the output.
        # This covers both JSON and text paths â€” no downstream path needs to handle it.
        stripped = _strip_think_blocks(raw)

        # SALVAGE: aggressive uncensored fine-tunes (e.g. *-HauhauCS-Aggressive)
        # routinely ignore enable_thinking AND /no_think, generating a huge
        # reasoning block and then stopping â€” leaving NOTHING after </think>.
        # _strip_think_blocks then returns empty -> "NO PROMPT OUTPUT".
        # When that happens, try to recover the actual prompt draft buried in the
        # reasoning blob instead of silently returning empty.
        if not stripped.strip() and raw.strip():
            salvaged = self._extract_draft_from_reasoning(raw)
            if salvaged and salvaged.strip():
                print("[LLM_Prompt] Output was all reasoning â€” salvaged the prompt draft from it.")
                return salvaged.strip()
            print(
                "[LLM_Prompt] âš  Model produced ONLY reasoning, no usable prompt "
                f"({len(raw)} chars of think). This model ignores no-think controls "
                "â€” try a non-'reasoning'/non-aggressive build, or a different model."
            )
        return stripped

    def _extract_draft_from_reasoning(self, text: str) -> str:
        """Try to pull the final draft paragraph out of a raw reasoning blob.

        Qwen 3.5 uncensored models consistently structure their thinking output as:
          1. Analysis / deconstruction
          2. Drafting section
          3. "Revised Draft:" or "Final Draft:" followed by the actual output
          4. Optional review / token count check
          5. "Ready to output." or similar terminator

        This method finds the last "Revised Draft:" or "Draft Construction:" marker
        and returns only the paragraph that follows it, stripping any trailing
        meta-lines and artifacts.

        Returns empty string if no recognisable draft marker is found.
        """
        if not text:
            return ""

        # Find the last occurrence of a draft marker line
        # (last because sometimes there are multiple draft iterations)
        # Covers Qwen 3.5 patterns: "Revised Draft:", "Final Draft:", "Final Prompt:",
        # "**Final Prompt:**", "Draft Construction:", "Here is the prompt:", etc.
        markers = list(re.finditer(
            r"(?im)^.*?(\*{0,2}(revised|final)\s+(draft|prompt|output)\*{0,2}\s*:|"  # noqa: W503
            r"draft\s+construction\s*:|here\s+is\s+(the\s+)?(final\s+)?(prompt|output)\s*:|"  # noqa: W503
            r"^6\.\s+\*{0,2}draft).*$",
            text
        ))
        if not markers:
            return ""

        # Take everything after the last marker
        after = text[markers[-1].end():].strip()
        if not after:
            return ""

        # Strip all trailing meta/review lines Qwen 3.5 appends after the draft paragraph.
        # Applied in a loop until no more matches â€” lines can stack.
        for _ in range(10):
            before = after
            after = re.sub(
                r"(?im)^\s*(ready\s+to\s+output|looks\s+(solid|good|tight)|final\s+(check|review|polish|output)[^\n]*|token\s+count[^\n]*|approx\.?\s*\d+\s+tokens[^\n]*|good\s+range[^\n]*|section\s+order[^\n]*|let'?s\s+refine[^\n]*|check\s+for\s+(nsfw|aspect)[^\n]*)\.*\s*$",
                "", after
            ).strip()
            if after == before:
                break

        # Strip trailing cw artifact
        after = re.sub(r"(?m)cw\s*$", "", after).strip()

        # If what remains still looks like reasoning, give up
        if _looks_like_reasoning(after):
            return ""

        return after

    def _invoke_with_retry(
        self,
        system_prompt: str,
        user_prompt: str,
        media_content: list[dict],
        max_tokens: int,
        temperature: float,
        top_p: float,
        repetition_penalty: float,
        seed: int,
    ) -> str:
        """Invoke with reasoning detection, draft extraction, and retry.

        Strategy (in order):
        1. _invoke() strips tagged <think> blocks and known Qwen 3.5 raw reasoning markers.
        2. If output still looks like reasoning, attempt to extract the final draft
           paragraph directly from the blob â€” the answer is usually in there.
        3. Only if extraction fails, fire a retry with a tight system prompt.
        """
        raw = self._invoke(system_prompt, user_prompt, media_content, max_tokens, temperature, top_p, repetition_penalty, seed)

        if not _looks_like_reasoning(raw):
            # Clean output â€” run through cleaner and return
            return clean_model_output(raw, OutputCleanConfig(mode="prompt")) or raw.strip()

        # Reasoning detected â€” try to extract the draft before retrying
        print("[LLM_Prompt] Reasoning detected in output. Attempting draft extraction.")
        extracted = self._extract_draft_from_reasoning(raw)
        if extracted:
            print("[LLM_Prompt] Draft extracted successfully.")
            return clean_model_output(extracted, OutputCleanConfig(mode="prompt")) or extracted.strip()

        # Extraction failed â€” retry with a tight prompt, feeding back only the raw blob
        print("[LLM_Prompt] Extraction failed. Retrying with distillation prompt.")
        retry_system = (
            "You are a professional prompt writer.\n"
            "Output ONLY ONE final prompt paragraph.\n"
            "No analysis, no planning steps, no first-person.\n"
            "No bullet points, no headings, no JSON, no markdown, no quotes."
        )
        retry_user = f"Extract and rewrite only the final image prompt from the following text:\n\n{raw}"
        raw_retry = self._invoke(retry_system, retry_user, [], max_tokens, 0.4, 0.95, 1.05, seed + 999)

        if not _looks_like_reasoning(raw_retry):
            cleaned_retry = clean_model_output(raw_retry, OutputCleanConfig(mode="prompt"))
            if cleaned_retry:
                return cleaned_retry

        # Both extraction and retry failed — return whatever _strip_think_blocks gave us
        # Use keep_first_paragraph_only so trailing notes/analysis are dropped.
        print("[LLM_Prompt] Warning: could not clean reasoning output. Returning best effort.")
        cleaned = clean_model_output(raw, OutputCleanConfig(mode="prompt", keep_first_paragraph_only=True))
        return cleaned or raw.strip()

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def generate(
        self,
        model_name: str,
        system_prompt: str,
        custom_system_prompt: str,
        user_prompt: str,
        output_format: str,
        split_output: bool,
        max_tokens: int,
        temperature: float,
        top_p: float,
        top_k: int,
        min_p: float,
        repetition_penalty: float,
        device: str,
        auto_settings: bool,
        disable_thinking: bool,
        keep_model_loaded: bool,
        seed: int,
        n_ctx: int = 32768,
        n_gpu_layers: int = -1,
        presence_penalty: float = 0.0,
        frequency_penalty: float = 0.0,
        mirostat_mode: int = 0,
        mirostat_tau: float = 5.0,
        mirostat_eta: float = 0.1,
        preserve_thinking: bool = False,
        image_min_tokens: int = 1024,
        image_max_tokens: int = 4096,
        video_fps: float = 1.0,
        style: str = "",
        width: int = 0,
        height: int = 0,
        image=None,
        reference_image=None,
        video=None,
        audio=None,
    ):
        # ---- Server-side model-specific settings ----------------------------
        # The frontend JS preset only fires on a manual dropdown click, so on
        # workflow load / node creation / refresh the sliders keep stale or wrong
        # values (e.g. Gemma running at temp 0.6 / min_p 0.05 instead of 1.0 / 0).
        # When auto_settings is ON we resolve the OFFICIAL settings for the model
        # family here and override the widgets — guaranteed correct every run.
        # We also force thinking OFF (never useful for prompt generation).
        # presence_penalty arrives from the widget; auto_settings may override it.
        if auto_settings:
            resolved = _resolve_model_settings((model_name or "").lower())
            if resolved:
                temperature = resolved["temperature"]
                top_p = resolved["top_p"]
                top_k = resolved["top_k"]
                min_p = resolved["min_p"]
                repetition_penalty = resolved["repetition_penalty"]
                presence_penalty = resolved["presence_penalty"]
                disable_thinking = True
                print(
                    f"[LLM_Prompt] auto_settings ON -> temp={temperature}, top_p={top_p}, "
                    f"top_k={top_k}, min_p={min_p}, presence_penalty={presence_penalty}, "
                    f"rep={repetition_penalty}, thinking=OFF"
                )
            else:
                print(
                    f"[LLM_Prompt] auto_settings ON but no known preset for "
                    f"'{model_name}' â€” using the slider values as-is."
                )

        # Resolve system prompt
        if custom_system_prompt and custom_system_prompt.strip():
            sys_prompt = custom_system_prompt.strip()
        else:
            sys_prompt = SYSTEM_PROMPTS.get(system_prompt, "")

        if not sys_prompt:
            sys_prompt = (
                "You are a professional prompt writer. "
                "Generate a detailed, vivid prompt based on the user's input. "
                "Output only the final prompt text."
            )

        # Append output format instruction
        if output_format == "json":
            sys_prompt += (
                "\n\nReturn the output as valid JSON only. "
                "No preface, no explanations, no markdown fences, no </think>. "
                "Do not wrap in code blocks."
            )
        elif output_format == "list":
            sys_prompt += (
                "\n\nOutput only the numbered list exactly as specified in your instructions. "
                "No preface, no explanations, no analysis, no markdown fences, no </think>. "
                "Keep every item on its own numbered line."
            )
        else:
            sys_prompt += (
                "\n\nReturn only the final prompt text. "
                "No preface, no explanations, no analysis, no JSON, no markdown fences, and no </think>.\n"
                "Do not write planning steps (no 'First', 'Next', 'Then') and do not use first-person ('I', 'we')."
            )

        # Qwen 3.5 thinking suppression: /no_think is the official control token
        # recognized by Qwen 3.5's embedded chat template.
        # Uses self.loaded_model_name_lower set at model load time â€” no signature parsing.
        m_name_lower = self.loaded_model_name_lower
        is_qwen35 = (
            "qwen3.5" in m_name_lower
            or "qwen3-5" in m_name_lower
            or "qwen35" in m_name_lower
        )
        if is_qwen35 and disable_thinking:
            sys_prompt = "/no_think\n" + sys_prompt + "\n/no_think"

        # Build user prompt with labeled sections
        parts = []
        if user_prompt and user_prompt.strip():
            parts.append(f"USER PROMPT:\n{user_prompt.strip()}")
        if style and style.strip():
            parts.append(f"STYLE DESCRIPTION:\n{style.strip()}")
        if width > 0 and height > 0:
            canvas_block = _build_canvas_profile(width, height)
            if canvas_block:
                parts.append(canvas_block)

        final_user_prompt = "\n\n".join(parts) if parts else "Describe a scene vividly."

        # Qwen 3.x native control token: tells thinking models to skip the
        # thinking phase entirely and output the answer directly. Massive speed
        # win (~3x). Ignored silently by non-Qwen models, so safe to always append.
        # Gated on disable_thinking so users who want reasoning can get it.
        if disable_thinking and "qwen" in (model_name or "").lower() and "/no_think" not in final_user_prompt:
            final_user_prompt = f"{final_user_prompt}\n\n/no_think"


        # ---- Build the multimodal content list (image / reference / video / audio)
        # Each item is an OpenAI-style content dict, ordered and labelled here so
        # _invoke just prepends the user-prompt text and sends it.
        media_content: list[dict] = []

        # Main image(s). When a reference image is also present, label this one so
        # the model knows which is the content source vs the style source.
        if image is not None:
            frames = [image[i] for i in range(image.shape[0])] if image.ndim == 4 else [image]
            if reference_image is not None and frames:
                media_content.append({"type": "text", "text": "Input Image:"})
            for fr in frames:
                media_content.append(_image_tensor_to_content(fr))

        # Reference image for style-transfer prompts.
        if reference_image is not None:
            media_content.append({"type": "text", "text": "Reference Image:"})
            ref_frame = reference_image[0] if reference_image.ndim == 4 else reference_image
            media_content.append(_image_tensor_to_content(ref_frame))

        # Video frames, FPS-subsampled and context-budget aware.
        if video is not None:
            media_content.extend(
                _video_tensor_to_contents(video, target_fps=video_fps, n_ctx=n_ctx)
            )

        # Audio (Gemma-4 audio projector only). Never hard-fail on audio issues.
        if audio is not None:
            try:
                media_content.append(_audio_to_content(audio))
            except Exception as e:
                print(f"[LLM_Prompt] Audio could not be encoded ({e}); ignoring audio input.")

        # Load model and generate. want_vision drives whether we load the vision
        # handler at all â€” only when image/reference/video/audio is connected.
        # With no media, Qwen loads text-only so the no-think template engages.
        want_vision = bool(media_content)
        try:
            self._load_model(
                model_name, device, n_ctx=n_ctx, n_gpu_layers=n_gpu_layers,
                disable_thinking=disable_thinking, want_vision=want_vision,
                preserve_thinking=preserve_thinking,
                image_min_tokens=image_min_tokens, image_max_tokens=image_max_tokens,
            )

            if media_content and self.chat_handler is None:
                print("[LLM_Prompt] Warning: media provided but no vision/audio projector. Media will be ignored.")

            # All output formats route through _invoke() directly.
            # _invoke() returns text already cleaned by _strip_think_blocks().
            # No reasoning detection, no retry loops, no false positives.
            result = self._invoke(
                system_prompt=sys_prompt,
                user_prompt=final_user_prompt,
                media_content=media_content if self.chat_handler is not None else [],
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                min_p=min_p,
                repetition_penalty=repetition_penalty,
                seed=seed,
                disable_thinking=disable_thinking,
                presence_penalty=presence_penalty,
                frequency_penalty=frequency_penalty,
                mirostat_mode=mirostat_mode,
                mirostat_tau=mirostat_tau,
                mirostat_eta=mirostat_eta,
            )

            # Normalize labeled positive/negative output to pipe format for the
            # Prompt Splitter (no-op when | already present or no negative label).
            # Local models usually follow the pipe instruction, but this is a
            # cheap safety net matching the API node's behavior.
            if output_format == "text":
                result = normalize_prompt_separator(result)

            # NOTE: Qwen 3.5 cache-clear was tried here but caused mixed output
            # when the _ctx.memory_clear() call silently failed on some
            # llama-cpp-python versions — leaving n_tokens=0 with stale KV cache.
            # If the "works twice then stops" issue returns, the right fix is to
            # toggle keep_model_loaded=False, not to manipulate cache internals.

            # Split 'positive|negative' into the two outputs (or full text on
            # positive + empty negative when split_output is off / no pipe).
            positive, negative = split_positive_negative(result, split_output)
            return (positive, negative)

        finally:
            if not keep_model_loaded:
                self.clear()


# ---------------------------------------------------------------------------
# V3 node (comfy_api schema) — thin wrapper over the module-level runner
# ---------------------------------------------------------------------------

# Single cached runner shared across executions (V3 nodes are stateless).
_RUNNER = _LLMRunner()


class LLMPromptNode(io.ComfyNode):
    """V3 prompt-generation node — local GGUF vision/text models via llama-cpp.

    The heavy lifting (model cache, loading, inference, cleanup) lives in the
    module-level _LLMRunner. node_id and widget names are kept identical to the
    old V1 node so existing workflows and llm_prompt_presets.js keep working.
    """

    @classmethod
    def define_schema(cls) -> io.Schema:
        # Rescan on each schema build so freshly added models/prompts appear
        # without a restart (same behaviour as the old INPUT_TYPES).
        models = _refresh_model_list()
        model_keys = sorted(models.keys()) or ["(no .gguf files found in models/LLM)"]
        prompt_presets = list(SYSTEM_PROMPTS.keys()) if SYSTEM_PROMPTS else ["(add .md files to prompts/)"]
        gpu_list = [f"cuda:{i}" for i in range(torch.cuda.device_count())]
        device_options = ["auto", "cpu", "mps"] + gpu_list

        return io.Schema(
            node_id="LLMPrompt",
            display_name="LLM Prompt",
            category="LLM Prompt",
            description=(
                "Prompt generation via local GGUF vision/text models (llama-cpp). "
                "Qwen 2.5/3/3.5/3.6 and Gemma 3/4 with automatic family detection, "
                "handler-level thinking control, and image/video/audio inputs."
            ),
            inputs=[
                # ----- Core -----
                io.Combo.Input("model_name", options=model_keys, default=model_keys[0],
                               tooltip="GGUF model from the models/LLM folder. Rescans on each load."),
                io.Combo.Input("system_prompt", options=prompt_presets, default=prompt_presets[0],
                               tooltip="System prompt preset loaded from prompts/*.md files."),
                io.String.Input("custom_system_prompt", multiline=True, default="",
                                tooltip="Custom system prompt. Overrides the preset if not empty."),
                io.String.Input("user_prompt", multiline=True, default="",
                                tooltip="Your prompt text. Combined with the style input if connected."),
                io.Combo.Input("output_format", options=["text", "json", "list"], default="text",
                               tooltip="text = single prompt with reasoning cleanup. json = structured JSON. "
                                       "list = numbered multi-scene output (for LTX video tracks)."),
                io.Boolean.Input("split_output", default=True,
                                 tooltip="Split the model's 'positive|negative' output into the two node outputs. "
                                         "OFF = full text on positive, negative empty."),
                io.Boolean.Input("disable_thinking", default=True,
                                 tooltip="Stop thinking-capable models from emitting a reasoning block. Controlled at "
                                         "the chat handler when a vision model is loaded, plus /no_think + output "
                                         "stripping as fallbacks. Forced ON when auto_settings is on."),
                io.Boolean.Input("auto_settings", default=True,
                                 tooltip="Apply the OFFICIAL recommended sampling settings for the detected model "
                                         "family and force thinking OFF, overriding the sliders. Turn OFF to control "
                                         "every setting manually."),
                io.Int.Input("max_tokens", default=2048, min=64, max=32000,
                             tooltip="Maximum tokens to generate. Raise for large models like Gemma 4 26B."),
                io.Float.Input("temperature", default=0.7, min=0.1, max=2.0, step=0.05),
                io.Float.Input("top_p", default=0.9, min=0.0, max=1.0, step=0.05),
                io.Int.Input("top_k", default=30, min=0, max=200,
                             tooltip="Top-K sampling. 0 = disabled. 30 quality floor for Qwen, 64 for Gemma 4."),
                io.Float.Input("min_p", default=0.05, min=0.0, max=1.0, step=0.01,
                               tooltip="Min-P sampling quality floor. 0.05 is a safe default."),
                io.Float.Input("repetition_penalty", default=1.0, min=0.5, max=2.0, step=0.05,
                               tooltip="1.0 = disabled (Gemma 4 recommendation). Raise only if you see repetition."),
                io.Int.Input("seed", default=1, min=1, max=2**32 - 1),
                io.Boolean.Input("keep_model_loaded", default=True,
                                 tooltip="Keep the model in VRAM between runs."),
                io.Combo.Input("device", options=device_options, default="auto",
                               tooltip="Device for inference. auto = GPU if available."),

                # ----- Advanced (kept at the bottom; auto_settings stays authoritative) -----
                io.Float.Input("presence_penalty", default=0.0, min=-2.0, max=2.0, step=0.1,
                               tooltip="Advanced: discourages repeating any token. auto_settings overrides this."),
                io.Float.Input("frequency_penalty", default=0.0, min=-2.0, max=2.0, step=0.1,
                               tooltip="Advanced: penalizes frequent tokens. 0 = off."),
                io.Int.Input("mirostat_mode", default=0, min=0, max=2,
                             tooltip="Advanced: 0 = off. 1/2 enable Mirostat adaptive sampling (overrides top_k/top_p)."),
                io.Float.Input("mirostat_tau", default=5.0, min=0.0, max=20.0, step=0.1,
                               tooltip="Advanced: Mirostat target entropy (only when mirostat_mode != 0)."),
                io.Float.Input("mirostat_eta", default=0.1, min=0.0, max=1.0, step=0.01,
                               tooltip="Advanced: Mirostat learning rate (only when mirostat_mode != 0)."),
                io.Boolean.Input("preserve_thinking", default=False,
                                 tooltip="Advanced (Qwen 3.5/3.6 vision): keep <think> traces for ALL prior turns."),
                io.Int.Input("image_min_tokens", default=1024, min=256, max=4096, step=64,
                             tooltip="Advanced: min visual tokens per image (Qwen VL handlers). Higher = finer detail."),
                io.Int.Input("image_max_tokens", default=4096, min=1024, max=16384, step=64,
                             tooltip="Advanced: max visual tokens per image. Caps VRAM on large images."),
                io.Float.Input("video_fps", default=1.0, min=0.1, max=5.0, step=0.1,
                               tooltip="Advanced: temporal sampling rate for the video input (frames per second)."),
                io.Int.Input("n_ctx", default=32768, min=2048, max=262144, step=256,
                             tooltip="Advanced: context window size. Higher uses more VRAM."),
                io.Int.Input("n_gpu_layers", default=-1, min=-1, max=200, step=1,
                             tooltip="Advanced: -1 = offload all layers to GPU. Reduce if VRAM runs out."),

                # ----- Optional connections -----
                io.String.Input("style", optional=True, force_input=True,
                                tooltip="Style description from an external node."),
                io.Int.Input("width", optional=True, force_input=True,
                             tooltip="Image width — auto-detected as an aspect-ratio composition profile."),
                io.Int.Input("height", optional=True, force_input=True,
                             tooltip="Image height — auto-detected as an aspect-ratio composition profile."),
                io.Image.Input("image", optional=True, tooltip="Input image for vision analysis."),
                io.Image.Input("reference_image", optional=True,
                               tooltip="Reference image for style-transfer prompts (the style source)."),
                io.Image.Input("video", optional=True, tooltip="Video frames [F,H,W,3] for vision analysis."),
                io.Audio.Input("audio", optional=True, tooltip="Audio input (Gemma-4 audio projector only)."),
            ],
            outputs=[
                io.String.Output("positive", display_name="positive"),
                io.String.Output("negative", display_name="negative"),
            ],
        )

    @classmethod
    def execute(cls, **kwargs) -> io.NodeOutput:
        # Drop any framework-injected extras the runner doesn't expect, then run.
        call_kwargs = _filter_kwargs_for_callable(_LLMRunner.generate, kwargs)
        positive, negative = _RUNNER.generate(**call_kwargs)
        return io.NodeOutput(positive, negative)


# ---------------------------------------------------------------------------
# ComfyUI registration — V3 io.ComfyNode classes register fine through the
# classic NODE_CLASS_MAPPINGS path (they carry a V1-compat shim), so the
# package's existing __init__ aggregation with the API/Grok nodes is unchanged.
# ---------------------------------------------------------------------------

NODE_CLASS_MAPPINGS = {
    "LLMPrompt": LLMPromptNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LLMPrompt": "LLM Prompt",
}