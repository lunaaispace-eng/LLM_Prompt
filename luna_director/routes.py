"""Luna Director HTTP routes (plan task A6): writer, cloud runs, Generate batches, assets, resize, history.

Handlers parse and validate the body, then run the blocking work (writer calls, Pillow, the ledger) in the
default executor. Errors are always `{"error": {"code", "message"}}`: 409 for a busy writer, 400 for a
refusal (a named writer failure, a bad ref, a bad resize state, a bad body), 500 for anything else. Keys
never appear in a response: the job runner scrubs them and the writer never returns them.

`install(server)` wires the routes into ComfyUI's PromptServer; the routes themselves only see `Deps`, so the
tests run them on a plain aiohttp app with fakes.
"""
from __future__ import annotations

import asyncio
import dataclasses
import importlib
import io
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from aiohttp import web
from PIL import Image, ImageOps

try:  # inside the pack (ComfyUI, tests/_comfy.py)
    from ..luna_imaging.capabilities import caps_for, provider_for
    from ..luna_imaging.masks import outpaint_canvas
    from ..luna_imaging.resize import parse_state, ratio_presets
    from ..luna_imaging.providers import gemini as _gemini
    from ..luna_imaging.providers import xai as _xai
except ImportError:  # imported as a top-level package (pure tests)
    from luna_imaging.capabilities import caps_for, provider_for
    from luna_imaging.masks import outpaint_canvas
    from luna_imaging.resize import parse_state, ratio_presets
    from luna_imaging.providers import gemini as _gemini
    from luna_imaging.providers import xai as _xai

from . import jobs as jobs_mod
from .presets_map import EDIT_REWRITE, GENERATE, preset_for_target
from .store import RefError, ResizeStateError, resolve_ref

log = logging.getLogger("luna_director.routes")

# The write body (plan A6, pre-flight P10; A9 buildWriteBody sends exactly these). `outpaint` (margins left,
# top, right, bottom) is the one addition: an outpaint write needs the grown canvas, as the run does
# (Implementation choice, recorded in the ledger).
WRITE_KEYS = ("canvas", "mask", "refs", "provider", "model", "preset", "target_model", "operation", "request",
              "send", "thinking", "negative", "mask_mode", "crop_padding", "vision_mp", "server_url", "gguf",
              "size", "variants", "sections", "exact_text", "feedback", "prior_prompt", "result", "outpaint")
WRITER_CODES_400 = ("no_key", "cli_missing", "provider", "timeout", "refused")
FIXED_SIZE_ASPECTS = ["1:1", "2:3", "3:2"]   # GPT Image 1.x sizes (1024x1024, 1024x1536, 1536x1024)
BACKGROUNDS = ["auto", "opaque", "transparent"]


@dataclass
class Deps:
    store: object
    jobs: object                     # jobs.JobRunner
    writer: object                   # the luna_director.writer module (or a fake with the same names)
    dirs: dict                       # ref type -> folder ("input", "output", "temp")
    emit: Callable | None = None
    settings: dict = field(default_factory=dict)   # {} in stage A
    packages_root: Path | None = None              # stage B (B5)
    registry: object = None                        # stage B (B5)


class _Refused(ValueError):
    """A bad request body; 400 `refused`."""


