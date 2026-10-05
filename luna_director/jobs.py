"""Luna Director cloud jobs: a small worker pool on `luna_imaging.studio.run`.

Every job emits `luna.job` events to its sid in the order queued -> running -> done | error | cancelled, and every
job that ran is written to the project's history through `Store` (the paid image saved first, then the entry).
Generate (S10) expands a batch into one job per model x image, and Final re-renders a chosen draft as a child
entry: an edit with the draft as Image 1 on every model (Peter 2026-10-04, "Draft as reference").

Cancel (Peter's S5 approval, the cancelled-run ruling): a queued job is never sent and writes no entry; a running
job is not aborted - when its call returns, the paid image is kept and the entry is marked `cancelled` with its
cost, so it counts in `Store.day_cost`. A cancelled call that returns an error is an `error` entry.

Keys come only from the injected `resolve_key` and are passed straight to `studio.run`; they never reach an
event, an entry or a log line. ComfyUI-free: `luna_imaging` + `luna_director.store` only.
"""
from __future__ import annotations

import concurrent.futures
import logging
import math
import re
import threading
import time
import uuid
from typing import Callable

try:  # inside the pack (ComfyUI, tests/_comfy.py)
    from ..luna_imaging import studio
    from ..luna_imaging.capabilities import caps_for, provider_for
    from ..luna_imaging.cost import gemini_cost, openai_estimate
    from ..luna_imaging.http import ProviderError
    from ..luna_imaging.providers import gemini as _gemini
    from ..luna_imaging.providers import openai as _openai
    from ..luna_imaging.providers import xai as _xai
    from ..luna_imaging.sizes import openai_size
    from ..luna_imaging.types import EditRequest
except ImportError:  # imported as a top-level package (pure tests)
    from luna_imaging import studio
    from luna_imaging.capabilities import caps_for, provider_for
    from luna_imaging.cost import gemini_cost, openai_estimate
    from luna_imaging.http import ProviderError
    from luna_imaging.providers import gemini as _gemini
    from luna_imaging.providers import openai as _openai
    from luna_imaging.providers import xai as _xai
    from luna_imaging.sizes import openai_size
    from luna_imaging.types import EditRequest

from .store import ENTRY_KEYS

log = logging.getLogger("luna_director.jobs")

EVENT = "luna.job"
ENGINE = "cloud"
OPERATIONS = ("generate", "edit", "compose", "inpaint", "outpaint")
TIERS = ("draft", "final")
MAX_MODELS = 4   # models per Generate batch (Implementation choice)

# Wording PROPOSED; the A18a check settles it.
FINAL_LEAD = ("Re-render Image 1 at full detail and resolution. Keep its composition, subjects, colours and any "
              "text exactly as they are.")

# Engine settings: recorded in the entry's `params` (seed has its own entry key; luna_imaging sends no seed).
PARAM_KEYS = ("aspect_ratio", "resolution", "quality", "background", "mask_mode", "crop_padding", "feather_px",
              "outpaint", "n")
SPEC_KEYS = ("model", "operation", "prompt", "negative", "request", "writer", "image", "mask", "refs", "parent",
             *PARAM_KEYS, "seed", "batch", "variant", "tier", "exact_text", "sections")
BATCH_KEYS = ("models", "count", "variants_mode", "prompts", "tier", "refs", "exact_text", "request", "writer",
              "parent", "aspect_ratio", "resolution", "quality", "background", "seed")
_PROMPT_KEYS = ("prompt", "negative", "sections")
_DEFAULTS = {"aspect_ratio": "auto", "resolution": "1K", "quality": "auto", "background": "auto",
             "mask_mode": "auto", "crop_padding": 0.25, "feather_px": 16, "outpaint": None, "n": 1}
_MODE_RE = re.compile(r"^mode\s*:\s*(native|crop)\s*$")


# ---------------------------------------------------------------- per-model rules (all read from the core)

def ref_limit(model: str, operation: str) -> int:
    """How many references `model` takes for `operation` (pre-flight P20): generate 0, compose `max_inputs`
    (every image is a reference), otherwise `max_inputs - 1` (the first image is the picture)."""
    if operation == "generate":
        return 0
    n = caps_for(model).max_inputs
    return n if operation == "compose" else max(0, n - 1)


