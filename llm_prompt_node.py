"""LLM Prompt — Single ComfyUI node for prompt generation via GGUF vision models.

Loads system prompts from .md files in the prompts/ folder.
Accepts user_prompt (multiline text) and style (STRING from external nodes).
Uses llama-cpp-python with Qwen 3.5 optimal settings.

Usage:
    1. Place GGUF models in ComfyUI/models/LLM/GGUF/
    2. Add .md system prompt files to the prompts/ folder
    3. Connect the node — select model, pick a prompt preset, type your prompt
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
from typing import Optional

import numpy as np
import torch
from llama_cpp import Llama
from PIL import Image

from huggingface_hub import hf_hub_download

import folder_paths
from .output_cleaner import CleanConfig, clean_output

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NODE_DIR = Path(__file__).parent
PROMPTS_DIR = NODE_DIR / "prompts"
GGUF_CONFIG_PATH = NODE_DIR / "gguf_models.json"

# Qwen 3.5 optimal inference settings (Unsloth recommendations)
PROMPT_GEN_PARAMS = {
    "temperature": 0.6,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "repeat_penalty": 1.0,
}

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

    # Direct match
    if (w_r, h_r) in _ASPECT_LABELS:
        return _ASPECT_LABELS[(w_r, h_r)]

    # Find closest known ratio
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

    # Fallback: describe orientation with raw ratio
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

    Each file becomes a preset. The filename (without extension) is the
    display name. File content is the system prompt text.

    Optional YAML frontmatter (between --- markers) can set:
        title: Display Name Override
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

        # Check for YAML frontmatter
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
                # No yaml available or parse error — use full text
                content = text

        if content:
            prompts[title] = content

    return prompts


SYSTEM_PROMPTS = load_system_prompts()

# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------

def load_model_catalog() -> dict:
    """Load gguf_models.json catalog for VL models."""
    fallback = {"base_dir": "LLM/GGUF", "models": {}}
    if not GGUF_CONFIG_PATH.exists():
        return fallback
    try:
        with open(GGUF_CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except Exception as exc:
        print(f"[LLM_Prompt] gguf_models.json load failed: {exc}")
        return fallback

    base_dir = data.get("base_dir") or "LLM/GGUF"
    raw_models = data.get("models", {})

    flattened: dict[str, dict] = {}
    seen: set[str] = set()

    # Parse qwenVL_model repos (vision models)
    repos = raw_models.get("qwenVL_model") or {}
    for repo_key, repo in repos.items():
        if not isinstance(repo, dict):
            continue
        author = repo.get("author")
        repo_name = repo.get("repo_name") or repo_key
        repo_id = repo.get("repo_id")
        alt_repo_ids = repo.get("alt_repo_ids") or []
        defaults = repo.get("defaults") or {}
        mmproj_file = repo.get("mmproj_file")
        model_files = repo.get("model_files") or []

        for model_file in model_files:
            display = Path(model_file).name
            if display in seen:
                display = f"{display} ({repo_key})"
            seen.add(display)
            flattened[display] = {
                **defaults,
                "author": author,
                "repo_dirname": repo_name,
                "repo_id": repo_id,
                "alt_repo_ids": alt_repo_ids,
                "filename": model_file,
                "mmproj_filename": mmproj_file,
            }

    return {"base_dir": base_dir, "models": flattened}


MODEL_CATALOG = load_model_catalog()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_base_dir(base_dir_value: str) -> Path:
    base_dir = Path(base_dir_value)
    if base_dir.is_absolute():
        return base_dir
    return Path(folder_paths.models_dir) / base_dir


def _safe_dirname(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return "unknown"
    return "".join(ch for ch in value if ch.isalnum() or ch in "._- ").strip() or "unknown"


def _filter_kwargs(fn, kwargs: dict) -> dict:
    """Filter kwargs to only those accepted by fn's signature."""
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
    """Convert a ComfyUI image tensor to base64 PNG."""
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
    """Sample evenly-spaced frames from a video tensor."""
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
    return device_choice


