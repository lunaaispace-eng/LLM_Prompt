"""LLM Prompt — Custom ComfyUI nodes for prompt generation via local GGUF and remote APIs.

Nodes shipped:
  - LLMPrompt         : local GGUF inference via llama-cpp-python
  - LLMPromptAPI      : OpenAI-compatible HTTP — Gemini (default), Grok,
                        OpenAI, OpenRouter, or any custom endpoint.
  - Grok Imagine (API Key) : image/video generation via xAI directly using your
                        own XAI_API_KEY (no ComfyUI credits, no proxy).

The LLM nodes share the same system prompt presets (.md files), canvas profile
(width+height → composition guidance), and image/video handling.
"""

from .llm_prompt_node import (
    NODE_CLASS_MAPPINGS as _GGUF_NODES,
    NODE_DISPLAY_NAME_MAPPINGS as _GGUF_NAMES,
)
from .llm_prompt_api_node import (
    NODE_CLASS_MAPPINGS as _API_NODES,
    NODE_DISPLAY_NAME_MAPPINGS as _API_NAMES,
)

NODE_CLASS_MAPPINGS = {**_GGUF_NODES, **_API_NODES}
NODE_DISPLAY_NAME_MAPPINGS = {**_GGUF_NAMES, **_API_NAMES}

# Dataset captioning nodes (Load Image For Caption + Save Caption). Optional —
# only register if the import succeeds, so a failure here never takes down the
# core LLM nodes.
try:
    from .dataset_caption_nodes import (
        NODE_CLASS_MAPPINGS as _CAPTION_NODES,
        NODE_DISPLAY_NAME_MAPPINGS as _CAPTION_NAMES,
    )
    NODE_CLASS_MAPPINGS.update(_CAPTION_NODES)
    NODE_DISPLAY_NAME_MAPPINGS.update(_CAPTION_NAMES)
except Exception as e:  # pragma: no cover
    print(f"[LLM_Prompt] Dataset captioning nodes not loaded: {e}")

# Grok Imagine (BYO key) nodes — optional; only register if the import succeeds
# (keeps the core LLM nodes loading even if comfy_api/VIDEO support is missing).
try:
    from .grok_imagine_nodes import (
        NODE_CLASS_MAPPINGS as _GROK_NODES,
        NODE_DISPLAY_NAME_MAPPINGS as _GROK_NAMES,
    )
    NODE_CLASS_MAPPINGS.update(_GROK_NODES)
    NODE_DISPLAY_NAME_MAPPINGS.update(_GROK_NAMES)
except Exception as e:  # pragma: no cover
    print(f"[LLM_Prompt] Grok Imagine (API Key) nodes not loaded: {e}")

# Gemini Image (BYO key) node — optional; only register if the import succeeds
# (needs google-genai + torch/PIL, same as the rest of the pack).
try:
    from .gemini_image_node import (
        NODE_CLASS_MAPPINGS as _GEMINI_IMG_NODES,
        NODE_DISPLAY_NAME_MAPPINGS as _GEMINI_IMG_NAMES,
    )
    NODE_CLASS_MAPPINGS.update(_GEMINI_IMG_NODES)
    NODE_DISPLAY_NAME_MAPPINGS.update(_GEMINI_IMG_NAMES)
except Exception as e:  # pragma: no cover
    print(f"[LLM_Prompt] Gemini Image (API Key) node not loaded: {e}")

# Gemini Omni Video (BYO key) — Interactions API; optional like the rest.
try:
    from .gemini_omni_node import (
        NODE_CLASS_MAPPINGS as _OMNI_NODES,
        NODE_DISPLAY_NAME_MAPPINGS as _OMNI_NAMES,
    )
    NODE_CLASS_MAPPINGS.update(_OMNI_NODES)
    NODE_DISPLAY_NAME_MAPPINGS.update(_OMNI_NAMES)
except Exception as e:  # pragma: no cover
    print(f"[LLM_Prompt] Gemini Omni Video (API Key) node not loaded: {e}")

# GPT Image (BYO key) — OpenAI Images API; optional like the rest.
try:
    from .openai_image_node import (
        NODE_CLASS_MAPPINGS as _GPT_IMG_NODES,
        NODE_DISPLAY_NAME_MAPPINGS as _GPT_IMG_NAMES,
    )
    NODE_CLASS_MAPPINGS.update(_GPT_IMG_NODES)
    NODE_DISPLAY_NAME_MAPPINGS.update(_GPT_IMG_NAMES)
except Exception as e:  # pragma: no cover
    print(f"[LLM_Prompt] GPT Image (API Key) node not loaded: {e}")

# Luna Image Studio (BYO keys) — every provider over luna_imaging/; optional like the rest.
try:
    from .luna_image_studio_node import (
        NODE_CLASS_MAPPINGS as _STUDIO_NODES,
        NODE_DISPLAY_NAME_MAPPINGS as _STUDIO_NAMES,
    )
    NODE_CLASS_MAPPINGS.update(_STUDIO_NODES)
    NODE_DISPLAY_NAME_MAPPINGS.update(_STUDIO_NAMES)
except Exception as e:  # pragma: no cover
    print(f"[LLM_Prompt] Luna Image Studio (API Key) node not loaded: {e}")

# Luna Director launcher (D2). Optional like the other nodes.
try:
    from .luna_director_node import (
        NODE_CLASS_MAPPINGS as _DIRECTOR_NODES,
        NODE_DISPLAY_NAME_MAPPINGS as _DIRECTOR_NAMES,
    )
    NODE_CLASS_MAPPINGS.update(_DIRECTOR_NODES)
    NODE_DISPLAY_NAME_MAPPINGS.update(_DIRECTOR_NAMES)
except Exception as e:  # pragma: no cover
    print(f"[LLM_Prompt] Luna Director launcher node not loaded: {e}")

# Luna Director routes (/luna/director/*, /luna/studio/*); optional like the nodes.
try:
    from server import PromptServer
    from .luna_director import routes as _director_routes
    _director_routes.install(PromptServer.instance)
except Exception as e:  # pragma: no cover
    print(f"[LLM_Prompt] Luna Director routes not loaded: {e}")

# Writer interface for other packs (ComfyUI-LunaStudio): reachable as sys.modules["luna_writer_api"].
try:
    from . import luna_writer_api as _writer_api
    _writer_api.register()
except Exception as e:  # pragma: no cover
    print(f"[LLM_Prompt] writer interface not registered: {e}")

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]

WEB_DIRECTORY = "./web"