def auto_operation(model: str, n_refs: int) -> str:
    """Feature 7: compose when references are given and the model takes any, else generate."""
    return "compose" if n_refs > 0 and ref_limit(model, "compose") > 0 else "generate"


def _k(label: str) -> float:
    try:
        return float(str(label).upper().rstrip("K"))
    except ValueError:
        return 0.0


def options_for(model: str) -> dict:
    """The quality and resolution values the core offers for `model`, cheapest first ("auto" aside), from the
    provider modules' own lists."""
    provider = provider_for(model)
    if provider == "openai":
        return {"quality": _openai.qualities_for(model), "resolution": _openai.resolutions_for(model)}
    if provider == "gemini":
        return {"quality": [], "resolution": sorted(_gemini.sizes_for(model), key=_k)}
    return {"quality": _xai.qualities_for(model), "resolution": sorted(_xai.RESOLUTIONS, key=_k)}


def draft_params(model: str, params: dict) -> dict:
    """Feature 3: `params` at the lowest-cost quality and resolution the core offers for `model`. A model with no
    quality control is sent `auto`."""
    opts = options_for(model)
    out = dict(params or {})
    out["quality"] = opts["quality"][0] if opts["quality"] else "auto"
    out["resolution"] = opts["resolution"][0]
    return out


def final_options(model: str) -> list[str]:
    """The Final choices `model` offers: GPT Image qualities high / max, Gemini / Grok resolutions 2K / 4K."""
    opts = options_for(model)
    if provider_for(model) == "openai":
        return [q for q in ("high", "max") if q in opts["quality"]]
    return [r for r in ("2K", "4K") if r in opts["resolution"]]


def final_params(model: str, params: dict, choice: dict | None) -> dict:
    """Final settings: GPT Image -> quality `high` or `max` at the given resolution; Gemini / Grok -> resolution
    2K or 4K, and Grok's best quality. A choice the model does not offer falls back to its first Final option
    (Implementation choice); a model with no Final resolution takes its largest."""
    choice = choice or {}
    opts = options_for(model)
    finals = final_options(model)
    out = dict(params or {})
    if provider_for(model) == "openai":
        q = choice.get("quality")
        out["quality"] = q if q in finals else finals[0]
        if choice.get("resolution"):
            out["resolution"] = choice["resolution"]
        return out
    r = choice.get("resolution")
    out["resolution"] = r if r in finals else (finals[0] if finals else opts["resolution"][-1])
    out["quality"] = opts["quality"][-1] if opts["quality"] else "auto"
    return out


# ---------------------------------------------------------------- prompt, mode, estimate

def exact_text_line(text: str) -> str:
    """Feature 5: the exact text as a quoted last line, byte-identical; a text holding `"` goes in curly quotes."""
    quoted = f"“{text}”" if '"' in text else f'"{text}"'
    return f"Text in the image, exactly: {quoted}"


def prompt_to_send(prompt: str, negative: str | None, exact_text: str | None, negative_on: bool) -> str:
    """The prompt the image model receives: the prompt, then the exact-text line, then `Avoid: <negative>` when
    the negative switch is on and the negative is not empty (cloud APIs take no negative field)."""
    out = prompt or ""
    if exact_text:
        out += "\n" + exact_text_line(exact_text)
    if negative_on and negative and negative.strip():
        out += "\nAvoid: " + negative
    return out


def mode_from_info(info) -> str:
    """The entry's `mode`: the `mode : native|crop` line `studio.run` appends for region edits, else "whole"."""
    for line in info or ():
        m = _MODE_RE.match(str(line).strip())
        if m:
            return m.group(1)
    return "whole"


def estimate_cloud_cost(spec: dict) -> float | None:
    """Pre-run cost in USD, or None ("after run"): OpenAI from the output-token table, Gemini from its price
    table, xAI never. OpenAI region edits send a size known only from the picture, so they are "after run" too."""
    spec = normalize_spec(spec)
    model, n = spec["model"], spec["n"]
    provider = provider_for(model)
    if provider == "openai":
        if spec["operation"] in ("inpaint", "outpaint"):
            return None
        size = openai_size(model, spec["aspect_ratio"], spec["resolution"], 0, 0, [])
        one = openai_estimate(model, spec["quality"], size)
        return None if one is None else one * n
    if provider == "gemini":
        return gemini_cost(model, _gemini.price_size(model, spec["resolution"]), n)
    return None