def _download_file(repo_ids: list[str], filename: str, target_path: Path):
    """Download a single file from HuggingFace to target_path."""
    if target_path.exists():
        return

    target_path.parent.mkdir(parents=True, exist_ok=True)

    for repo_id in repo_ids:
        if not repo_id:
            continue
        print(f"[LLM_Prompt] Downloading {filename} from {repo_id}...")
        try:
            downloaded = hf_hub_download(
                repo_id=repo_id,
                filename=filename,
                repo_type="model",
                local_dir=str(target_path.parent),
            )
            downloaded_path = Path(downloaded)
            if downloaded_path.exists() and downloaded_path.resolve() != target_path.resolve():
                downloaded_path.replace(target_path)
            if target_path.exists():
                print(f"[LLM_Prompt] Downloaded: {target_path}")
                return
        except Exception as exc:
            print(f"[LLM_Prompt] Download failed from {repo_id}: {exc}")

    raise FileNotFoundError(
        f"[LLM_Prompt] Could not download {filename}. "
        f"Tried: {', '.join(r for r in repo_ids if r)}. "
        f"Download manually to: {target_path}"
    )


# ---------------------------------------------------------------------------
# Model entry resolution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedModel:
    display_name: str
    repo_id: str | None
    alt_repo_ids: list[str]
    author: str | None
    repo_dirname: str
    model_filename: str
    mmproj_filename: str | None
    context_length: int
    image_max_tokens: int
    n_batch: int
    gpu_layers: int
    top_k: int
    pool_size: int


def _resolve_model(model_name: str) -> ResolvedModel:
    """Resolve a model display name to full paths and settings."""
    all_models = MODEL_CATALOG.get("models") or {}
    entry = all_models.get(model_name) or {}

    # Fallback: match by filename
    if not entry:
        wanted = {model_name, f"{model_name}.gguf"}
        if "/" in model_name:
            tail = model_name.rsplit("/", 1)[-1].strip()
            wanted.update({tail, f"{tail}.gguf"})
        for candidate in all_models.values():
            fn = candidate.get("filename")
            if fn and Path(fn).name in wanted:
                entry = candidate
                break

    def _int(name: str, default: int) -> int:
        try:
            return int(entry.get(name, default))
        except Exception:
            return default

    return ResolvedModel(
        display_name=model_name,
        repo_id=entry.get("repo_id"),
        alt_repo_ids=[str(x) for x in (entry.get("alt_repo_ids") or []) if x],
        author=str(entry.get("author")) if entry.get("author") else None,
        repo_dirname=_safe_dirname(str(entry.get("repo_dirname") or model_name)),
        model_filename=str(entry.get("filename") or model_name),
        mmproj_filename=str(entry["mmproj_filename"]) if entry.get("mmproj_filename") else None,
        context_length=_int("context_length", 32768),
        image_max_tokens=_int("image_max_tokens", 4096),
        n_batch=_int("n_batch", 1024),
        gpu_layers=_int("gpu_layers", -1),
        top_k=_int("top_k", 20),
        pool_size=_int("pool_size", 4194304),
    )


# ---------------------------------------------------------------------------
# The Node
# ---------------------------------------------------------------------------

