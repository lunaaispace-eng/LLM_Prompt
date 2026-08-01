"""Dataset captioning nodes for LLM_Prompt.

    LoadImageForCaption --IMAGE--> LLM Prompt (caption mode) --caption--> SaveCaption
            |-- filename ------------------------------------------------------^
            |-- directory -----------------------------------------------------^

Captions a folder ONE image per queue run and writes each caption to disk
immediately (durable + resumable), so a slow local model still saves progress
as it goes and nothing is lost if you stop partway.

How to run: wire the three nodes, set the Run button to **Run (Instant)** (auto
queue). Each run loads the FIRST not-yet-captioned image, captions it, writes
`<directory>/<filename>.<ext>` next to it, then the next run auto-fires and
picks up the next image. No batch count (ComfyUI caps that at 100), no clicking
N times. When every image has a caption the loader prints ALL DONE and the runs
become instant no-ops — press the red ✕ to stop.

Resume is automatic: `skip_existing` means already-captioned images are skipped,
so you can stop and restart anytime.
"""

from __future__ import annotations

import json
import os
import re

import numpy as np
import torch
from PIL import Image, ImageOps

from comfy_api.latest import io

_IMG_EXTS_DEFAULT = ".png,.jpg,.jpeg,.webp,.bmp,.tiff"