def _err(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": {"code": code, "message": _scrub(message)}}, status=status)


def _scrub(message: str) -> str:
    api = _pack("llm_prompt_api_node")
    names = {"OPENAI_API_KEY", "XAI_API_KEY"}
    for cfg in api.PROVIDERS.values():
        env = cfg.get("env_var")
        names.update(env if isinstance(env, list) else [env] if env else [])
    file_keys = api._load_env_file_keys()
    keys = {value.strip() for name in names for value in (os.environ.get(name), file_keys.get(name))
            if value and value.strip()}
    for key in sorted(keys, key=len, reverse=True):
        message = message.replace(key, "[key removed]")
    return re.sub(r"sk-[A-Za-z0-9_\-]{12,}|xai-[A-Za-z0-9_\-]{12,}|AIza[0-9A-Za-z_\-]{20,}",
                  "[key removed]", message)


def _error_response(exc: BaseException, writer_error=None) -> web.Response:
    if writer_error is not None and isinstance(exc, writer_error):
        code = getattr(exc, "code", "provider")
        if code == "busy":
            return _err(409, code, str(exc))
        return _err(400 if code in WRITER_CODES_400 else 500, code, str(exc))
    if isinstance(exc, RefError):
        return _err(400, "bad_ref", str(exc))
    if isinstance(exc, ResizeStateError):
        return _err(400, "bad_state", str(exc))
    if isinstance(exc, (_Refused, ValueError, KeyError, TypeError)):
        msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        return _err(400, "refused", str(msg))
    log.error("director route failed: %s", _scrub(f"{type(exc).__name__}: {exc}"))
    return _err(500, "internal", f"{type(exc).__name__}: {exc}")


async def _json(request: web.Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise _Refused("body must be JSON") from None
    if not isinstance(body, dict):
        raise _Refused("body must be a JSON object")
    return body


def _need_str(body: dict, key: str) -> str:
    v = body.get(key)
    if not isinstance(v, str) or not v:
        raise _Refused(f"{key} is required")
    return v


def _pack(name: str):
    pkg = (__package__ or "").rpartition(".")[0]
    return importlib.import_module(f"{pkg}.{name}" if pkg else name)


# ---------------------------------------------------------------- config

def _aspects(model: str) -> list[str]:
    provider = provider_for(model)
    if provider == "gemini":
        return list(_gemini.ASPECTS)
    if provider == "xai":
        return list(_xai.ASPECTS)
    if caps_for(model).free_size:
        return [a for a in _pack("openai_image_node").ASPECT_RATIOS if a != "auto"]
    return list(FIXED_SIZE_ASPECTS)


def model_info(model: str) -> dict:
    """One cloud model as the browser needs it; every value comes from the core or A5 (no copies)."""
    caps = caps_for(model)
    opts = jobs_mod.options_for(model)
    aspects = _aspects(model)
    options = {"aspect_ratio": ["auto", *aspects], "resolution": opts["resolution"]}
    controls = ["aspect_ratio", "resolution"]
    if opts["quality"]:
        controls.append("quality")
        options["quality"] = opts["quality"]
    if caps.transparent:
        controls.append("background")
        options["background"] = list(BACKGROUNDS)
    controls.append("seed")
    options["seed"] = {"sent": False}   # Peter: "keep seed"; no cloud provider in the core sends one
    return {
        "id": model,
        "ref_limit": {op: jobs_mod.ref_limit(model, op) for op in jobs_mod.OPERATIONS},
        "controls": controls,
        "options": options,
        "default_preset": preset_for_target(model),
        "generate_preset": preset_for_target(model, "generate"),
        "ratio_presets": ratio_presets(aspects),
        "draft": jobs_mod.draft_params(model, {}),
        "final_options": jobs_mod.final_options(model),
    }


def cloud_models(models: list[str]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for m in dict.fromkeys(models):
        groups.setdefault(provider_for(m), []).append(model_info(m))
    return [{"provider": p, "models": ms} for p, ms in groups.items()]


def build_config(deps: Deps) -> dict:
    node = _pack("llm_prompt_node")
    api = _pack("llm_prompt_api_node")
    edit_titles = set(EDIT_REWRITE.values())
    gen_titles = set(GENERATE.values())
    presets = [{"title": t, "edit_rewrite": t in edit_titles, "generate": t in gen_titles}
               for t in sorted(deps.writer._presets())]
    return {
        "writers": {"Local GGUF": sorted(node._refresh_model_list()), "providers": list(api.PROVIDERS)},
        "presets": presets,
        "engines": ["cloud"],
        "settings": dict(deps.settings),
        "cloud": cloud_models(list(_pack("luna_image_studio_node").MODELS)),
    }


# ---------------------------------------------------------------- images

def _open(ref, dirs, *, mode=None) -> Image.Image:
    path = resolve_ref(ref, dirs)
    if not path.is_file():
        raise RefError("ref does not exist")
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            return im.convert(mode) if mode else im.copy()
    except Exception:
        raise RefError("not an image") from None


def _margins(v) -> list[int]:
    if not isinstance(v, (list, tuple)) or len(v) != 4:
        raise _Refused("outpaint must be four margins (left, top, right, bottom)")
    out = []
    for x in v:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0:
            raise _Refused("outpaint margins must be numbers >= 0")
        out.append(int(x))
    return out


def build_writer_request(writer, body: dict, dirs: dict):
    """The write body -> `writer.WriterRequest`, every ref opened as an image."""
    unknown = set(body) - set(WRITE_KEYS)
    if unknown:
        raise _Refused(f"unknown write keys: {', '.join(sorted(unknown))}")
    send = body.get("send") or {}
    if not isinstance(send, dict):
        raise _Refused("send must be {canvas, mask, refs}")
    refs = body.get("refs") or []
    if not isinstance(refs, list) or not all(isinstance(r, dict) and isinstance(r.get("ref"), dict) for r in refs):
        raise _Refused("refs must be a list of {role, ref}")
    canvas = _open(body["canvas"], dirs) if body.get("canvas") else None
    mask = _open(body["mask"], dirs, mode="L") if body.get("mask") else None
    if body.get("outpaint") is not None:
        if canvas is None:
            raise _Refused("outpaint needs a canvas")
        canvas, mask = outpaint_canvas(canvas, _margins(body["outpaint"]))
    size = body.get("size")
    if size is not None:
        if not isinstance(size, (list, tuple)) or len(size) != 2:
            raise _Refused("size must be [width, height] or null")
        size = (int(size[0]), int(size[1]))
    kw = dict(
        provider=body.get("provider") or "", model=body.get("model") or "", preset=body.get("preset") or None,
        target_model=body.get("target_model") or "", operation=body.get("operation") or "edit",
        request=body.get("request") or "", canvas=canvas, mask=mask,
        refs=[(str(r.get("role") or ""), _open(r["ref"], dirs)) for r in refs],
        send_canvas=bool(send.get("canvas", True)), send_mask=bool(send.get("mask", True)),
        send_refs=bool(send.get("refs", True)), thinking=bool(body.get("thinking", False)),
        negative=bool(body.get("negative", True)), server_url=body.get("server_url") or "",
        gguf=body.get("gguf") or {}, size=size, variants=int(body.get("variants") or 1),
        sections=bool(body.get("sections", False)), exact_text=body.get("exact_text") or "",
        feedback=body.get("feedback") or "", prior_prompt=body.get("prior_prompt") or "",
        result=_open(body["result"], dirs) if body.get("result") else None,
    )
    for key in ("mask_mode", "crop_padding", "vision_mp"):
        if body.get(key) is not None:
            kw[key] = body[key]
    return writer.WriterRequest(**kw)


# ---------------------------------------------------------------- routes

def register_routes(routes: web.RouteTableDef, deps: Deps) -> None:
    writer_error = getattr(deps.writer, "WriterError", None)

    async def run(fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    def handler(fn):
        async def wrapped(request):
            try:
                return await fn(request)
            except Exception as e:  # every failure becomes {"error": {...}}
                return _error_response(e, writer_error)
        return wrapped

    @routes.get("/luna/director/config")
    @handler
    async def config(request):
        return web.json_response(await run(build_config, deps))

    @routes.post("/luna/director/write")
    @handler
    async def write(request):
        body = await _json(request)

        def go():
            res = deps.writer.write(build_writer_request(deps.writer, body, deps.dirs))
            return dataclasses.asdict(res) if dataclasses.is_dataclass(res) else dict(res)
        return web.json_response(await run(go))

    @routes.post("/luna/director/asset")
    @handler
    async def asset(request):
        if request.content_type.startswith("multipart/"):
            form = await request.post()
            project, image = form.get("project"), form.get("image")
            if not isinstance(project, str) or image is None or not hasattr(image, "file"):
                raise _Refused("multipart needs project and image")
            data = image.file.read()
        else:
            body = await _json(request)
            project = _need_str(body, "project")
            src = body.get("from_ref")
            if not isinstance(src, dict):
                raise _Refused("from_ref is required")
            data = None

        def go():
            raw = data if data is not None else resolve_ref(src, deps.dirs).read_bytes()
            return {"ref": deps.store.import_asset(project, raw)}
        return web.json_response(await run(go))

    @routes.post("/luna/director/resize")
    @handler
    async def resize(request):
        body = await _json(request)
        project = _need_str(body, "project")
        refs = body.get("refs")
        if not isinstance(refs, list) or not refs:
            raise _Refused("refs must list at least one ref")
        state = body.get("state") or {}
        if not isinstance(state, dict):
            raise _Refused("state must be {all?, items?}")
        dry = bool(body.get("dry_run", False))
        items = parse_state(state, len(refs))

        def go():
            return {"items": [deps.store.resize_asset(project, r, it, deps.dirs, dry_run=dry)
                              for r, it in zip(refs, items)]}
        return web.json_response(await run(go))

    @routes.post("/luna/studio/run")
    @handler
    async def studio_run(request):
        body = await _json(request)
        sid, project = body.get("sid") or "", _need_str(body, "project")
        return web.json_response(await run(deps.jobs.submit_cloud, sid, project, body.get("spec")))

    @routes.post("/luna/studio/cancel")
    @handler
    async def cancel(request):
        body = await _json(request)
        if body.get("batch_id"):
            return web.json_response({"ok": True, "cancelled": deps.jobs.cancel_batch(str(body["batch_id"]))})
        return web.json_response({"ok": deps.jobs.cancel(_need_str(body, "job_id"))})

    @routes.post("/luna/studio/batch")
    @handler
    async def batch(request):
        body = await _json(request)
        sid, project = body.get("sid") or "", _need_str(body, "project")
        return web.json_response(await run(deps.jobs.submit_batch, sid, project, body.get("batch")))

    @routes.post("/luna/studio/estimate")
    @handler
    async def estimate(request):
        body = await _json(request)

        def go():
            if body.get("batch") is not None:
                return jobs_mod.estimate_batch(body["batch"])
            spec = jobs_mod.normalize_spec(body.get("spec"))
            one = jobs_mod.estimate_cloud_cost(spec)
            return {"total": one or 0.0, "per_model": [{"model": spec["model"], "est_cost_usd": one}],
                    "unknown": [] if one is not None else [spec["model"]]}
        return web.json_response(await run(go))

    @routes.post("/luna/studio/final")
    @handler
    async def final(request):
        body = await _json(request)
        sid, project = body.get("sid") or "", _need_str(body, "project")
        entry_id = _need_str(body, "entry_id")
        return web.json_response(await run(deps.jobs.submit_final, sid, project, entry_id, body.get("choice")))

    @routes.get("/luna/studio/jobs")
    @handler
    async def jobs_list(request):
        return web.json_response({"jobs": deps.jobs.jobs_for(request.query.get("sid", ""))})

    @routes.get("/luna/director/history")
    @handler
    async def history(request):
        project = request.query.get("project") or ""
        try:
            limit = int(request.query.get("limit", "200"))
        except ValueError:
            raise _Refused("limit must be a whole number") from None

        def go():
            return {"entries": deps.store.list_history(project, limit),
                    "day_cost": deps.store.day_cost(date.today().isoformat())}
        return web.json_response(await run(go))

    @routes.get("/luna/director/history/{id}")
    @handler
    async def history_get(request):
        project, eid = request.query.get("project") or "", request.match_info["id"]
        entry = await run(deps.store.get_entry, project, eid)
        if entry is None:
            raise _Refused(f"no history entry {eid!r} in project {project!r}")
        return web.json_response(entry)

    @routes.post("/luna/director/history/{id}")
    @handler
    async def history_patch(request):
        body = await _json(request)
        project = _need_str(body, "project")
        patch = body.get("patch")
        if not isinstance(patch, dict):
            raise _Refused("patch must be an object")
        return web.json_response(await run(deps.store.update_history, project, request.match_info["id"], patch))


# ---------------------------------------------------------------- ComfyUI wiring

def install(server) -> None:
    """Build `Deps` from ComfyUI and add the routes to `server`. Called once from the pack's __init__."""
    import folder_paths

    from . import writer
    from .store import Store

    dirs = {"input": folder_paths.get_input_directory(), "output": folder_paths.get_output_directory(),
            "temp": folder_paths.get_temp_directory()}
    store = Store(Path(dirs["output"]) / "luna_director", Path(dirs["input"]) / "luna_director")
    studio_node = _pack("luna_image_studio_node")

    def emit(event, data, sid):
        server.send_sync(event, data, sid)

    runner = jobs_mod.JobRunner(emit, store, studio_node._resolve_key, lambda ref: _open(ref, dirs))
    # PromptServer.routes is added to the app after the custom nodes load, as every pack's routes are.
    register_routes(server.routes, Deps(store=store, jobs=runner, writer=writer, dirs=dirs, emit=emit))
