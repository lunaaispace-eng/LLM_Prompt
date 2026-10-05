"""Director writer service (Director plan, task A4): rewrites the user's words into a prompt for the
chosen target image model, on a local GGUF model, a cloud API or a subscription CLI.

Edit mode (inpaint / edit / outpaint): the writer sees the marked canvas, a close-up of the region
and the references, and is told the target model and how that model will receive the images.
Generate mode (generate / compose, S10): the result being refined (Refine only) and the references;
N variants with five sections each, an exact text quoted verbatim.

The pure parts (maps, context, parsing, overlays) need no ComfyUI. The node modules
(`llm_prompt_node`, `llm_prompt_api_node`) are imported lazily, from the pack this module lives in,
so a write and a graph run share one `_RUNNER_LOCK`.
"""
from __future__ import annotations

import base64
import contextlib
import importlib
import io
import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, ContextManager

from PIL import Image, ImageChops, ImageDraw, ImageFilter

try:  # inside the pack (ComfyUI, tests/_comfy.py)
    from ..luna_imaging import studio as _studio
    from ..luna_imaging.masks import expand_box, fit_mask, mask_bbox
    from ..output_cleaner import split_positive_negative
    from .presets_map import GENERATE_OPERATIONS, preset_for_target
except ImportError:  # imported as a top-level package (pure tests)
    from luna_imaging import studio as _studio
    from luna_imaging.masks import expand_box, fit_mask, mask_bbox
    from output_cleaner import split_positive_negative
    from luna_director.presets_map import GENERATE_OPERATIONS, preset_for_target

LOCAL_GGUF = "Local GGUF"
SUPERGROK = "Grok (SuperGrok)"
SECTIONS = ("subject", "style", "composition", "lighting", "camera")
NEUTRAL_GREY = (128, 128, 128)
MARK_COLOR = (255, 0, 255)
# How long a write waits for the GGUF model when a graph run holds it. Short on purpose: a graph run
# can take minutes, and the panel must say "busy" instead of hanging (carry-over from review).
GGUF_LOCK_WAIT_S = 2.0
# Thinking switch on Gemini 2.5 (budget models). Gemini 3 takes a level instead; the node picks
# the field by family, so both are always passed (Implementation choice).
GEMINI_25_THINKING_BUDGET = 8192
NEGATIVE_OFF_LINE = "Write the positive prompt only; no negative prompt."
CONTACT_SHEET_LINE = "(Your images arrive as one contact sheet; each panel is labelled with its image number.)"
ERROR_CODES = ("busy", "no_key", "cli_missing", "provider", "timeout")


class WriterError(RuntimeError):
    """A named writer failure. `code` is one of ERROR_CODES; the caller keeps the request."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class WriterRequest:
    """One write. `canvas` is None only for generate / compose (S10). For outpaint, `canvas` is the
    grown canvas and `mask` its new area (as `luna_imaging.masks.outpaint_canvas` returns them).
    `refs` holds only the references that are sent (the browser drops those over the model's limit)."""
    provider: str = ""  # "Local GGUF" or an llm_prompt_api_node.PROVIDERS key
    model: str = ""
    preset: str | None = None  # a user-picked preset title wins over the target's default
    target_model: str = ""
    engine: str = "cloud"  # "cloud" | "package" (stage B)
    package_prompt: str | None = None  # carried, unused until B4
    operation: str = "edit"
    request: str = ""
    canvas: Image.Image | None = None
    mask: Image.Image | None = None
    refs: list[tuple[str, Image.Image]] = field(default_factory=list)
    send_canvas: bool = True
    send_mask: bool = True
    send_refs: bool = True
    mask_mode: str = "auto"
    crop_padding: float = 0.25
    thinking: bool = False
    negative: bool = True
    vision_mp: float = 1.0
    timeout: float = 180
    server_url: str = ""
    gguf: dict = field(default_factory=dict)
    pack_images: bool | None = None
    # Generate mode (S10)
    size: tuple[int, int] | None = None
    variants: int = 1
    sections: bool = False
    exact_text: str = ""
    feedback: str = ""
    prior_prompt: str = ""
    result: Image.Image | None = None


@dataclass
class WriterResult:
    positive: str
    negative: str
    log: str
    preset_used: str
    legend: list[str]  # the writer's own images, as listed under YOUR IMAGES
    seconds: float
    variants: list[dict]  # [{positive, negative, sections}]; edit mode: one item


@contextlib.contextmanager
def _serialise(kind: str):
    with _GPU_LOCK:
        yield


_GPU_LOCK = threading.Lock()
# Module hook around every local-GPU write. Serialise-only until B14 adds the VRAM rule.
gpu_guard: Callable[[str], ContextManager] = _serialise


# ---------------------------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------------------------

def is_generate(req: WriterRequest) -> bool:
    return req.operation in GENERATE_OPERATIONS


def _flatten(img: Image.Image) -> Image.Image:
    """RGB, with any transparency composited onto neutral grey (a plain convert would drop the
    alpha and show whatever colour the transparent pixels hold, usually black)."""
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, NEUTRAL_GREY + (255,))
        return Image.alpha_composite(bg, rgba).convert("RGB")
    return img.convert("RGB")


