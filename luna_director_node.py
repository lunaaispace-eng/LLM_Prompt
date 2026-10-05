"""Luna Director launcher (D2). A graph run returns the selected history image, or the input.

`_execute_director` is the whole body so the D1 node (A20) can call it without a refactor.
The studio's "from graph" import reads `ui.images`, which is always the input image.
"""
from __future__ import annotations

import json
from pathlib import Path

import folder_paths
import numpy as np
import torch
from PIL import Image

from comfy_api.latest import io
from comfy_api.latest import ui as comfy_ui

from .luna_director.store import Store, open_rgb, resolve_ref


def _parse_state(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    text = (raw or "").strip() if isinstance(raw, str) else ""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _dirs() -> dict:
    return {
        "input": folder_paths.get_input_directory(),
        "output": folder_paths.get_output_directory(),
        "temp": folder_paths.get_temp_directory(),
    }


def _store() -> Store:
    dirs = _dirs()
    return Store(Path(dirs["output"]) / "luna_director", Path(dirs["input"]) / "luna_director")


def _image_tensor(img: Image.Image) -> torch.Tensor:
    arr = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    return torch.from_numpy(arr)[None]


def _mask_like(image: torch.Tensor) -> torch.Tensor:
    return torch.zeros((image.shape[0], image.shape[1], image.shape[2]), dtype=torch.float32, device=image.device)


def _info(entry: dict) -> str:
    cost = entry.get("cost_usd")
    cost_s = f"${cost:.4f}" if isinstance(cost, (int, float)) and not isinstance(cost, bool) else "n/a"
    return "\n".join([
        f"model : {entry.get('model') or ''}",
        f"operation : {entry.get('operation') or ''}",
        f"status : {entry.get('status') or ''}",
        f"cost : {cost_s}",
    ])


def _ui_images(image):
    """The input image, saved to temp the way PreviewImage does, so "from graph" can import it."""
    if image is None:
        return {"images": []}
    return comfy_ui.PreviewImage(image)


def _load_selected(state: dict):
    project, selected = state.get("project"), state.get("selected")
    if not isinstance(project, str) or not project or not isinstance(selected, str) or not selected:
        return None
    entry = _store().get_entry(project, selected)
    if not entry:
        return None
    outputs = entry.get("outputs") or []
    if not outputs:
        return None
    path = resolve_ref(outputs[0], _dirs())
    if not path.is_file():
        raise FileNotFoundError(f"history image is missing: {path.name}")
    return entry, _image_tensor(open_rgb(path))


def _execute_director(image=None, director_state=""):
    state = _parse_state(director_state)
    loaded = _load_selected(state)
    if loaded is not None:
        entry, out_image = loaded
        prompt = entry.get("prompt") or state.get("prompt") or ""
        info = _info(entry)
    else:
        out_image = image if image is not None else torch.zeros((1, 64, 64, 3))
        prompt = state.get("prompt") or ""
        info = ""
    if not isinstance(prompt, str):
        prompt = "" if prompt is None else str(prompt)
    return io.NodeOutput(out_image, _mask_like(out_image), prompt, info, ui=_ui_images(image))


class LunaDirectorLauncher(io.ComfyNode):
    """Opens Luna Image Studio. A queued run outputs the selected result, or the input image."""

    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="LunaDirectorLauncher",
            display_name="Luna Director (Studio)",
            category="Luna/LLM",
            is_output_node=True,
            description=(
                "Opens the Luna Image Studio over the graph. Connect an image and queue the graph: "
                "the studio imports that input with From graph.\n\n"
                "Open Studio opens the full-screen studio (Edit and Generate). Esc closes it. "
                "A queued run outputs the selected history result — image, mask, prompt and info — "
                "without a new API call. With nothing selected, the input image passes through."
            ),
            inputs=[
                io.Image.Input(
                    "image", optional=True,
                    tooltip="Optional image from the graph. The studio imports it with From graph."),
                io.String.Input(
                    "director_state", default="",
                    tooltip="Studio link: project, selected result and prompt. Hidden on the node; "
                            "the studio writes it. Open Studio reads it back."),
            ],
            outputs=[
                io.Image.Output(
                    "image",
                    tooltip="The selected history result, or the input image when nothing is selected."),
                io.Mask.Output(
                    "mask",
                    tooltip="An empty mask the size of the output image. Region marks stay in the studio."),
                io.String.Output(
                    "prompt",
                    tooltip="The prompt stored with the selected result, or the studio prompt."),
                io.String.Output(
                    "info",
                    tooltip="Model, operation, status and cost of the selected result."),
            ],
        )

    @classmethod
    def execute(cls, image=None, director_state=""):
        return _execute_director(image, director_state)


NODE_CLASS_MAPPINGS = {"LunaDirectorLauncher": LunaDirectorLauncher}
NODE_DISPLAY_NAME_MAPPINGS = {"LunaDirectorLauncher": "Luna Director (Studio)"}
