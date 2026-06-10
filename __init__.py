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

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]

WEB_DIRECTORY = "./web"
