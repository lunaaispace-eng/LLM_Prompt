"""LLM Prompt — ComfyUI node for prompt generation via GGUF vision models.

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
from huggingface_hub import hf_hub_download
from llama_cpp import Llama
from PIL import Image

import folder_paths

# Bundled output cleaner — no external dependencies
from .output_cleaner import OutputCleanConfig, clean_model_output

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NODE_DIR = Path(__file__).parent
PROMPTS_DIR = NODE_DIR / "prompts"
GGUF_CONFIG_PATH = NODE_DIR / "gguf_models.json"

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
# Model catalog (same structure as QwenVL-Mod gguf_models.json)
# ---------------------------------------------------------------------------

def _load_gguf_vl_catalog():
    if not GGUF_CONFIG_PATH.exists():
        return {"base_dir": "LLM/GGUF", "models": {}}
    try:
        with open(GGUF_CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except Exception as exc:
        print(f"[LLM_Prompt] gguf_models.json load failed: {exc}")
        return {"base_dir": "LLM/GGUF", "models": {}}

    base_dir = data.get("base_dir") or "LLM/GGUF"
    flattened: dict[str, dict] = {}

    # Parse qwenVL_model repos (vision models) — may be at root or nested under "models"
    raw_models = data.get("models") or {}
    repos = raw_models.get("qwenVL_model") or data.get("qwenVL_model") or data.get("vl_repos") or {}
    seen: set[str] = set()
    for repo_key, repo in repos.items():
        if not isinstance(repo, dict):
            continue
        author = repo.get("author") or repo.get("publisher")
        repo_name = repo.get("repo_name") or repo.get("repo_name_override") or repo_key
        repo_id = repo.get("repo_id") or (f"{author}/{repo_name}" if author and repo_name else None)
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

    # Also parse text-only Qwen_model if present
    text_repos = raw_models.get("Qwen_model") or data.get("Qwen_model") or {}
    if isinstance(text_repos, dict):
        for repo_key, repo in text_repos.items():
            if not isinstance(repo, dict):
                continue
            author = repo.get("author") or repo.get("publisher")
            repo_name = repo.get("repo_name") or repo.get("repo_name_override") or repo_key
            repo_id = repo.get("repo_id")
            alt_repo_ids = repo.get("alt_repo_ids") or []
            defaults = repo.get("defaults") if isinstance(repo.get("defaults"), dict) else {}
            model_files = repo.get("model_files") or []
            for model_file in model_files:
                display = Path(model_file).name
                if display in seen:
                    display = f"{display} ({repo_key})"
                seen.add(display)
                entry = dict(defaults)
                entry.update({
                    "author": author,
                    "repo_dirname": repo_name,
                    "repo_id": repo_id,
                    "alt_repo_ids": alt_repo_ids,
                    "filename": model_file,
                })
                flattened[display] = entry

    return {"base_dir": base_dir, "models": flattened}


GGUF_VL_CATALOG = _load_gguf_vl_catalog()

# ---------------------------------------------------------------------------
# Helpers (same as QwenVL-Mod)
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


def _download_single_file(repo_ids: list[str], filename: str, target_path: Path):
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
# Model entry resolution (same as QwenVL-Mod)
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
    all_models = GGUF_VL_CATALOG.get("models") or {}
    entry = all_models.get(model_name) or {}

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
        n_batch=_int("n_batch", 512),
        gpu_layers=_int("gpu_layers", -1),
        top_k=_int("top_k", 0),
        pool_size=_int("pool_size", 4194304),
    )


def _looks_like_reasoning(text: str) -> bool:
    """Detect if model output is reasoning/planning instead of a final prompt.

    Catches: numbered lists, markdown bold headers, bullet analysis,
    first-person planning, and thinking patterns.
    """
    if not text:
        return False
    # Standard planning: "I should", "First,", "Let me", etc.
    if re.search(r"(?i)\b(i\s+(should|need|must|will|am\s+going\s+to|have\s+to))\b", text):
        return True
    if re.search(r"(?im)^\s*(okay[,.:]?|first[,.:]?|next[,.:]?|then[,.:]?|wait[,.:]?|let me)\b", text):
        return True
    # Numbered analysis steps: "1. **Subject:**" or "2. Deconstruct"
    numbered = len(re.findall(r"(?m)^\s*\d+\.\s+\*{0,2}[A-Z]", text))
    if numbered >= 2:
        return True
    # Markdown bold headers: "**Subject:**" "**Lighting:**"
    bold_headers = len(re.findall(r"(?m)\*{1,2}[A-Za-z][^*]+\*{1,2}\s*:", text))
    if bold_headers >= 2:
        return True
    # Bullet point analysis
    bullets = len(re.findall(r"(?m)^\s*[-*]\s+", text))
    if bullets >= 3:
        return True
    return False


# ---------------------------------------------------------------------------
# The Node — built on QwenVL-Mod's proven inference code
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

    @classmethod
    def INPUT_TYPES(cls):
        all_models = GGUF_VL_CATALOG.get("models") or {}
        model_keys = sorted(all_models.keys()) or ["(add models to gguf_models.json)"]

        prompt_presets = list(SYSTEM_PROMPTS.keys()) if SYSTEM_PROMPTS else ["(add .md files to prompts/)"]

        num_gpus = torch.cuda.device_count()
        gpu_list = [f"cuda:{i}" for i in range(num_gpus)]
        device_options = ["auto", "cpu", "mps"] + gpu_list

        return {
            "required": {
                "model_name": (model_keys, {
                    "default": model_keys[0],
                    "tooltip": "GGUF model from gguf_models.json catalog.",
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
                "english_output": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "Force final output in English using translation prompt.",
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
            },
            "optional": {
                "style": ("STRING", {
                    "forceInput": True,
                    "tooltip": "Style description from external node.",
                }),
                "width": ("INT", {
                    "forceInput": True,
                    "tooltip": "Image width — auto-detected as aspect ratio.",
                }),
                "height": ("INT", {
                    "forceInput": True,
                    "tooltip": "Image height — auto-detected as aspect ratio.",
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
        gc.collect()

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()

    # ------------------------------------------------------------------
    # Model loading (same as QwenVL-Mod GGUF)
    # ------------------------------------------------------------------

    def _load_model(self, model_name: str, device: str):
        resolved = _resolve_model(model_name)
        base_dir = _resolve_base_dir(GGUF_VL_CATALOG.get("base_dir") or "LLM/GGUF")

        author_dir = _safe_dirname(resolved.author or "")
        repo_dir = _safe_dirname(resolved.repo_dirname)
        target_dir = base_dir / author_dir / repo_dir

        model_path = target_dir / Path(resolved.model_filename).name
        mmproj_path = (
            target_dir / Path(resolved.mmproj_filename).name
            if resolved.mmproj_filename else None
        )

        repo_ids = [r for r in [resolved.repo_id] + resolved.alt_repo_ids if r]

        if not model_path.exists():
            if repo_ids:
                _download_single_file(repo_ids, resolved.model_filename, model_path)
            else:
                raise FileNotFoundError(f"[LLM_Prompt] Model not found: {model_path}")

        if mmproj_path and not mmproj_path.exists():
            if repo_ids and resolved.mmproj_filename:
                _download_single_file(repo_ids, resolved.mmproj_filename, mmproj_path)
            else:
                raise FileNotFoundError(f"[LLM_Prompt] mmproj not found: {mmproj_path}")

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
            return

        self.clear()
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            time.sleep(0.1)

        # Load vision handler (same as QwenVL-Mod)
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
                        "[LLM_Prompt] No Qwen VL chat handler found in llama_cpp."
                    )

            mmproj_kwargs = {
                "clip_model_path": str(mmproj_path),
                "image_max_tokens": resolved.image_max_tokens,
                "force_reasoning": False,
                "verbose": False,
            }
            mmproj_kwargs = _filter_kwargs_for_callable(
                getattr(handler_cls, "__init__", handler_cls), mmproj_kwargs
            )
            self.chat_handler = handler_cls(**mmproj_kwargs)

        # Load LLM (same kwargs as QwenVL-Mod)
        llm_kwargs = {
            "model_path": str(model_path),
            "n_ctx": resolved.context_length,
            "n_gpu_layers": n_gpu_layers,
            "n_batch": resolved.n_batch,
            "swa_full": True,
            "verbose": False,
            "pool_size": resolved.pool_size,
            "top_k": resolved.top_k,
            # Qwen 3.5: disable thinking mode at template level
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if has_mmproj and self.chat_handler is not None:
            llm_kwargs["chat_handler"] = self.chat_handler
            llm_kwargs["image_min_tokens"] = 1024
            llm_kwargs["image_max_tokens"] = resolved.image_max_tokens

        print(
            f"[LLM_Prompt] Loading: {model_path.name} "
            f"(device={device_kind}, gpu_layers={n_gpu_layers}, ctx={resolved.context_length})"
        )

        llm_kwargs_filtered = _filter_kwargs_for_callable(
            getattr(Llama, "__init__", Llama), llm_kwargs
        )

        if has_mmproj and self.chat_handler and "chat_handler" not in llm_kwargs_filtered:
            print("[LLM_Prompt] Warning: Llama() doesn't accept chat_handler. Images will be ignored.")

        self.llm = Llama(**llm_kwargs_filtered)
        self.current_signature = signature
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
        """Run inference — same call signature as QwenVL-Mod GGUF."""
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
            temperature=float(temperature),
            top_p=float(top_p),
            repeat_penalty=float(repetition_penalty),
            seed=int(seed),
            stop=["<|im_end|>", "<|im_start|>"],
        )
        elapsed = max(time.perf_counter() - start, 1e-6)

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
        cleaned = clean_model_output(str(raw or ""), OutputCleanConfig(mode="text"))
        return cleaned.strip()

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
        """Invoke with planning/reasoning detection and retry."""

        raw = self._invoke(system_prompt, user_prompt, images_b64, max_tokens, temperature, top_p, repetition_penalty, seed)
        cleaned = clean_model_output(raw, OutputCleanConfig(mode="prompt"))

        if not cleaned or _looks_like_reasoning(raw) or "<think" in raw.lower():
            retry_system = (
                "You are a professional prompt writer.\n"
                "Output ONLY ONE final prompt paragraph.\n"
                "No analysis, no planning steps, no first-person, and no <think>.\n"
                "No bullet points, no headings, no JSON, no markdown, no quotes."
            )
            retry_user = f"Rewrite the following into the final prompt paragraph:\n\n{raw}"
            raw_retry = self._invoke(retry_system, retry_user, [], max_tokens, 0.4, 0.95, 1.05, seed + 999)
            cleaned_retry = clean_model_output(raw_retry, OutputCleanConfig(mode="prompt"))
            if cleaned_retry and not _looks_like_reasoning(cleaned_retry):
                return cleaned_retry

        return cleaned or ""

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
        english_output: bool,
        device: str,
        keep_model_loaded: bool,
        seed: int,
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
        else:
            sys_prompt += (
                "\n\nReturn only the final prompt text. "
                "No preface, no explanations, no analysis, no JSON, no markdown fences, and no </think>.\n"
                "Do not write planning steps (no 'First', 'Next', 'Then') and do not use first-person ('I', 'we')."
            )

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
            self._load_model(model_name, device)

            if images_b64 and self.chat_handler is None:
                print("[LLM_Prompt] Warning: images provided but no vision projector. Images will be ignored.")

            if output_format == "json":
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

            # English output: translate if needed
            if english_output and result:
                result = self._invoke(
                    system_prompt=(
                        "Return a single English paragraph (150-300 words). No prefixes, bullets, JSON, or </think>. "
                        "Cover subject, environment, lighting, camera settings, composition, color/texture, and style. "
                        "Output only the prompt."
                    ),
                    user_prompt=result,
                    images_b64=[],
                    max_tokens=max_tokens,
                    temperature=0.3,
                    top_p=0.95,
                    repetition_penalty=1.05,
                    seed=seed + 1,
                )
                result = clean_model_output(result, OutputCleanConfig(mode="prompt"))

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
