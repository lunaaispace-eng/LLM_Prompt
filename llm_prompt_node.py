
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
import io
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
from llama_cpp import Llama
from PIL import Image

import folder_paths

# Bundled output cleaner â€” no external dependencies
from .output_cleaner import OutputCleanConfig, clean_model_output

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NODE_DIR = Path(__file__).parent
PROMPTS_DIR = NODE_DIR / "prompts"
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
    """Convert pixel dimensions to a composition-intent aspect ratio string."""
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


def _scan_llm_folder() -> dict[str, dict]:
    """Scan all registered LLM folders recursively and return a dict of display_name -> model info.

    For each .gguf found that is NOT an mmproj file:
      - display_name = filename (e.g. "Qwen3-VL-8B-Q8_0.gguf")
      - full_path    = absolute path to the model file
      - mmproj_path  = absolute path to companion mmproj if found in same folder, else None

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

    # Walk all registered folders
    all_ggufs = []
    for llm_folder in existing:
        all_ggufs.extend(sorted(llm_folder.rglob("*.gguf")))

    for gguf_file in all_ggufs:
        name_lower = gguf_file.name.lower()

        # Skip mmproj files â€” they are companions, not models
        if "mmproj" in name_lower:
            continue

        # Find mmproj companions in the same folder
        folder = gguf_file.parent
        mmproj_candidates = [
            f for f in folder.glob("*.gguf")
            if "mmproj" in f.name.lower()
        ]

        mmproj_path = None
        if mmproj_candidates:
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

        # Build display name â€” use filename, suffix with parent folder if duplicate
        display = gguf_file.name
        if display in seen_names:
            display = f"{gguf_file.parent.name}/{gguf_file.name}"
        seen_names.add(display)

        models[display] = {
            "full_path": str(gguf_file),
            "mmproj_path": str(mmproj_path) if mmproj_path else None,
        }

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
    if not entry:
        raise FileNotFoundError(
            f"[LLM_Prompt] Model not found in scan: {model_name}. "
            f"Check that the file exists in your LLM folder."
        )
    model_path = Path(entry["full_path"])
    mmproj_path = Path(entry["mmproj_path"]) if entry.get("mmproj_path") else None
    return model_path, mmproj_path


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
    """Remove thinking content from model output.

    Handles:
    - Tagged blocks:   <think>...</think>content  â†’ content
    - Unclosed tags:   <think>...EOF              â†’ empty
    - Qwen 3.5 raw reasoning sections (no tags): strips everything up to the
      last recognizable section header, then takes what follows as the output.
    - Trailing 'cw' terminator artifact from Qwen 3.5 thinking mode.
    """
    if not text:
        return text

    # Remove tagged think blocks (complete and unclosed)
    cleaned = re.sub(r"(?is)<think>.*?</think>", "", text)
    cleaned = re.sub(r"(?is)<think>.*$", "", cleaned)

    # Strip Qwen 3.5 raw reasoning: find the last "Revised Draft:" or "Ready to output"
    # marker and take only the content that follows it as the actual output.
    reasoning_section_end = re.search(
        r"(?im)^.*?(revised\s+draft\s*:|final\s+output\s*:).*$",
        cleaned
    )
    if reasoning_section_end:
        after = cleaned[reasoning_section_end.end():].strip()
        if after:
            cleaned = after

    # Strip trailing meta-lines Qwen 3.5 emits after the draft â€” loop until stable
    for _ in range(10):
        before = cleaned
        cleaned = re.sub(
            r"(?im)^\s*(ready\s+to\s+output|looks\s+(solid|good|tight)|final\s+(check|review|polish|output)[^\n]*|token\s+count[^\n]*|approx\.?\s*\d+\s+tokens[^\n]*|good\s+range[^\n]*|section\s+order[^\n]*|let'?s\s+refine[^\n]*|check\s+for\s+(nsfw|aspect)[^\n]*)\.*\s*$",
            "", cleaned
        ).strip()
        if cleaned == before:
            break

    # Strip trailing Qwen 3.5 thinking terminator artifact: bare "cw" at end of output
    cleaned = re.sub(r"(?m)\bcw\s*$", "", cleaned).strip()

    # Gemma 4 / SuperGemma: strip control tokens that can leak from embedded templates
    cleaned = re.sub(r"_?\s*<\|?channel\|?>\s*(?:thought|analysis|reasoning)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</?thought>", "", cleaned, flags=re.IGNORECASE)

    # SuperGemma uncensored: hard-cut on loop runaway tokens â€” everything after is garbage
    for _marker in ("thought_turn", "turn_turn"):
        if _marker in cleaned:
            cleaned = cleaned.split(_marker)[0]

    # SuperGemma uncensored: strip language prefix artifacts emitted before the actual answer
    cleaned = re.sub(r"^ë‚˜ì˜ ë‹µë³€ì€ ë‹¤ìŒê³¼ ê°™ìŠµë‹ˆë‹¤\.\s*", "", cleaned)
    cleaned = re.sub(r"^å›žç­”[:ï¼š]\s*", "", cleaned)
    cleaned = re.sub(r"^(?:Assistant|Answer):\s*", "", cleaned, flags=re.IGNORECASE)

    return cleaned.strip()


# ---------------------------------------------------------------------------
# The Node â€” built on QwenVL-Mod's proven inference code
# ---------------------------------------------------------------------------

class LLMPromptNode:
    """Prompt generation node using local GGUF VL models.

    Uses the same inference code as QwenVL-Mod GGUF. Adds system prompt
    presets from .md files, user prompt, style, and aspect ratio inputs.
    """

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("PROMPT",)
    FUNCTION = "generate"
    CATEGORY = "LLM Prompt"
    OUTPUT_NODE = False

    def __init__(self):
        self.llm = None
        self.chat_handler = None
        self.current_signature = None
        self.loaded_model_name_lower = ""

    @classmethod
    def INPUT_TYPES(cls):
        # Refresh scan every time the node UI loads â€” new models appear without restart
        models = _refresh_model_list()
        model_keys = sorted(models.keys()) or ["(no .gguf files found in models/LLM)"]

        prompt_presets = list(SYSTEM_PROMPTS.keys()) if SYSTEM_PROMPTS else ["(add .md files to prompts/)"]

        num_gpus = torch.cuda.device_count()
        gpu_list = [f"cuda:{i}" for i in range(num_gpus)]
        device_options = ["auto", "cpu", "mps"] + gpu_list

        return {
            "required": {
                "model_name": (model_keys, {
                    "default": model_keys[0],
                    "tooltip": "GGUF model from models/LLM folder. Rescans on each load.",
                }),
                "system_prompt": (prompt_presets, {
                    "default": prompt_presets[0],
                    "tooltip": "System prompt preset loaded from prompts/*.md files.",
                }),
                "custom_system_prompt": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "tooltip": "Custom system prompt. Overrides preset if not empty.",
                }),
                "user_prompt": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "tooltip": "Your prompt text. Combined with style input if connected.",
                }),
                "output_format": (["text", "json", "list"], {
                    "default": "text",
                    "tooltip": "text = single prompt with reasoning cleanup. json = structured JSON. list = numbered multi-scene output (bypasses reasoning detection â€” use for LTX video track generation).",
                }),
                "max_tokens": ("INT", {
                    "default": 2048,
                    "min": 64,
                    "max": 32000,
                    "tooltip": "Maximum tokens to generate. 2048 works for most prompts. Raise for large models like Gemma 4 26B.",
                }),
                "temperature": ("FLOAT", {
                    "default": 0.7,
                    "min": 0.1,
                    "max": 2.0,
                    "step": 0.05,
                }),
                "top_p": ("FLOAT", {
                    "default": 0.9,
                    "min": 0.0,
                    "max": 1.0,
                    "step": 0.05,
                }),
                "repetition_penalty": ("FLOAT", {
                    "default": 1.1,
                    "min": 0.5,
                    "max": 2.0,
                    "step": 0.05,
                }),
                "device": (device_options, {
                    "default": "auto",
                    "tooltip": "Device for inference. auto = GPU if available.",
                }),
                "keep_model_loaded": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Keep model in VRAM between runs.",
                }),
                "seed": ("INT", {
                    "default": 1,
                    "min": 1,
                    "max": 2**32 - 1,
                }),
                "n_ctx": ("INT", {
                    "default": 32768,
                    "min": 2048,
                    "max": 262144,
                    "step": 256,
                    "tooltip": "Context window size. 32768 works for most models. Gemma 4 supports up to 128K, SuperGemma up to 262K. Higher values use more VRAM.",
                }),
                "n_gpu_layers": ("INT", {
                    "default": -1,
                    "min": -1,
                    "max": 200,
                    "step": 1,
                    "tooltip": "-1 = offload all layers to GPU (recommended). Reduce if VRAM runs out loading large models like Gemma 4 26B. Ignored (forced to 0) when device is CPU.",
                }),
            },
            "optional": {
                "style": ("STRING", {
                    "forceInput": True,
                    "tooltip": "Style description from external node.",
                }),
                "width": ("INT", {
                    "forceInput": True,
                    "tooltip": "Image width â€” auto-detected as aspect ratio.",
                }),
                "height": ("INT", {
                    "forceInput": True,
                    "tooltip": "Image height â€” auto-detected as aspect ratio.",
                }),
                "image": ("IMAGE", {
                    "tooltip": "Input image for vision analysis.",
                }),
                "video": ("IMAGE", {
                    "tooltip": "Input video frames for vision analysis.",
                }),
            },
        }

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

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()

    # ------------------------------------------------------------------
    # Model loading (same as QwenVL-Mod GGUF)
    # ------------------------------------------------------------------

    def _load_model(self, model_name: str, device: str, n_ctx: int = 32768, n_gpu_layers: int = -1):
        # Resolve paths directly from the scanned folder list â€” no JSON catalog needed
        model_path, mmproj_path = _resolve_model_paths(model_name)

        if not model_path.exists():
            raise FileNotFoundError(f"[LLM_Prompt] Model file missing: {model_path}")
        if mmproj_path and not mmproj_path.exists():
            print(f"[LLM_Prompt] Warning: mmproj not found at {mmproj_path}, running text-only.")
            mmproj_path = None

        device_kind = _pick_device(device)
        # CPU always 0; on GPU use the user-supplied value (-1 = all layers, N = partial offload)
        effective_gpu_layers = 0 if device_kind == "cpu" else n_gpu_layers
        has_mmproj = mmproj_path is not None

        signature = {
            "model_path": str(model_path),
            "mmproj_path": str(mmproj_path) if has_mmproj else "",
            "n_gpu_layers": effective_gpu_layers,
            "n_ctx": n_ctx,
            "device_kind": device_kind,
        }
        if self.llm is not None and self.current_signature == signature:
            return

        self.clear()
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            time.sleep(0.1)

        # Load vision handler â€” handler selection order matches JamePeng 0.3.39 API
        self.chat_handler = None
        if has_mmproj:
            m_name_lower = model_path.name.lower()
            handler_cls = None

            # Gemma 4 vision: Gemma3ChatHandler (chat_format="gemma3")
            if "gemma" in m_name_lower:
                try:
                    from llama_cpp.llama_chat_format import Gemma3ChatHandler
                    handler_cls = Gemma3ChatHandler
                    print("[LLM_Prompt] Using Gemma3ChatHandler for vision.")
                except ImportError:
                    pass

            # Qwen 3.5 (text+vision): Qwen35ChatHandler
            if handler_cls is None and ("qwen3.5" in m_name_lower or "qwen3-5" in m_name_lower or "qwen35" in m_name_lower):
                try:
                    from llama_cpp.llama_chat_format import Qwen35ChatHandler
                    handler_cls = Qwen35ChatHandler
                    print("[LLM_Prompt] Using Qwen35ChatHandler.")
                except ImportError:
                    pass

            # Qwen 3 VL
            if handler_cls is None:
                try:
                    from llama_cpp.llama_chat_format import Qwen3VLChatHandler
                    handler_cls = Qwen3VLChatHandler
                    print("[LLM_Prompt] Using Qwen3VLChatHandler for vision.")
                except ImportError:
                    pass

            # Qwen 2.5 VL fallback
            if handler_cls is None:
                try:
                    from llama_cpp.llama_chat_format import Qwen25VLChatHandler
                    handler_cls = Qwen25VLChatHandler
                    print("[LLM_Prompt] Using Qwen25VLChatHandler for vision.")
                except ImportError:
                    pass

            if handler_cls is None:
                raise RuntimeError(
                    "[LLM_Prompt] No compatible vision chat handler found in llama_cpp. "
                    "Check your llama-cpp-python version."
                )

            # Build handler kwargs â€” image_min/max_tokens go HERE on the handler.
            # force_reasoning=False is only supported by Qwen3VLChatHandler and Qwen25VLChatHandler.
            # Qwen35ChatHandler and Gemma3ChatHandler reject it with a hard TypeError.
            # We resolve per-handler rather than relying on _filter_kwargs_for_callable because
            # these handlers use **kwargs internally and don't expose a normal inspectable signature.
            from llama_cpp.llama_chat_format import Qwen3VLChatHandler as _Q3VL
            try:
                from llama_cpp.llama_chat_format import Qwen25VLChatHandler as _Q25VL
            except ImportError:
                _Q25VL = None

            supports_force_reasoning = handler_cls in (
                _Q3VL, *([_Q25VL] if _Q25VL is not None else [])
            )

            mmproj_kwargs = {
                "clip_model_path": str(mmproj_path),
                "image_max_tokens": 4096,
                "image_min_tokens": 1024,
                "verbose": False,
            }
            if supports_force_reasoning:
                mmproj_kwargs["force_reasoning"] = False

            mmproj_kwargs = _filter_kwargs_for_callable(
                getattr(handler_cls, "__init__", handler_cls), mmproj_kwargs
            )
            self.chat_handler = handler_cls(**mmproj_kwargs)

        # Load LLM
        llm_kwargs = {
            "model_path": str(model_path),
            "n_ctx": n_ctx,
            "n_gpu_layers": effective_gpu_layers,
            "n_batch": 512,
            "swa_full": True,
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
                llm_kwargs["chat_format"] = "gemma3"
            elif "llama-3" in m_name_lower:
                llm_kwargs["chat_format"] = "llama-3"
            elif "qwen" in m_name_lower:
                # Let GGUF embedded template handle all Qwen variants (2.5, 3, 3.5, 3.6).
                # Do not override with chatml â€” it breaks enable_thinking support.
                pass

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
        images_b64: list[str],
        max_tokens: int,
        temperature: float,
        top_p: float,
        repetition_penalty: float,
        seed: int,
    ) -> str:
        """Run inference â€” same call signature as QwenVL-Mod GGUF."""
        if images_b64 and self.chat_handler is not None:
            content = [{"type": "text", "text": user_prompt}]
            for img in images_b64:
                if img:
                    content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img}"},
                    })
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
            "repeat_penalty": float(repetition_penalty),
            "seed": int(seed),
            # enable_thinking=False: passed here via chat_template_kwargs so the embedded
            # Jinja2 template in the GGUF can act on it. Best-effort â€” silently ignored
            # by models whose templates don't check for it.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        completion_kwargs = _filter_kwargs_for_callable(
            self.llm.create_chat_completion, completion_kwargs
        )
        # Stop tokens applied AFTER filtering so they can never be silently dropped.
        # Neither <think> nor </think> are stop tokens.
        # Stopping on <think> produces empty output when thinking models emit it first.
        # Stopping on </think> halts generation before the actual answer (which comes
        # AFTER the closing tag) â€” _strip_think_blocks() handles the full block in post.
        completion_kwargs["stop"] = [
            "<|im_end|>", "<|im_start|>",
            "<end_of_turn>", "<eos>", "<|eot_id|>", "<|end_of_text|>",
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

        raw = (result.get("choices") or [{}])[0].get("message", {}).get("content", "")
        # Strip think blocks here, before any caller sees the output.
        # This covers both JSON and text paths â€” no downstream path needs to handle it.
        return _strip_think_blocks(str(raw or ""))

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
        images_b64: list[str],
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
        raw = self._invoke(system_prompt, user_prompt, images_b64, max_tokens, temperature, top_p, repetition_penalty, seed)

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
        max_tokens: int,
        temperature: float,
        top_p: float,
        repetition_penalty: float,
        device: str,
        keep_model_loaded: bool,
        seed: int,
        n_ctx: int = 32768,
        n_gpu_layers: int = -1,
        style: str = "",
        width: int = 0,
        height: int = 0,
        image=None,
        video=None,
    ):
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
        if is_qwen35:
            sys_prompt = "/no_think\n" + sys_prompt + "\n/no_think"

        # Build user prompt with labeled sections
        parts = []
        if user_prompt and user_prompt.strip():
            parts.append(f"USER PROMPT:\n{user_prompt.strip()}")
        if style and style.strip():
            parts.append(f"STYLE DESCRIPTION:\n{style.strip()}")
        if width > 0 and height > 0:
            ar_label = _detect_aspect_ratio(width, height)
            parts.append(f"ASPECT RATIO / CANVAS FORMAT:\n{ar_label}")

        final_user_prompt = "\n\n".join(parts) if parts else "Describe a scene vividly."


        # Process images
        images_b64: list[str] = []
        if image is not None:
            if image.ndim == 4:
                for i in range(image.shape[0]):
                    img = _tensor_to_base64_png(image[i])
                    if img:
                        images_b64.append(img)
            else:
                img = _tensor_to_base64_png(image)
                if img:
                    images_b64.append(img)

        if video is not None:
            for frame in _sample_video_frames(video, 16):
                img = _tensor_to_base64_png(frame)
                if img:
                    images_b64.append(img)

        # Load model and generate
        try:
            self._load_model(model_name, device, n_ctx=n_ctx, n_gpu_layers=n_gpu_layers)

            if images_b64 and self.chat_handler is None:
                print("[LLM_Prompt] Warning: images provided but no vision projector. Images will be ignored.")

            if output_format in ("json", "list"):
                # Direct invoke â€” no reasoning detection or retry.
                # json: structured output. list: numbered multi-scene output where
                # numbered lines would be falsely flagged as reasoning.
                result = self._invoke(
                    system_prompt=sys_prompt,
                    user_prompt=final_user_prompt,
                    images_b64=images_b64 if self.chat_handler is not None else [],
                    max_tokens=max_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    repetition_penalty=repetition_penalty,
                    seed=seed,
                )
            else:
                result = self._invoke_with_retry(
                    system_prompt=sys_prompt,
                    user_prompt=final_user_prompt,
                    images_b64=images_b64 if self.chat_handler is not None else [],
                    max_tokens=max_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    repetition_penalty=repetition_penalty,
                    seed=seed,
                )

            return (result,)

        finally:
            if not keep_model_loaded:
                self.clear()


# ---------------------------------------------------------------------------
# ComfyUI registration
# ---------------------------------------------------------------------------

NODE_CLASS_MAPPINGS = {
    "LLMPrompt": LLMPromptNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LLMPrompt": "LLM Prompt",
}