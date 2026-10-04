"""Luna Director store: projects, the per-project `history.jsonl` ledger, the day's cost, safe file refs and
project asset storage de-duplicated by content.

Layout (both roots injected, so this module never asks ComfyUI where they are):
  root        = <output>/luna_director        -> <root>/<project>/history.jsonl and saved results
  assets_root = <input>/luna_director         -> <assets_root>/<project>/<sha256[:16]>.<ext>

The ledger is append-only. One JSON object per line: `{"op": "add", "entry": {...}}` or
`{"op": "patch", "id": ..., "patch": {...}}`. Readers fold the patches in file order and skip any line that does
not parse (a torn last line after a crash), so the rest of the history always loads.

A ref is `{name, subfolder, type}` everywhere; `resolve_ref` maps it to a path under the injected `dirs` and
refuses anything that escapes them.

Stdlib + Pillow (+ luna_imaging.resize) only; ComfyUI-free.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import threading
import unicodedata
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from PIL import Image, ImageOps

from luna_imaging.resize import MODES, plan_resize, apply_state

SUBDIR = "luna_director"
SLUG_MAX = 48
LEDGER = "history.jsonl"

ENTRY_KEYS = (
    "id", "parent", "project", "ts", "engine", "model", "package", "operation", "request", "writer",
    "prompt", "negative", "seed", "params", "inputs", "outputs", "mode", "est_cost_usd", "cost_usd",
    "seconds", "status", "error", "star", "note", "hidden",
    # Generate keys (S10): null outside the Generate tab.
    "batch", "variant", "tier", "exact_text", "sections",
)
STATUSES = ("done", "error", "cancelled")
TIERS = (None, "draft", "final")
PATCH_TYPES = {"star": bool, "note": str, "hidden": bool}
_ENTRY_DEFAULTS = {"inputs": [], "outputs": [], "star": False, "note": "", "hidden": False}
_WRITER_DEFAULTS = {"feedback": "", "variants_mode": None}

# Pillow format -> file extension. MPO (phone JPEGs) is JPEG-compatible, so it is stored as .jpg.
_EXT = {"JPEG": "jpg", "MPO": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif", "BMP": "bmp", "TIFF": "tif"}
_SLUG_RE = re.compile(r"^[a-z0-9-]{1,%d}$" % SLUG_MAX)
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


class RefError(ValueError):
    """A ref or project name that is unsafe, unknown or not an image."""


class ResizeStateError(ValueError):
    """A resize state value that is refused before it reaches Pillow; the message names the field."""


# ---------------------------------------------------------------- names and refs

def project_slug(name) -> str:
    """Any project name -> `[a-z0-9-]`, at most 48 characters; "default" when nothing is left."""
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:SLUG_MAX].strip("-")
    return slug or "default"


def _check_project(project) -> str:
    if not isinstance(project, str) or not _SLUG_RE.match(project):
        raise RefError(f"bad project name: {project!r}")
    return project


def resolve_ref(ref: dict, dirs: dict) -> Path:
    """`{name, subfolder, type}` -> an absolute path inside `dirs[type]`. Raises `RefError` on an unknown type
    or a path that leaves that folder (checked with `os.path.commonpath`, as ComfyUI's upload route does)."""
    if not isinstance(ref, dict):
        raise RefError("ref must be {name, subfolder, type}")
    kind, name, sub = ref.get("type"), ref.get("name"), ref.get("subfolder") or ""
    if not isinstance(kind, str) or kind not in dirs:
        raise RefError(f"unknown ref type: {kind!r}")
    if not isinstance(name, str) or not name or not isinstance(sub, str):
        raise RefError("ref needs a file name")
    base = os.path.abspath(dirs[kind])
    full = os.path.abspath(os.path.join(base, sub, name))
    try:
        inside = os.path.commonpath((base, full)) == base
    except ValueError:   # another drive on Windows
        inside = False
    if not inside or full == base:
        raise RefError("ref leaves its folder")
    return Path(full)


def open_rgb(path) -> Image.Image:
    """Open an asset as the Luna Asset Loader does: EXIF orientation applied, then RGB."""
    with Image.open(path) as im:
        return ImageOps.exif_transpose(im).convert("RGB")


# ---------------------------------------------------------------- resize state

def _number(item, key, *, positive=True):
    v = item.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or (positive and v <= 0):
        raise ResizeStateError(f"bad resize state: {key} must be a finite number > 0, got {v!r}")


def validate_resize_item(item: dict) -> dict:
    """Refuse a settled resize item (`resize.parse_state` output) whose values Pillow or the maths would
    choke on. Returns the item unchanged when it is valid."""
    if not isinstance(item, dict):
        raise ResizeStateError("bad resize state: item must be an object")
    if item.get("mode") not in MODES:
        raise ResizeStateError(f"bad resize state: mode must be one of {', '.join(MODES)}, got {item.get('mode')!r}")
    for key in ("max_mp", "longest_side", "scale_factor"):
        _number(item, key)
    snap = item.get("snap")
    if isinstance(snap, bool) or not isinstance(snap, int) or snap < 0:
        raise ResizeStateError(f"bad resize state: snap must be a whole number >= 0, got {snap!r}")
    if not isinstance(item.get("ratio"), str):
        raise ResizeStateError(f"bad resize state: ratio must be text like 16:9, got {item.get('ratio')!r}")
    if item.get("ratio_action") not in ("crop", "pad"):
        raise ResizeStateError(f"bad resize state: ratio_action must be crop or pad, got {item.get('ratio_action')!r}")
    pad = item.get("pad_color")
    if not isinstance(pad, str) or not _HEX_COLOR.match(pad):
        raise ResizeStateError(f"bad resize state: pad_color must be #rrggbb, got {pad!r}")
    up = item.get("allow_upscale")
    if not isinstance(up, bool):
        raise ResizeStateError(f"bad resize state: allow_upscale must be true or false, got {up!r}")
    anchor = item.get("crop_anchor")
    if isinstance(anchor, dict):
        for k in ("x", "y"):
            if k in anchor:
                v = anchor[k]
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                    raise ResizeStateError(f"bad resize state: crop_anchor.{k} must be a number 0..1, got {v!r}")
    elif not isinstance(anchor, str):
        raise ResizeStateError(f"bad resize state: crop_anchor must be a name or {{x, y}}, got {anchor!r}")
    return item


def _oriented_size(im: Image.Image) -> tuple[int, int]:
    w, h = im.size
    try:
        orientation = im.getexif().get(0x0112)
    except Exception:
        orientation = None
    return (h, w) if orientation in (5, 6, 7, 8) else (w, h)


# ---------------------------------------------------------------- store

class Store:
    project_slug = staticmethod(project_slug)
    resolve_ref = staticmethod(resolve_ref)

    def __init__(self, root: Path, assets_root: Path, clock: Callable[[], datetime] | None = None):
        self.root = Path(root)
        self.assets_root = Path(assets_root)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._lock = threading.Lock()

    # ---- ledger

    def _ledger(self, project) -> Path:
        return self.root / _check_project(project) / LEDGER

    def _append(self, project, record: dict) -> None:
        path = self._ledger(project)
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "ab+") as f:
                f.seek(0, os.SEEK_END)
                if f.tell():
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":   # a torn last line: start ours on a fresh line
                        line = b"\n" + line
                f.write(line)

    @staticmethod
    def _read(path: Path) -> list[dict]:
        """Fold the ledger at `path` -> entries in append order. Unparseable lines are skipped."""
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return []
        entries: dict[str, dict] = {}
        for line in raw.splitlines():
            try:
                rec = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("op") == "add" and isinstance(rec.get("entry"), dict) and rec["entry"].get("id"):
                entries[rec["entry"]["id"]] = rec["entry"]
            elif rec.get("op") == "patch" and rec.get("id") in entries and isinstance(rec.get("patch"), dict):
                entries[rec["id"]].update({k: v for k, v in rec["patch"].items() if k in PATCH_TYPES})
        return list(entries.values())

    def add_history(self, project, entry: dict) -> dict:
        """Store a new entry; assigns `id` (uuid4 hex), `ts` (local ISO time) and `project`."""
        _check_project(project)
        unknown = set(entry) - set(ENTRY_KEYS)
        if unknown:
            raise ValueError(f"unknown history keys: {', '.join(sorted(unknown))}")
        if entry.get("status") not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}, got {entry.get('status')!r}")
        if entry.get("tier") not in TIERS:
            raise ValueError(f"tier must be draft, final or null, got {entry.get('tier')!r}")
        full = {k: _ENTRY_DEFAULTS.get(k) for k in ENTRY_KEYS}
        full.update(entry)
        if isinstance(full["writer"], dict):
            full["writer"] = {**_WRITER_DEFAULTS, **full["writer"]}
        full["id"] = uuid.uuid4().hex
        full["ts"] = self._clock().isoformat()
        full["project"] = project
        full = json.loads(json.dumps(full))   # a detached, JSON-clean copy
        self._append(project, {"op": "add", "entry": full})
        return full

    def update_history(self, project, id, patch: dict) -> dict:
        """Append a patch line. Only `star`, `note`, `hidden` may change."""
        if not isinstance(patch, dict):
            raise ValueError("patch must be an object")
        other = set(patch) - set(PATCH_TYPES)
        if other:
            raise ValueError(f"only star, note and hidden can be changed, not: {', '.join(sorted(other))}")
        for k, v in patch.items():
            if not isinstance(v, PATCH_TYPES[k]):
                raise ValueError(f"{k} must be {PATCH_TYPES[k].__name__}, got {v!r}")
        entry = self.get_entry(project, id)
        if entry is None:
            raise KeyError(f"no history entry {id!r} in project {project!r}")
        if patch:
            self._append(project, {"op": "patch", "id": id, "patch": patch})
            entry.update(patch)
        return entry

    def list_history(self, project, limit=200) -> list[dict]:
        """Entries with their patches folded, newest first."""
        entries = self._read(self._ledger(project))
        entries.reverse()
        return entries if limit is None else entries[:max(0, int(limit))]

    def get_entry(self, project, id) -> dict | None:
        """One entry of any age, patches folded."""
        for e in self._read(self._ledger(project)):
            if e.get("id") == id:
                return e
        return None

    def day_cost(self, date_iso) -> float:
        """Actual cost (`cost_usd`) of every project's entries whose `ts` falls on the local date `date_iso`.
        A cancelled entry's cost counts; an entry without an actual cost does not."""
        day = date.fromisoformat(str(date_iso))
        total = 0.0
        if not self.root.is_dir():
            return total
        for ledger in sorted(self.root.glob(f"*/{LEDGER}")):
            for e in self._read(ledger):
                cost = e.get("cost_usd")
                if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost):
                    continue
                try:
                    ts = datetime.fromisoformat(e.get("ts"))
                except (TypeError, ValueError):
                    continue
                if ts.astimezone().date() == day:
                    total += cost
        return round(total, 6)

    # ---- files

    def save_png(self, project, img: Image.Image, stem) -> dict:
        """Save a result PNG under `<root>/<project>/`; never overwrites. Returns its output ref."""
        folder = self.root / _check_project(project)
        folder.mkdir(parents=True, exist_ok=True)
        base = re.sub(r"[^A-Za-z0-9_-]+", "_", str(stem or "")).strip("_")[:64] or "result"
        buf = io.BytesIO()
        img.save(buf, "PNG")
        data = buf.getvalue()
        for n in range(10000):
            name = f"{base}.png" if n == 0 else f"{base}-{n}.png"
            try:
                with open(folder / name, "xb") as f:
                    f.write(data)
                return {"name": name, "subfolder": f"{SUBDIR}/{project}", "type": "output"}
            except FileExistsError:
                continue
        raise RefError(f"no free file name for {base}.png")

    def import_asset(self, project, data: bytes) -> dict:
        """Store image bytes once per content under `<assets_root>/<project>/<sha256[:16]>.<ext>`."""
        _check_project(project)
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise RefError("not an image")
        try:
            with Image.open(io.BytesIO(data)) as im:
                fmt = im.format
                im.load()
        except Exception:
            raise RefError("not an image") from None
        if not fmt:
            raise RefError("not an image")
        name = f"{hashlib.sha256(data).hexdigest()[:16]}.{_EXT.get(fmt, fmt.lower())}"
        folder = self.assets_root / project
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        if not path.exists():
            tmp = folder / f".{name}.{uuid.uuid4().hex}.tmp"
            tmp.write_bytes(bytes(data))
            os.replace(tmp, path)
        return {"name": name, "subfolder": f"{SUBDIR}/{project}", "type": "input"}

    def resize_asset(self, project, ref: dict, item: dict, dirs: dict, dry_run=False) -> dict:
        """Plan (and unless `dry_run`, apply) one settled resize item on the asset `ref`.

        The image is opened as the Asset Loader does (EXIF orientation, then RGB) before `apply_state`, so the
        copy is RGB whatever the source mode. A changed image is stored through `import_asset` as a new asset;
        the original is kept. Returns `{in, out, crop_box, pad_size, changed}` plus `ref` when not a dry run
        (an unchanged image returns its own ref)."""
        _check_project(project)
        validate_resize_item(item)
        path = resolve_ref(ref, dirs)
        if not path.is_file():
            raise RefError("ref does not exist")
        try:
            if dry_run:
                with Image.open(path) as im:
                    size = _oriented_size(im)
                img = None
            else:
                img = open_rgb(path)
                size = img.size
        except Exception:
            raise RefError("not an image") from None
        plan = plan_resize(size[0], size[1], item)
        out = {
            "in": list(size), "out": list(plan.out_size),
            "crop_box": list(plan.crop_box) if plan.crop_box else None,
            "pad_size": list(plan.pad_size) if plan.pad_size else None,
            "changed": plan.changed,
        }
        if dry_run:
            return out
        if not plan.changed:
            out["ref"] = dict(ref)
            return out
        result = apply_state(img, item)
        buf = io.BytesIO()
        result.save(buf, "PNG")
        out["ref"] = self.import_asset(project, buf.getvalue())
        return out