def _natural_key(name: str):
    """Sort key so Suki_2 < Suki_10 (numeric chunks compared as numbers)."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _list_images(directory: str, extensions: str, sort: str) -> list[str]:
    if not directory or not os.path.isdir(directory):
        return []
    exts = set()
    for e in extensions.split(","):
        e = e.strip().lower()
        if not e:
            continue
        exts.add(e if e.startswith(".") else "." + e)
    files = [
        n for n in os.listdir(directory)
        if os.path.isfile(os.path.join(directory, n))
        and os.path.splitext(n)[1].lower() in exts
    ]
    if sort == "date":
        files.sort(key=lambda n: os.path.getmtime(os.path.join(directory, n)))
    else:
        files.sort(key=_natural_key)
    return files


def _load_image_tensor(path: str, max_side: int = 0) -> torch.Tensor:
    """Load an image as a ComfyUI IMAGE tensor: float32 [1, H, W, 3] in 0..1.

    If max_side > 0, downscale so the longest side is at most max_side (keeps
    aspect). Captioning does not need full resolution and smaller = far fewer
    vision tokens = much faster local inference. Originals on disk are untouched.
    """
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)  # honor camera/EXIF orientation
    img = img.convert("RGB")
    if max_side and max_side > 0:
        w, h = img.size
        longest = max(w, h)
        if longest > max_side:
            scale = max_side / float(longest)
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    arr = np.asarray(img).astype(np.float32) / 255.0
    return torch.from_numpy(arr)[None,]


def _blank_tensor() -> torch.Tensor:
    return torch.zeros((1, 64, 64, 3), dtype=torch.float32)


class LoadImageForCaption(io.ComfyNode):
    """Load the next un-captioned image from a folder (one per run)."""

    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="LoadImageForCaption",
            display_name="Load Image For Caption",
            category="Luna/LLM",
            description=(
                "Loads the FIRST image in a folder that has no caption yet — one per "
                "queue run. Use with Run (Instant) so it auto-advances through the "
                "whole dataset, saving each caption as it goes. Resumable: stop and "
                "restart anytime; already-captioned images are skipped."
            ),
            inputs=[
                io.String.Input(
                    "directory", default="",
                    tooltip="Folder of images to caption. One un-captioned image is loaded per run.",
                ),
                io.Combo.Input(
                    "sort", options=["name", "date"], default="name",
                    tooltip="Order by natural filename (Suki_0001, Suki_0002, ... Suki_0010) or by file modified time.",
                ),
                io.String.Input(
                    "extensions", default=_IMG_EXTS_DEFAULT,
                    tooltip="Comma-separated image extensions to include.",
                ),
                io.Boolean.Input(
                    "skip_existing", default=True,
                    tooltip="Skip images that already have a caption sidecar (see caption_ext). Keep ON so runs "
                            "advance through the folder and a stopped run resumes cleanly. OFF = always load the "
                            "first image (re-caption everything).",
                ),
                io.Combo.Input(
                    "caption_ext", options=["txt", "json"], default="txt",
                    tooltip="Which sidecar extension counts as 'already captioned'. Match this to Save Caption's 'ext'.",
                ),
            ],
            outputs=[
                io.Image.Output("image", display_name="image"),
                io.String.Output("filename", display_name="filename"),
                io.String.Output("directory", display_name="directory"),
                io.Int.Output("remaining", display_name="remaining"),
            ],
        )

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        # Always re-scan so each run sees the caption just written and advances.
        return float("nan")

    @classmethod
    def execute(cls, directory, sort, extensions, skip_existing, caption_ext):
        files = _list_images(directory, extensions, sort)
        total = len(files)
        if total == 0:
            print(f"[LoadImageForCaption] No images found in: {directory!r}")
            return io.NodeOutput(_blank_tensor(), "", directory, 0)

        todo = []
        for name in files:
            base = os.path.splitext(name)[0]
            if skip_existing and os.path.exists(os.path.join(directory, f"{base}.{caption_ext}")):
                continue
            todo.append(name)

        if not todo:
            print(f"[LoadImageForCaption] ALL DONE — all {total} images captioned. "
                  f"Press the red ✕ to stop the queue.")
            return io.NodeOutput(_blank_tensor(), "", directory, 0)

        name = todo[0]
        base = os.path.splitext(name)[0]
        full = os.path.join(directory, name)
        done = total - len(todo)
        try:
            tensor = _load_image_tensor(full)
        except Exception as e:
            print(f"[LoadImageForCaption] failed to load {full}: {e}")
            return io.NodeOutput(_blank_tensor(), "", directory, len(todo))

        print(f"[LoadImageForCaption] [{done + 1}/{total}] {name}  "
              f"({len(todo) - 1} left after this)")
        return io.NodeOutput(tensor, base, directory, len(todo))


class SaveCaption(io.ComfyNode):
    """Write a caption next to its image: <directory>/<filename>.<ext>, raw text."""

    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="SaveCaption",
            display_name="Save Caption",
            category="Luna/LLM",
            description=(
                "Writes the caption to <directory>/<filename>.<ext> — raw caption "
                "text only, no Positive/Negative labels, no image re-encode. Wire "
                "filename + directory from Load Image For Caption for in-place "
                "dataset captioning (kohya/training-standard sidecar .txt)."
            ),
            inputs=[
                io.String.Input(
                    "caption", multiline=True, default="", force_input=True,
                    tooltip="The caption text to save (from the LLM node's output).",
                ),
                io.String.Input(
                    "filename", default="", force_input=True,
                    tooltip="Base name WITHOUT extension (from Load Image For Caption). Saved as <filename>.<ext>.",
                ),
                io.String.Input(
                    "directory", default="", force_input=True,
                    tooltip="Folder to write into (from Load Image For Caption) — same folder as the image = in-place sidecar.",
                ),
                io.Combo.Input(
                    "ext", options=["txt", "json"], default="txt",
                    tooltip='txt = raw caption text (kohya/training standard). json = {"caption": "..."}.',
                ),
                io.Combo.Input(
                    "overwrite", options=["overwrite", "skip", "append"], default="overwrite",
                    tooltip="overwrite = replace an existing caption. skip = keep the existing file. append = add to it.",
                ),
            ],
            outputs=[
                io.String.Output("path", display_name="path"),
            ],
            is_output_node=True,
        )

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return float("nan")

    @classmethod
    def execute(cls, caption, filename, directory, ext, overwrite):
        if not directory or not filename:
            # Happens on the ALL DONE / empty run — nothing to write.
            return io.NodeOutput("")
        try:
            os.makedirs(directory, exist_ok=True)
        except Exception as e:
            print(f"[SaveCaption] cannot create dir {directory!r}: {e}")
            return io.NodeOutput("")

        text = (caption or "").strip()
        out_path = os.path.join(directory, f"{filename}.{ext}")
        base = os.path.basename(out_path)

        if os.path.exists(out_path) and overwrite == "skip":
            print(f"[SaveCaption] skip (exists): {base}")
            return io.NodeOutput(out_path)

        try:
            if ext == "json":
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump({"caption": text}, f, ensure_ascii=False, indent=2)
            elif overwrite == "append" and os.path.exists(out_path):
                with open(out_path, "a", encoding="utf-8") as f:
                    f.write("\n" + text)
            else:
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(text)
            print(f"[SaveCaption] wrote {base} ({len(text)} chars)")
        except Exception as e:
            print(f"[SaveCaption] FAILED to write {out_path}: {e}")
            return io.NodeOutput("")

        return io.NodeOutput(out_path)


NODE_CLASS_MAPPINGS = {
    "LoadImageForCaption": LoadImageForCaption,
    "SaveCaption": SaveCaption,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LoadImageForCaption": "Load Image For Caption",
    "SaveCaption": "Save Caption",
}