# ---------------------------------------------------------------- specs and batches

def normalize_spec(spec: dict) -> dict:
    """Every spec key present, engine defaults filled. Raises ValueError (-> `refused`) on an unknown key, an
    unknown model or operation, or a value of the wrong kind."""
    if not isinstance(spec, dict):
        raise ValueError("spec must be an object")
    unknown = set(spec) - set(SPEC_KEYS)
    if unknown:
        raise ValueError(f"unknown spec keys: {', '.join(sorted(unknown))}")
    out = {k: spec.get(k) for k in SPEC_KEYS}
    for k, v in _DEFAULTS.items():
        if out[k] is None and (k not in spec or k not in ("crop_padding", "feather_px", "n")):
            out[k] = v
    provider_for(out["model"] if isinstance(out["model"], str) else "")   # ValueError on an unknown model
    if out["operation"] not in OPERATIONS:
        raise ValueError(f"unknown operation: {out['operation']!r}")
    if not isinstance(out["prompt"], str):
        raise ValueError("prompt must be text")
    for k in ("negative", "request", "exact_text"):
        if out[k] is None:
            out[k] = ""
        if not isinstance(out[k], str):
            raise ValueError(f"{k} must be text")
    if out["refs"] is None:
        out["refs"] = []
    if not isinstance(out["refs"], list) or not all(isinstance(r, dict) for r in out["refs"]):
        raise ValueError("refs must be a list of {name, subfolder, type}")
    for k in ("image", "mask", "writer", "sections"):
        if out[k] is not None and not isinstance(out[k], dict):
            raise ValueError(f"{k} must be an object or null")
    if out["tier"] is not None and out["tier"] not in TIERS:
        raise ValueError(f"tier must be draft, final or null, got {out['tier']!r}")
    n = out["n"]
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 8:
        raise ValueError(f"n must be a whole number in 1..8, got {n!r}")
    padding = out["crop_padding"]
    if (isinstance(padding, bool) or not isinstance(padding, (int, float))
            or not 0 <= padding <= 1 or not math.isfinite(padding)):
        raise ValueError("crop_padding must be a finite number in 0..1")
    feather = out["feather_px"]
    if isinstance(feather, bool) or not isinstance(feather, int) or feather < 0:
        raise ValueError("feather_px must be a whole number >= 0")
    if out["outpaint"] is not None:
        margins = out["outpaint"]
        if not isinstance(margins, (list, tuple)) or len(margins) != 4:
            raise ValueError("outpaint must be four margins (left, top, right, bottom)")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 2048
               or not math.isfinite(v) for v in margins):
            raise ValueError("outpaint margins must be finite numbers in 0..2048")
        out["outpaint"] = [int(v) for v in margins]
    return out


def _check_batch(batch: dict) -> dict:
    if not isinstance(batch, dict):
        raise ValueError("batch must be an object")
    unknown = set(batch) - set(BATCH_KEYS)
    if unknown:
        raise ValueError(f"unknown batch keys: {', '.join(sorted(unknown))}")
    models = batch.get("models")
    if not isinstance(models, list) or not 1 <= len(models) <= MAX_MODELS:
        raise ValueError(f"models must list 1 to {MAX_MODELS} models")
    if len(set(models)) != len(models):
        raise ValueError("a model is listed twice")
    for m in models:
        provider_for(m if isinstance(m, str) else "")
    count = batch.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 8:
        raise ValueError(f"count must be a whole number in 1..8, got {count!r}")
    mode = batch.get("variants_mode")
    if mode not in ("same", "varied"):
        raise ValueError(f"variants_mode must be same or varied, got {mode!r}")
    prompts = batch.get("prompts")
    want = 1 if mode == "same" else count
    if not isinstance(prompts, list) or len(prompts) != want:
        got = len(prompts) if isinstance(prompts, list) else 0
        raise ValueError(f"variants_mode {mode} with count {count} needs {want} prompt(s), got {got}")
    for p in prompts:
        if not isinstance(p, dict) or set(p) - set(_PROMPT_KEYS) or not isinstance(p.get("prompt"), str):
            raise ValueError("each prompt must be {prompt, negative, sections}")
    if batch.get("tier") not in TIERS:
        raise ValueError(f"tier must be draft or final, got {batch.get('tier')!r}")
    return batch


