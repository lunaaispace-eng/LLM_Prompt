"""The one import helper for tests that need a node module.

The pack is loaded as a stub package `llm_prompt_pack` whose `__path__` is this checkout's
root, so the pack's heavy `__init__.py` never runs and a worktree never imports another
checkout. Every such test imports through `load()`, so one process holds one copy of each
module (one `_RUNNER_LOCK`). `load()` raises `unittest.SkipTest` when ComfyUI is missing.

Same pattern as the inline loader in `test_studio_node.py` (phase 1).
"""
import contextlib
import importlib
import io
import os
import sys
import types
import unittest
import warnings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = r"E:\ComfyUI-Easy-Install\ComfyUI"
for _p in (ROOT, COMFY):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Third-party import noise, not this pack's: torch warns about pynvml, SWIG-built modules
# warn at import and at interpreter exit, and dill leaves os.devnull open until exit.
warnings.filterwarnings("ignore", message=r"builtin type (SwigPy|swigvarlink)", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=r"unclosed file .*name='nul'", category=ResourceWarning)

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import comfy_api.latest  # noqa: F401
        import torch  # noqa: F401
    HAVE_COMFY = True
except Exception:  # pragma: no cover - plain python without ComfyUI
    HAVE_COMFY = False

PKG = "llm_prompt_pack"


def load(name: str):
    """Import `<pack>.<name>` through the stub package; SkipTest without ComfyUI."""
    if not HAVE_COMFY:
        raise unittest.SkipTest("comfy_api not importable (run with the ComfyUI python)")
    if PKG not in sys.modules:
        pkg = types.ModuleType(PKG)
        pkg.__path__ = [ROOT]
        sys.modules[PKG] = pkg
    # The pack's modules print their key / model checks on import; keep the test output clean.
    with contextlib.redirect_stdout(io.StringIO()), warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return importlib.import_module(f"{PKG}.{name}")
