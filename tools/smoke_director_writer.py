"""Live smoke for the Director writer (task A4). MANUAL - not discovered by unittest.

A transport check only: one write per route on a fixed 768x768 test image with a centre mask, at
the cheapest settings. The A7 presets do not exist yet, so every call overrides `preset` with an
existing general preset (otherwise the missing-preset error fires); output quality is judged in A7.

The subscription routes spend that subscription's quota; Gemini spends API money (cents). The
local GGUF route loads a model on the GPU - run it only when ComfyUI is not holding the VRAM.

Run with the ComfyUI python:

    E:\\ComfyUI-Easy-Install\\python_embeded\\python.exe tools\\smoke_director_writer.py
        [--only gemini,claude,codex,grok,gguf,gen-gemini,gen-gguf] [--gguf-model NAME] [--dry]

--dry swaps write_prompt_api and the GGUF runner for fakes, so the script itself can be checked
without a network, a CLI or a model. Keys are read by the API node from env / .env and never
printed. Exit code = number of FAILs.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import os
import sys
import time
import types
import warnings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = r"E:\ComfyUI-Easy-Install\ComfyUI"
for _p in (ROOT, COMFY):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from PIL import Image, ImageDraw  # noqa: E402

PKG = "llm_prompt_pack"
PRESET = "Image Edit"
# The A7 generation presets do not exist yet and no general preset writes the [VARIANT n] form, so the
# generate check uses this in-memory stand-in (never written to prompts/). Transport + parse only.
GEN_PRESET = "Smoke Generate (in-memory)"
GEN_PRESET_TEXT = (
    "You write image-generation prompts. Write as many different prompts as VARIANTS asks, each a "
    "genuinely different take on the idea. Output exactly this form for each, nothing else:\n"
    "[VARIANT 1]\n[SUBJECT] ...\n[STYLE] ...\n[COMPOSITION] ...\n[LIGHTING] ...\n[CAMERA] ...\n"
    "[NEGATIVE] ...\n\nthen [VARIANT 2] and so on.")
ROUTES = {
    "gemini": ("Gemini", "gemini-3.5-flash-lite"),
    "claude": ("Claude (Max)", "claude-haiku-4-5"),
    "codex": ("Codex (ChatGPT)", "gpt-5.6-luna"),
    "grok": ("Grok (SuperGrok)", "grok-4.5"),
    "gguf": ("Local GGUF", None),
}
REGION_WORDS = ("apple", "centre", "center", "middle", "table", "bowl", "fruit")


def load(name):
    if PKG not in sys.modules:
        pkg = types.ModuleType(PKG)
        pkg.__path__ = [ROOT]
        sys.modules[PKG] = pkg
    with contextlib.redirect_stdout(io.StringIO()), warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return importlib.import_module(f"{PKG}.{name}")


def test_image():
    img = Image.new("RGB", (768, 768), (200, 190, 170))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 520, 768, 768), fill=(120, 80, 50))      # table
    d.ellipse((300, 380, 470, 550), fill=(230, 220, 200))    # bowl
    d.rectangle((60, 100, 260, 400), fill=(90, 120, 160))    # window
    mask = Image.new("L", (768, 768), 0)
    ImageDraw.Draw(mask).rectangle((284, 284, 484, 484), fill=255)
    return img, mask


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="gemini,claude,codex,grok,gguf,gen-gemini,gen-gguf")
    ap.add_argument("--gguf-model", default="")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    W = load("luna_director.writer")
    api = load("llm_prompt_api_node")
    node = load("llm_prompt_node")
    # Run from the dev checkout, the node's "ComfyUI root" (three folders up) is not the install;
    # point it there so the API node finds the install's .env (the key stays inside the node).
    from pathlib import Path
    api._comfyui_root = lambda: Path(COMFY)
    real_presets = W._presets
    W._presets = lambda: {**real_presets(), GEN_PRESET: GEN_PRESET_TEXT}
    img, mask = test_image()
    gguf_model = args.gguf_model or next((k for k in sorted(node._SCANNED_MODELS) if "qwen3-vl" in k.lower()), "")

    if args.dry:
        api.write_prompt_api = lambda **kw: (("[VARIANT 1]\n[SUBJECT] a\n[VARIANT 2]\n[SUBJECT] b c d\n"
                                              "[VARIANT 3]\n[SUBJECT] e f g h", "", "")
                                             if not kw["split_output"] else ("a red apple in the bowl", "", ""))
        node._RUNNER.generate = lambda **kw: (("[VARIANT 1]\n[SUBJECT] a\n[VARIANT 2]\n[SUBJECT] b c d\n"
                                               "[VARIANT 3]\n[SUBJECT] e f g h", "", "")
                                              if not kw["split_output"] else ("a red apple in the bowl", "", ""))
        gguf_model = gguf_model or "dry.gguf"

    fails = 0
    for name in [n.strip() for n in args.only.split(",") if n.strip()]:
        gen = name.startswith("gen-")
        provider, model = ROUTES[name[4:] if gen else name]
        if provider == "Local GGUF":
            model = gguf_model
            if not model:
                print(f"SKIP {name}: no Qwen3-VL GGUF found (pass --gguf-model)")
                continue
        if gen:
            req = W.WriterRequest(provider=provider, model=model, preset=GEN_PRESET, target_model="gemini-3.1-flash-image-preview",
                                  operation="generate", request="a lighthouse keeper at dawn", size=(1024, 1024),
                                  variants=3, sections=True, timeout=240)
        else:
            req = W.WriterRequest(provider=provider, model=model, preset=PRESET, target_model="gemini-3.1-flash-image-preview",
                                  operation="inpaint", request="put a red apple in the bowl", canvas=img, mask=mask,
                                  timeout=240)
        t0 = time.perf_counter()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                res = W.write(req)
        except Exception as e:  # report every route, then the summary
            fails += 1
            code = getattr(e, "code", type(e).__name__)
            print(f"FAIL {name} ({provider} {model}) {time.perf_counter() - t0:.1f}s [{code}] {e}")
            continue
        secs = time.perf_counter() - t0
        if gen:
            print(f"OK   {name} ({provider} {model}) {secs:.1f}s variants={len(res.variants)} "
                  f"sections={[bool(v['sections']) for v in res.variants]}")
        else:
            named = any(w in res.positive.lower() for w in REGION_WORDS)
            print(f"OK   {name} ({provider} {model}) {secs:.1f}s region_named={named}")
            print(f"     {res.positive[:300]!r}")
    return fails


if __name__ == "__main__":
    sys.exit(main())