def mark_overlay(canvas: Image.Image, mask: Image.Image, color=MARK_COLOR, alpha: float = 0.35) -> Image.Image:
    """The canvas with the masked region tinted `color` at `alpha` and outlined in `color`."""
    base = _flatten(canvas)
    m = fit_mask(mask, base.size)
    tint = Image.blend(base, Image.new("RGB", base.size, color), alpha)
    out = Image.composite(tint, base, m)
    width = max(2, round(min(base.size) / 256))
    edge = ImageChops.subtract(m, m.filter(ImageFilter.MinFilter(2 * width + 1)))
    out.paste(color, (0, 0) + base.size, edge)
    return out


def contact_sheet(items: list[tuple[str, Image.Image]], max_side: int = 1024) -> Image.Image:
    """All images on one labelled sheet (one image for routes that take few or small images)."""
    n = len(items)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    cell = max_side // max(cols, rows)
    band = max(14, cell // 12)
    sheet = Image.new("RGB", (cols * cell, rows * cell), (32, 32, 32))
    draw = ImageDraw.Draw(sheet)
    for i, (label, img) in enumerate(items):
        x, y = (i % cols) * cell, (i // cols) * cell
        thumb = _flatten(img)
        thumb.thumbnail((cell - 4, cell - band - 4), Image.LANCZOS)
        sheet.paste(thumb, (x + (cell - thumb.width) // 2, y + band + (cell - band - thumb.height) // 2))
        draw.text((x + 4, y + 2), label.split(":")[0], fill=(255, 255, 255))
    return sheet


def _plan(req: WriterRequest):
    """(mode, box) from luna_imaging's own routing; None when no region decision applies."""
    if req.operation not in ("inpaint", "outpaint") or req.canvas is None or req.mask is None:
        return None
    m = fit_mask(req.mask, req.canvas.size)
    return _studio.region_plan(req.target_model, req.operation, m, req.mask_mode, req.crop_padding)


def _ref_name(role: str) -> str:
    return f"reference '{role}'" if role else "reference"


def model_image_legend(req: WriterRequest) -> list[str]:
    """How the target image model will number the images it receives (stage A: cloud targets).
    The region routing comes from luna_imaging.studio.region_plan, the same call studio.run makes."""
    if req.operation == "generate":
        return []
    first: list[str] = []
    if req.canvas is not None and not is_generate(req):
        plan = _plan(req)
        if plan is None:
            first = ["the full picture"]
        elif plan[0] == "native":
            first = ["the full picture with a mask"]
        elif plan[0] == "outpaint":
            first = ["the full picture on a larger canvas, the grey border to be filled"]
        else:
            first = ["the region crop"]
    names = first + [_ref_name(role) for role, _ in req.refs]
    return [f"Image {i} = {name}" for i, name in enumerate(names, 1)]


def _crop_box(req: WriterRequest):
    m = fit_mask(req.mask, req.canvas.size)
    bbox = mask_bbox(m)
    if bbox is None:
        raise ValueError("mask is empty")
    return expand_box(bbox, req.crop_padding, req.canvas.size)


def _vision_items(req: WriterRequest) -> list[tuple[str, Image.Image]]:
    """The writer's images in order, labelled, before downscaling."""
    items: list[tuple[str, Image.Image]] = []
    if is_generate(req):
        if req.result is not None:
            items.append(("the result being refined", req.result))
    else:
        if req.canvas is None:
            raise ValueError(f"{req.operation} needs a canvas")
        region = req.operation in ("inpaint", "outpaint") and req.mask is not None and req.send_mask
        if req.send_canvas:
            if region:
                what = ("the picture on its larger canvas; the new area to fill is tinted magenta and outlined"
                        if req.operation == "outpaint"
                        else "the picture; the region to change is tinted magenta and outlined")
                items.append((what, mark_overlay(req.canvas, req.mask)))
            else:
                items.append(("the picture", req.canvas))
        if region and req.operation == "inpaint":
            items.append(("a close-up of the region, unmarked", _flatten(req.canvas).crop(_crop_box(req))))
    if req.send_refs:
        items += [(_ref_name(role), img) for role, img in req.refs]
    return [(f"Image {i}: {what}", img) for i, (what, img) in enumerate(items, 1)]


def build_context(req: WriterRequest, legend: list[str], model_legend: list[str], *,
                  packed: bool = False, already_used: list[str] | None = None,
                  variants: int | None = None) -> str:
    """The REFERENCE CONTEXT block: values only (the presets' examples teach the rest). The user's
    request is never in it; it goes in as user_prompt."""
    lines = [f"TARGET IMAGE MODEL: {req.target_model} ({req.engine})", f"OPERATION: {req.operation}"]
    lines.append("YOUR IMAGES:")
    if packed and legend:
        lines.append(CONTACT_SHEET_LINE)
    lines += legend or ["none"]
    if model_legend:
        lines.append("THE IMAGE MODEL WILL RECEIVE:")
        lines.append(", ".join(model_legend))
    if is_generate(req):
        lines.append(f"VARIANTS: {variants if variants is not None else max(1, int(req.variants))}")
        if req.sections:
            lines.append("SECTIONS: " + ", ".join(SECTIONS))
        if req.exact_text:
            lines.append(f'EXACT TEXT: "{req.exact_text}"')
        if req.prior_prompt:
            lines.append(f"PREVIOUS PROMPT:\n{req.prior_prompt}")
        if req.feedback:
            lines.append(f"FEEDBACK:\n{req.feedback}")
        if already_used:
            lines.append("ALREADY USED:\n" + "\n".join(f"- {p}" for p in already_used))
    if not req.negative:
        lines.append(NEGATIVE_OFF_LINE)
    return "\n".join(lines)


def assemble_prompt(sections: dict) -> str:
    """Five sections in order, each trimmed, empty ones skipped, a full stop added unless the text
    ends in . ! or ?, joined by one space (the rule of tests/fixtures/director/assemble_cases.json)."""
    parts = []
    for name in SECTIONS:
        text = str((sections or {}).get(name) or "").strip()
        if not text:
            continue
        parts.append(text if text[-1] in ".!?" else text + ".")
    return " ".join(parts)


_VARIANT_RE = re.compile(r"\[VARIANT\s*\d+\]", re.IGNORECASE)
_SECTION_RE = re.compile(r"\[(SUBJECT|STYLE|COMPOSITION|LIGHTING|CAMERA)\]", re.IGNORECASE)


def parse_generate_output(text: str, n: int | None = None) -> list[dict]:
    """`[VARIANT n]` blocks -> [{positive, negative, sections}], at most n (all when None).
    Only a [NEGATIVE] tag splits a generate block; with section tags the positive is assembled from
    them, without tags the text is the prompt and `sections` is None. No variant tags = one variant."""
    text = (text or "").strip()
    if not text:
        return []
    tags = list(_VARIANT_RE.finditer(text))
    if tags:
        blocks = [text[t.end():(tags[i + 1].start() if i + 1 < len(tags) else len(text))]
                  for i, t in enumerate(tags)]
    else:
        blocks = [text]
    out = []
    for block in blocks:
        parts = re.split(r"\[\s*NEGATIVE\s*\]", block.strip(), maxsplit=1, flags=re.IGNORECASE)
        positive, _ = split_positive_negative(parts[0], False)
        negative = parts[1].strip() if len(parts) > 1 else ""
        marks = list(_SECTION_RE.finditer(positive))
        sections = None
        if marks:
            sections = {name: "" for name in SECTIONS}
            for i, mk in enumerate(marks):
                end = marks[i + 1].start() if i + 1 < len(marks) else len(positive)
                sections[mk.group(1).lower()] = " ".join(positive[mk.end():end].split())
            positive = assemble_prompt(sections)
        if positive or negative:
            out.append({"positive": positive.strip(), "negative": negative.strip(), "sections": sections})
    return out if n is None else out[:max(1, int(n))]


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", (text or "").lower()))


def _jaccard(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    if not wa and not wb:
        return 1.0
    return len(wa & wb) / len(wa | wb)


def distinct_variants(prompts: list[str], threshold: float = 0.8) -> bool:
    """True when every pair's lower-cased word-set Jaccard is below `threshold`."""
    return all(_jaccard(prompts[i], prompts[j]) < threshold
               for i in range(len(prompts)) for j in range(i + 1, len(prompts)))


def _keep_distinct(kept: list[dict], new: list[dict], n: int) -> list[dict]:
    for v in new:
        if len(kept) >= n:
            break
        if all(_jaccard(v["positive"], k["positive"]) < 0.8 for k in kept):
            kept.append(v)
    return kept


def classify_error(err: BaseException) -> WriterError:
    """A provider / CLI exception -> a named WriterError with the original message."""
    msg = str(err) or type(err).__name__
    low = msg.lower()
    if "requires an api key" in low:
        code = "no_key"
    elif "cli was not found" in low:
        code = "cli_missing"
    elif isinstance(err, TimeoutError) or "did not answer within" in low or "timed out" in low:
        code = "timeout"
    else:
        code = "provider"
    return WriterError(code, msg)


# ---------------------------------------------------------------------------------------------
# Pack-bound parts (lazy node imports)
# ---------------------------------------------------------------------------------------------

def _pack_module(name: str):
    pkg = (__package__ or "").rpartition(".")[0]
    if not pkg:
        raise WriterError("provider", "the writer needs the LLM_Prompt pack loaded (ComfyUI)")
    return importlib.import_module(f"{pkg}.{name}")


def _presets() -> dict[str, str]:
    """Every loaded preset, re-read from prompts/ (new files appear without a restart)."""
    return _pack_module("llm_prompt_node").load_system_prompts()


def build_vision_inputs(req: WriterRequest) -> list[tuple[str, Image.Image]]:
    """The labelled writer images, each flattened to RGB and downscaled to `req.vision_mp`."""
    shrink = _pack_module("llm_prompt_node")._downscale_pil_to_mp
    return [(label, shrink(_flatten(img), req.vision_mp)) for label, img in _vision_items(req)]


def _thinking_api(provider: str, model: str, on: bool) -> dict:
    """Thinking switch on write_prompt_api (S3). The node's own per-model clamps still apply (Grok
    and some OpenAI models turn "none" into "low"; a CLI's "none" is its lowest effort)."""
    budget_model = provider == "Gemini" and (model or "").lower().startswith("gemini-2.")
    level = "None" if budget_model else ("medium" if on else "None")
    return {"disable_thinking": not on, "reasoning_effort": "medium" if on else "none",
            "gemini_thinking_level": level,
            "gemini_thinking_budget": GEMINI_25_THINKING_BUDGET if (on and budget_model) else 0}


def gguf_kwargs(req: WriterRequest, *, context: str, media: list[dict], width: int, height: int,
                split_output: bool, system_prompt: str = "None", preset_text: str = "") -> dict:
    """`_LLMRunner.generate` kwargs: the GGUF node's input defaults, `req.gguf` over them, then the
    writer's own values (R-1: generate has required parameters with no defaults)."""
    node = _pack_module("llm_prompt_node")
    schema = node.LLMPromptNode.define_schema()
    kw = {i.id: i.default for i in schema.inputs if getattr(i, "default", None) is not None}
    kw.update(req.gguf or {})
    kw.update(model_name=req.model, system_prompt=system_prompt, custom_system_prompt=preset_text,
              user_prompt=req.request, context=context, width=int(width), height=int(height),
              media_override=list(media), split_output=bool(split_output), output_format="text",
              disable_thinking=not req.thinking)
    if req.thinking and kw.get("auto_settings"):
        # auto_settings forces thinking off in the node; apply the family's official sampling here
        # instead, so thinking on keeps the same sampling.
        kw["auto_settings"] = False
        resolved = node._resolve_model_settings((req.model or "").lower()) or {}
        for name in ("temperature", "top_p", "top_k", "min_p", "repetition_penalty", "presence_penalty"):
            if name in resolved:
                kw[name] = resolved[name]
    return node._filter_kwargs_for_callable(node._LLMRunner.generate, kw)


def _png_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _canvas_size(req: WriterRequest) -> tuple[int, int]:
    """0 x 0 for inpaint (a region rewrite gets no composition block); else the real size."""
    if is_generate(req):
        return tuple(int(v) for v in req.size) if req.size else (0, 0)
    if req.operation == "inpaint" or req.canvas is None:
        return 0, 0
    return req.canvas.size


def _call(req: WriterRequest, title: str, preset_text: str, context: str,
          items: list[tuple[str, Image.Image]], split_output: bool) -> tuple[str, str, str]:
    width, height = _canvas_size(req)
    if req.provider == LOCAL_GGUF:
        node = _pack_module("llm_prompt_node")
        media: list[dict] = []
        for label, img in items:
            media += [{"type": "text", "text": label}, node._pil_to_content(img, 0.0)]
        kwargs = gguf_kwargs(req, context=context, media=media, width=width, height=height,
                             split_output=split_output, system_prompt=title, preset_text=preset_text)
        lock = node._RUNNER_LOCK
        if not lock.acquire(timeout=min(float(req.timeout), GGUF_LOCK_WAIT_S)):
            raise WriterError("busy", "GGUF busy: a graph run holds the model — request kept")
        try:
            with gpu_guard("gguf"):
                return node._RUNNER.generate(**kwargs)
        except WriterError:
            raise
        except Exception as e:
            raise classify_error(e) from e
        finally:
            lock.release()

    api = _pack_module("llm_prompt_api_node")
    try:
        return api.write_prompt_api(
            provider=req.provider, model_name=req.model, system_prompt=title,
            custom_system_prompt=preset_text, user_prompt=req.request, context=context,
            width=width, height=height, images_b64=[_png_b64(img) for _, img in items],
            split_output=split_output, timeout_seconds=max(1, int(req.timeout)),
            server_url=req.server_url if req.provider == "Custom" else "",
            **_thinking_api(req.provider, req.model, req.thinking))
    except Exception as e:
        raise classify_error(e) from e


def write(req: WriterRequest) -> WriterResult:
    """Run one write. Raises WriterError (named) on every failure; `req` is never changed."""
    start = time.perf_counter()
    if req.engine != "cloud":
        raise WriterError("provider", f"engine {req.engine!r} is stage B and not built yet — request kept")
    title = req.preset or preset_for_target(req.target_model, req.operation)
    if not title:
        raise WriterError("provider", f"no writer preset for target model {req.target_model!r} — pick one")
    presets = _presets()
    if title not in presets:
        raise WriterError("provider", f"preset missing: {title}")
    preset_text = presets[title]

    items = build_vision_inputs(req)
    legend = [label for label, _ in items]
    model_legend = model_image_legend(req)
    pack = req.pack_images if req.pack_images is not None else req.provider == SUPERGROK
    sent = ([(f"Contact sheet of Images 1-{len(items)}, each panel labelled", contact_sheet(items))]
            if (pack and items) else items)
    ctx = dict(packed=bool(pack and items))

    if not is_generate(req):
        positive, negative, log = _call(req, title, preset_text,
                                        build_context(req, legend, model_legend, **ctx), sent, True)
        negative = negative if req.negative else ""
        return WriterResult(positive, negative, log, title, legend, time.perf_counter() - start,
                            [{"positive": positive, "negative": negative, "sections": None}])

    n = max(1, int(req.variants))
    raw, _, log = _call(req, title, preset_text, build_context(req, legend, model_legend, **ctx), sent, False)
    kept = _keep_distinct([], parse_generate_output(raw, n), n)
    if n > 1 and len(kept) < n:
        retry_ctx = build_context(req, legend, model_legend, already_used=[v["positive"] for v in kept],
                                  variants=n - len(kept), **ctx)
        raw2, _, log2 = _call(req, title, preset_text, retry_ctx, sent, False)
        kept = _keep_distinct(kept, parse_generate_output(raw2), n)
        log = "\n".join(p for p in (log, log2, "retried once for distinct variants") if p)
    if len(kept) < n:
        raise WriterError("provider", f"writer returned {len(kept)} distinct prompts of {n}")
    if not req.negative:
        kept = [dict(v, negative="") for v in kept]
    first = kept[0]
    return WriterResult(first["positive"], first["negative"], log, title, legend,
                        time.perf_counter() - start, kept)