def batch_job_spec(batch: dict, model: str, variant: int, batch_id: str | None = None) -> dict:
    """The one cloud job of `batch` for `model` and `variant` (1-based): n = 1, the operation picked
    automatically, the references cut to the model's limit, the engine settings at the batch's tier."""
    prompts = batch["prompts"]
    p = prompts[variant - 1] if batch["variants_mode"] == "varied" else prompts[0]
    refs = list(batch.get("refs") or [])
    operation = auto_operation(model, len(refs))
    params = {k: batch.get(k) if batch.get(k) is not None else _DEFAULTS[k]
              for k in ("aspect_ratio", "resolution", "quality", "background")}
    params["n"] = 1
    if batch["tier"] == "draft":
        params = draft_params(model, params)
    else:
        params = final_params(model, params, {"quality": params["quality"], "resolution": params["resolution"]})
    spec = {
        "model": model, "operation": operation, "prompt": p["prompt"], "negative": p.get("negative") or "",
        "request": batch.get("request") or "", "writer": batch.get("writer"), "image": None, "mask": None,
        "refs": refs[:ref_limit(model, operation)], "parent": batch.get("parent"), **params,
        "seed": batch.get("seed"), "batch": batch_id, "variant": variant, "tier": batch["tier"],
        "exact_text": batch.get("exact_text") or "", "sections": p.get("sections"),
    }
    return normalize_spec(spec)


def estimate_batch(batch: dict) -> dict:
    """`{total, per_model: [{model, est_cost_usd}], unknown: [model]}`. Each model's figure is count x the
    estimate of its job spec at the batch's tier; `total` sums the known ones; a model with no pre-run figure is
    listed in `unknown` (its `est_cost_usd` is None) and never counted as $0."""
    _check_batch(batch)
    per_model, unknown, total = [], [], 0.0
    for model in batch["models"]:
        one = estimate_cloud_cost(batch_job_spec(batch, model, 1))
        if one is None:
            unknown.append(model)
            per_model.append({"model": model, "est_cost_usd": None})
            continue
        figure = batch["count"] * one
        per_model.append({"model": model, "est_cost_usd": figure})
        total += figure
    return {"total": total, "per_model": per_model, "unknown": unknown}


def _sent_refs(entry: dict) -> list[dict]:
    """The references an entry's run sent: its inputs after the picture (edit / inpaint / outpaint) and the
    mask (inpaint)."""
    op = entry.get("operation")
    lead = (1 if op in ("edit", "inpaint", "outpaint") else 0) + (1 if op == "inpaint" else 0)
    return list((entry.get("inputs") or [])[lead:])


def _inputs(spec: dict) -> list[dict]:
    """The entry's `inputs`: the picture first (when there is one), the mask (inpaint), then the sent refs."""
    out = [spec["image"]] if spec["image"] else []
    if spec["mask"] and spec["operation"] == "inpaint":
        out.append(spec["mask"])
    return out + list(spec["refs"])


# ---------------------------------------------------------------- the entry (built in one place)

def build_entry(spec: dict, *, status: str, outputs: list, cost_usd, est_cost_usd, seconds, mode, error) -> dict:
    """The history entry of one run: every `Store.ENTRY_KEYS` key except `id`, `ts`, `project` (the store assigns
    those). `prompt` and `negative` / `exact_text` stay apart, as the user wrote them."""
    spec = normalize_spec(spec)
    entry = {
        "parent": spec["parent"], "engine": ENGINE, "model": spec["model"], "package": None,
        "operation": spec["operation"], "request": spec["request"], "writer": spec["writer"],
        "prompt": spec["prompt"], "negative": spec["negative"], "seed": spec["seed"],
        "params": {k: spec[k] for k in PARAM_KEYS}, "inputs": _inputs(spec), "outputs": list(outputs),
        "mode": mode, "est_cost_usd": est_cost_usd, "cost_usd": cost_usd, "seconds": seconds,
        "status": status, "error": error, "star": False, "note": "", "hidden": False,
        "batch": spec["batch"], "variant": spec["variant"], "tier": spec["tier"],
        "exact_text": spec["exact_text"] or None, "sections": spec["sections"],
    }
    assert set(entry) == set(ENTRY_KEYS) - {"id", "ts", "project"}
    return entry


# ---------------------------------------------------------------- the runner