class LLMPromptNode:
    """Single-purpose node: generate or enhance prompts using a local GGUF VL model.

    Inputs:
        - model_name: GGUF model from catalog
        - system_prompt: preset from .md files in prompts/ folder
        - custom_system_prompt: override or extend the preset
        - user_prompt: your prompt text (multiline)
        - style: STRING input from external nodes (prepended to user_prompt)
        - image/video: optional vision inputs

    Output:
        - PROMPT: the generated/enhanced prompt text (STRING)
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

    @classmethod
    def INPUT_TYPES(cls):
        # Model list from catalog
        all_models = MODEL_CATALOG.get("models") or {}
        model_keys = sorted([
            key for key, entry in all_models.items()
            if (entry or {}).get("mmproj_filename")
        ]) or ["(add models to gguf_models.json)"]

        # System prompt presets from .md files
        prompt_presets = list(SYSTEM_PROMPTS.keys()) if SYSTEM_PROMPTS else ["(add .md files to prompts/)"]

        # Device options
        num_gpus = torch.cuda.device_count()
        gpu_list = [f"cuda:{i}" for i in range(num_gpus)]
        device_options = ["auto", "cpu", "mps"] + gpu_list

        return {
            "required": {
                "model_name": (model_keys, {
                    "default": model_keys[0],
                    "tooltip": "GGUF vision model from gguf_models.json catalog.",
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
                "output_format": (["text", "json"], {
                    "default": "text",
                    "tooltip": "Output format: text = natural language prompt, json = structured JSON output.",
                }),
                "max_tokens": ("INT", {
                    "default": 512,
                    "min": 64,
                    "max": 4096,
                    "tooltip": "Maximum tokens to generate.",
                }),
                "device": (device_options, {
                    "default": "auto",
                    "tooltip": "Device for inference. auto = GPU if available.",
                }),
                "keep_model_loaded": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Keep model in VRAM between runs. Faster but uses memory.",
                }),
                "seed": ("INT", {
                    "default": 1,
                    "min": 1,
                    "max": 2**32 - 1,
                }),
            },
            "optional": {
                "style": ("STRING", {
                    "forceInput": True,
                    "tooltip": "Style description from external node. Injected as visual treatment layer.",
                }),
                "width": ("INT", {
                    "forceInput": True,
                    "tooltip": "Image width in pixels. Auto-detected as aspect ratio for composition guidance.",
                }),
                "height": ("INT", {
                    "forceInput": True,
                    "tooltip": "Image height in pixels. Auto-detected as aspect ratio for composition guidance.",
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
    # VRAM cleanup
    # ------------------------------------------------------------------

    def clear(self):
        """Unload model and free VRAM."""
        if self.chat_handler is not None:
            try:
                if hasattr(self.chat_handler, "close"):
                    self.chat_handler.close()
                elif hasattr(self.chat_handler, "__del__"):
                    self.chat_handler.__del__()
            except Exception:
                pass
            self.chat_handler = None

        if self.llm is not None:
            try:
                if hasattr(self.llm, "close"):
                    self.llm.close()
                del self.llm
            except Exception:
                pass
            self.llm = None

        self.current_signature = None
        gc.collect()

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()

    # ------------------------------------------------------------------
    # Model loading
    # ------------------------------------------------------------------

    def _load_model(self, model_name: str, device: str):
        """Load a GGUF vision model with Qwen 3.5 settings."""
        resolved = _resolve_model(model_name)
        base_dir = _resolve_base_dir(MODEL_CATALOG.get("base_dir") or "LLM/GGUF")

        author_dir = _safe_dirname(resolved.author or "")
        repo_dir = _safe_dirname(resolved.repo_dirname)
        target_dir = base_dir / author_dir / repo_dir

        model_path = target_dir / Path(resolved.model_filename).name
        mmproj_path = (
            target_dir / Path(resolved.mmproj_filename).name
            if resolved.mmproj_filename else None
        )

        # Auto-download from HuggingFace if not found locally
        repo_ids = [r for r in [resolved.repo_id] + resolved.alt_repo_ids if r]

        if not model_path.exists():
            if repo_ids:
                _download_file(repo_ids, resolved.model_filename, model_path)
            else:
                raise FileNotFoundError(
                    f"[LLM_Prompt] Model not found: {model_path}\n"
                    f"No repo_id in catalog — download manually or add repo_id to gguf_models.json."
                )

        if mmproj_path and not mmproj_path.exists():
            if repo_ids and resolved.mmproj_filename:
                _download_file(repo_ids, resolved.mmproj_filename, mmproj_path)
            else:
                raise FileNotFoundError(
                    f"[LLM_Prompt] Vision projector not found: {mmproj_path}\n"
                    f"No repo_id in catalog — download manually or add repo_id to gguf_models.json."
                )

        device_kind = _pick_device(device)
        n_gpu_layers = resolved.gpu_layers if device_kind == "cuda" else 0
        has_mmproj = mmproj_path is not None and mmproj_path.exists()

        signature = (
            str(model_path),
            str(mmproj_path) if has_mmproj else "",
            resolved.context_length,
            n_gpu_layers,
            device_kind,
        )
        if self.llm is not None and self.current_signature == signature:
            return  # Already loaded

        # Cleanup before loading
        self.clear()
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            time.sleep(0.1)

        # Load vision handler
        self.chat_handler = None
        if has_mmproj:
            handler_cls = None
            try:
                from llama_cpp.llama_chat_format import Qwen3VLChatHandler
                handler_cls = Qwen3VLChatHandler
            except ImportError:
                try:
                    from llama_cpp.llama_chat_format import Qwen25VLChatHandler
                    handler_cls = Qwen25VLChatHandler
                except ImportError:
                    raise RuntimeError(
                        "[LLM_Prompt] No Qwen VL chat handler found in llama_cpp. "
                        "Update llama-cpp-python to a vision-capable build."
                    )

            mmproj_kwargs = {
                "clip_model_path": str(mmproj_path),
                "image_max_tokens": resolved.image_max_tokens,
                "force_reasoning": False,
                "verbose": False,
            }
            mmproj_kwargs = _filter_kwargs(
                getattr(handler_cls, "__init__", handler_cls), mmproj_kwargs
            )
            self.chat_handler = handler_cls(**mmproj_kwargs)

        # Load LLM
        llm_kwargs = {
            "model_path": str(model_path),
            "n_ctx": resolved.context_length,
            "n_gpu_layers": n_gpu_layers,
            "n_batch": resolved.n_batch,
            "swa_full": True,
            "verbose": False,
            "pool_size": resolved.pool_size,
            "top_k": resolved.top_k,
            # Qwen 3.5: thinking mode OFF for prompt generation
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if has_mmproj and self.chat_handler is not None:
            llm_kwargs["chat_handler"] = self.chat_handler
            llm_kwargs["image_min_tokens"] = 1024
            llm_kwargs["image_max_tokens"] = resolved.image_max_tokens

        print(
            f"[LLM_Prompt] Loading: {model_path.name} "
            f"(device={device_kind}, gpu_layers={n_gpu_layers}, "
            f"ctx={resolved.context_length}, thinking=off)"
        )

        llm_kwargs_filtered = _filter_kwargs(
            getattr(Llama, "__init__", Llama), llm_kwargs
        )

        # Warn if vision handler can't be passed
        if has_mmproj and self.chat_handler and "chat_handler" not in llm_kwargs_filtered:
            print(
                "[LLM_Prompt] Warning: llama_cpp.Llama() doesn't accept chat_handler. "
                "Images will be ignored. Update llama-cpp-python."
            )

        self.llm = Llama(**llm_kwargs_filtered)
        self.current_signature = signature
        print(f"[LLM_Prompt] Model loaded: {model_path.name}")

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------

    def _invoke(
        self,
        system_prompt: str,
        user_prompt: str,
        images_b64: list[str],
        max_tokens: int,
        seed: int,
        clean_cfg: CleanConfig | None = None,
    ) -> str:
        """Run inference with the loaded model."""
        # Build messages
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
        result = self.llm.create_chat_completion(
            messages=messages,
            max_tokens=int(max_tokens),
            seed=int(seed),
            stop=["<|im_end|>", "<|im_start|>"],
            **PROMPT_GEN_PARAMS,
        )
        elapsed = max(time.perf_counter() - start, 1e-6)

        # Log performance
        usage = result.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if isinstance(completion_tokens, int) and completion_tokens > 0:
            tok_s = completion_tokens / elapsed
            print(
                f"[LLM_Prompt] Tokens: prompt={prompt_tokens}, "
                f"completion={completion_tokens}, "
                f"time={elapsed:.2f}s, speed={tok_s:.2f} tok/s"
            )

        raw = (result.get("choices") or [{}])[0].get("message", {}).get("content", "")
        cleaned = clean_output(str(raw or ""), clean_cfg or CleanConfig(mode="prompt"))
        return cleaned.strip()

    def _invoke_with_retry(
        self,
        system_prompt: str,
        user_prompt: str,
        images_b64: list[str],
        max_tokens: int,
        seed: int,
    ) -> str:
        """Invoke with planning detection and retry."""
        raw = self._invoke(system_prompt, user_prompt, images_b64, max_tokens, seed)

        # Check if output is just planning/thinking
        if not raw or self._looks_like_planning(raw):
            retry_system = (
                "You are a professional prompt writer.\n"
                "Output ONLY the final prompt text.\n"
                "No analysis, no planning steps, no first-person, no <think>.\n"
                "No bullet points, no headings, no JSON, no markdown fences."
            )
            retry_user = f"Rewrite the following into the final prompt:\n\n{raw}"
            retry = self._invoke(retry_system, retry_user, [], max_tokens, seed + 999)
            if retry and not self._looks_like_planning(retry):
                return retry

        return raw or ""

    @staticmethod
    def _looks_like_planning(text: str) -> bool:
        if not text:
            return False
        return bool(
            re.search(
                r"(?im)^\s*(okay[,.:]?|first[,.:]?|next[,.:]?|then[,.:]?|wait[,.:]?)\b",
                text,
            )
            or re.search(
                r"(?i)\b(i\s+(should|need|must|will|am\s+going\s+to|have\s+to))\b",
                text,
            )
        )

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
        device: str,
        keep_model_loaded: bool,
        seed: int,
        style: str = "",
        width: int = 0,
        height: int = 0,
        image=None,
        video=None,
    ):
        """Generate a prompt using the LLM.

        Combines: system_prompt (from .md) + style + aspect_ratio + user_prompt -> LLM -> cleaned output.
        output_format: "text" for natural language, "json" for structured JSON.
        """
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

        # Add output formatting instruction based on format choice
        if output_format == "json":
            sys_prompt += (
                "\n\nReturn the output as valid JSON only. "
                "No preface, no explanations, no markdown fences, no </think>. "
                "Do not wrap in code blocks."
            )
        else:
            sys_prompt += (
                "\n\nReturn only the final prompt text. "
                "No preface, no explanations, no analysis, no JSON, no markdown fences, no </think>. "
                "Do not write planning steps and do not use first-person."
            )

        # Build user prompt with labeled sections the model can parse
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
            self._load_model(model_name, device)

            if images_b64 and self.chat_handler is None:
                print(
                    "[LLM_Prompt] Warning: images provided but no vision projector loaded. "
                    "Images will be ignored."
                )

            if output_format == "json":
                # For JSON: don't strip JSON wrappers, don't strip planning
                result = self._invoke(
                    system_prompt=sys_prompt,
                    user_prompt=final_user_prompt,
                    images_b64=images_b64 if self.chat_handler is not None else [],
                    max_tokens=max_tokens,
                    seed=seed,
                    clean_cfg=CleanConfig(
                        mode="text",
                        strip_json_wrappers=False,
                        strip_planning=False,
                    ),
                )
            else:
                # For text: full cleaning with planning retry
                result = self._invoke_with_retry(
                    system_prompt=sys_prompt,
                    user_prompt=final_user_prompt,
                    images_b64=images_b64 if self.chat_handler is not None else [],
                    max_tokens=max_tokens,
                    seed=seed,
                )

            return (result,)

        finally:
            if not keep_model_loaded:
                self.clear()
                print("[LLM_Prompt] Model unloaded (keep_model_loaded=False)")


# ---------------------------------------------------------------------------
# ComfyUI registration
# ---------------------------------------------------------------------------

NODE_CLASS_MAPPINGS = {
    "LLMPrompt": LLMPromptNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LLMPrompt": "LLM Prompt",
}