class _Job:
    __slots__ = ("job_id", "sid", "project", "spec", "est", "state", "cancel_requested", "entry", "error")

    def __init__(self, sid, project, spec, est):
        self.job_id = uuid.uuid4().hex
        self.sid, self.project, self.spec, self.est = sid, project, spec, est
        self.state = "queued"
        self.cancel_requested = False
        self.entry = None
        self.error = None


class _Failure(Exception):
    def __init__(self, code: str, message: str, sent: bool):
        super().__init__(message)
        self.code, self.message, self.sent = code, message, sent


def _named(exc: BaseException) -> tuple[str, str]:
    """An exception -> `{code, message}` parts (pre-flight P15): a ValueError from the core is `refused`."""
    if isinstance(exc, ValueError):
        return "refused", str(exc)
    if isinstance(exc, ProviderError):
        if exc.billed:
            return "billed", str(exc)
        return ("timeout" if "timed out" in str(exc).lower() else "provider"), str(exc)
    if isinstance(exc, TimeoutError):
        return "timeout", str(exc) or "timed out"
    return "internal", f"{type(exc).__name__}: {exc}"


class JobRunner:
    """Cloud jobs on a `max_workers` pool (Implementation choice: 3). `emit(event, data, sid)` sends a socket
    event; `resolve_key(provider)` returns the API key (raises when there is none); `load_ref(ref)` opens a
    stored image. The job table lives for the process lifetime (the cloud reconcile reads it)."""

    def __init__(self, emit: Callable[[str, dict, str | None], None], store, resolve_key: Callable[[str], str],
                 load_ref: Callable[[dict], object], max_workers: int = 3):
        self._emit_fn = emit
        self.store = store
        self._resolve_key = resolve_key
        self._load_ref = load_ref
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers,
                                                           thread_name_prefix="luna-director-job")
        self._lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}
        self._futures: list[concurrent.futures.Future] = []
        self._keys: set[str] = set()   # keys seen, scrubbed from every message

    # ---- public API

    def submit_cloud(self, sid: str, project: str, spec: dict) -> dict:
        """Queue one cloud run. Returns `{job_id, est_cost_usd}`; a bad spec raises ValueError before anything
        is queued."""
        self._check_project(project)
        job = self._new_job(sid, project, normalize_spec(spec))
        self._start([job])
        return {"job_id": job.job_id, "est_cost_usd": job.est}

    def submit_batch(self, sid: str, project: str, batch: dict) -> dict:
        """Feature 2: one job per model x image (n = 1), variant-major so every model starts early."""
        self._check_project(project)
        _check_batch(batch)
        batch_id = uuid.uuid4().hex
        specs = [batch_job_spec(batch, model, v, batch_id)
                 for v in range(1, batch["count"] + 1) for model in batch["models"]]
        jobs = [self._new_job(sid, project, s) for s in specs]
        est = estimate_batch(batch)
        self._start(jobs)
        return {"batch_id": batch_id,
                "jobs": [{"job_id": j.job_id, "model": j.spec["model"], "variant": j.spec["variant"],
                          "est_cost_usd": j.est} for j in jobs],
                "est_cost_usd": est["total"], "unknown": est["unknown"]}

    def submit_final(self, sid: str, project: str, entry_id: str, choice: dict | None) -> dict:
        """Feature 3: re-render a chosen result as a child entry - an edit with its output as Image 1, its sent
        references after it within the edit limit, `FINAL_LEAD` before its prompt, at Final settings, the same
        aspect ratio, batch and variant."""
        entry = self.store.get_entry(project, entry_id)
        if entry is None:
            raise ValueError(f"no history entry {entry_id!r} in project {project!r}")
        outputs = entry.get("outputs") or []
        if not outputs:
            raise ValueError("that history entry has no image to finish")
        model = entry.get("model")
        if entry.get("engine") != ENGINE or not isinstance(model, str):
            raise ValueError("only a cloud result can be finished")
        params = {k: v for k, v in (entry.get("params") or {}).items() if k in PARAM_KEYS}
        params = final_params(model, {**params, "n": 1}, choice)
        spec = {
            **params, "model": model, "operation": "edit", "prompt": FINAL_LEAD + "\n" + (entry.get("prompt") or ""),
            "negative": entry.get("negative") or "", "request": entry.get("request") or "",
            "writer": entry.get("writer"), "image": outputs[0], "mask": None,
            "refs": _sent_refs(entry)[:ref_limit(model, "edit")], "parent": entry["id"],
            "seed": entry.get("seed"), "batch": entry.get("batch"), "variant": entry.get("variant"),
            "tier": "final", "exact_text": entry.get("exact_text") or "", "sections": entry.get("sections"),
        }
        return self.submit_cloud(sid, project, spec)

    def cancel(self, job_id: str) -> bool:
        """Queued -> cancelled now, never sent. Running -> marked; its result is kept when the call returns.
        False for an unknown, ended or already-cancelled job."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.state == "queued":
                job.state = "cancelled"
                payload = self._payload(job)
            elif job.state == "running" and not job.cancel_requested:
                job.cancel_requested = True
                return True
            else:
                return False
        self._emit(job, payload)
        return True

    def cancel_batch(self, batch_id: str) -> int:
        """Cancel every job of a batch by the `cancel` rule; returns how many it touched."""
        with self._lock:
            ids = [j.job_id for j in self._jobs.values() if batch_id and j.spec["batch"] == batch_id]
        return sum(1 for jid in ids if self.cancel(jid))

    def jobs_for(self, sid: str) -> list[dict]:
        """This sid's jobs, oldest first: `{job_id, state, entry?, error?}`."""
        with self._lock:
            out = []
            for j in self._jobs.values():
                if j.sid != sid:
                    continue
                row = {"job_id": j.job_id, "state": j.state}
                if j.entry is not None:
                    row["entry"] = j.entry
                if j.error is not None:
                    row["error"] = dict(j.error)
                out.append(row)
            return out

    def wait(self, timeout: float | None = None) -> None:
        """Block until every submitted job has finished (tests, shutdown)."""
        with self._lock:
            futures = list(self._futures)
        concurrent.futures.wait(futures, timeout=timeout)

    def close(self) -> None:
        self._pool.shutdown(wait=True)

    # ---- internals

    def _check_project(self, project) -> None:
        """Refuse a bad project before anything is sent: a paid image must have a ledger to land in."""
        if not isinstance(project, str) or self.store.project_slug(project) != project:
            raise ValueError(f"bad project name: {project!r}")

    def _new_job(self, sid, project, spec) -> _Job:
        return _Job(sid, project, spec, estimate_cloud_cost(spec))

    def _start(self, jobs: list[_Job]) -> None:
        with self._lock:
            for job in jobs:
                self._jobs[job.job_id] = job
        for job in jobs:
            self._emit(job, self._payload(job))
            fut = self._pool.submit(self._work, job)
            with self._lock:
                self._futures = [f for f in self._futures if not f.done()] + [fut]

    def _payload(self, job: _Job) -> dict:
        generate = job.spec["tier"] is not None
        data = {"job_id": job.job_id, "state": job.state,
                "batch": job.spec["batch"] if generate else None,
                "variant": job.spec["variant"] if generate else None,
                "model": job.spec["model"] if generate else None}
        if job.entry is not None:
            data["entry"] = job.entry
        if job.error is not None:
            data["error"] = dict(job.error)
        return data

    def _scrub(self, text: str) -> str:
        with self._lock:
            keys = list(self._keys)
        for key in keys:
            if key:
                text = text.replace(key, "***")
        return text

    def _emit(self, job: _Job, data: dict) -> None:
        try:
            self._emit_fn(EVENT, data, job.sid)
        except Exception:   # a closed socket must never kill the worker or lose the ledger write
            log.warning("luna.job emit failed for job %s", job.job_id, exc_info=True)

    def _work(self, job: _Job) -> None:
        with self._lock:
            if job.state != "queued":   # cancelled while queued: never sent, no entry
                return
            job.state = "running"
            payload = self._payload(job)
        self._emit(job, payload)
        started = time.monotonic()
        result, failure = None, None
        try:
            result = self._call(job)
        except _Failure as f:
            failure = f
        except Exception as exc:   # pragma: no cover - _call names every failure
            failure = _Failure(*_named(exc), sent=True)
        seconds = round(time.monotonic() - started, 2)
        try:
            if failure is None:
                entry, error = self._record(job, result, "done", seconds), None
                status = entry["status"] if entry is not None else "error"
            else:
                message = self._scrub(failure.message)
                error = {"code": failure.code, "message": message}
                # A billed failure keeps the estimate as its cost. The ledger stores the code on the
                # existing `error` field so history can say so after a reload; other errors stay a string.
                cost = job.est if failure.code == "billed" else None
                entry = self._record_error(job, error if failure.code == "billed" else message, seconds, cost)
                status = "error"
        except Exception:
            # Scrubbing and recording may themselves fail. Do not expose that exception's text:
            # it can contain a key, and no failure here may leave the job running.
            status, entry = "error", None
            error = {"code": "internal", "message": "job result could not be safely recorded"}
            try:
                entry = self._record_error(job, error["message"], seconds)
            except Exception:
                log.error("failed to record terminal error for job %s", job.job_id)
        with self._lock:
            job.state, job.entry, job.error = status, entry, error
            payload = self._payload(job)
        self._emit(job, payload)

    def _call(self, job: _Job):
        spec = job.spec
        provider = provider_for(spec["model"])
        try:
            key = self._resolve_key(provider)
        except Exception as exc:
            raise _Failure("no_key", str(exc) or f"no {provider} API key", sent=False) from None
        if key:
            with self._lock:
                self._keys.add(key)
        try:
            images = [self._load_ref(spec["image"])] if spec["image"] else []
            images += [self._load_ref(r) for r in spec["refs"]]
            mask = self._load_ref(spec["mask"]) if spec["mask"] else None
            negative_on = bool((spec["writer"] or {}).get("negative_on"))
            req = EditRequest(
                provider=provider, model=spec["model"], operation=spec["operation"],
                prompt=prompt_to_send(spec["prompt"], spec["negative"], spec["exact_text"], negative_on),
                images=images, mask=mask, aspect_ratio=spec["aspect_ratio"], resolution=spec["resolution"],
                n=spec["n"], quality=spec["quality"], background=spec["background"],
                outpaint=tuple(spec["outpaint"] or (0, 0, 0, 0)), mask_mode=spec["mask_mode"],
                crop_padding=float(spec["crop_padding"]), feather_px=int(spec["feather_px"]))
            result, _mask = studio.run(req, key)
        except Exception as exc:
            raise _Failure(*_named(exc), sent=True) from None
        return result

    def _add(self, job: _Job, entry: dict, fallback: dict) -> dict | None:
        """add_history; when the ledger refuses the entry, a minimal fallback so a paid image is never left
        without a record."""
        try:
            return self.store.add_history(job.project, entry)
        except Exception as exc:
            reason = self._scrub(str(exc))
            log.warning("ledger refused the entry of job %s (%s); writing a minimal entry", job.job_id, reason)
            try:
                return self.store.add_history(job.project, {**fallback, "error": f"ledger refused: {reason}"})
            except Exception:
                log.error("ledger refused the minimal entry of job %s too; outputs: %s", job.job_id,
                          fallback.get("outputs"), exc_info=True)
                return None

    def _record(self, job: _Job, result, status: str, seconds: float) -> dict | None:
        """Save every returned image first, then write the entry."""
        outputs = []
        stem = f"{job.spec['model']}_{job.job_id[:8]}"
        try:
            for img in result.images:
                outputs.append(self.store.save_png(job.project, img, stem))
        except Exception as exc:
            log.error("saving the result of job %s failed", job.job_id, exc_info=True)
            message = f"saving the image failed: {self._scrub(str(exc))}"
            entry = build_entry(job.spec, status="error", outputs=outputs, cost_usd=result.cost_usd,
                                est_cost_usd=job.est, seconds=seconds, mode=mode_from_info(result.info),
                                error=message)
            return self._add(job, entry, {"status": "error", "outputs": outputs, "cost_usd": result.cost_usd})
        with self._lock:
            status = "cancelled" if job.cancel_requested else status
        entry = build_entry(job.spec, status=status, outputs=outputs, cost_usd=result.cost_usd,
                            est_cost_usd=job.est, seconds=seconds, mode=mode_from_info(result.info), error=None)
        return self._add(job, entry, {"status": status, "outputs": outputs, "cost_usd": result.cost_usd})

    def _record_error(self, job: _Job, error, seconds: float, cost_usd=None) -> dict | None:
        entry = build_entry(job.spec, status="error", outputs=[], cost_usd=cost_usd, est_cost_usd=job.est,
                            seconds=seconds, mode=None, error=error)
        return self._add(job, entry, {"status": "error", "outputs": [], "cost_usd": cost_usd})
