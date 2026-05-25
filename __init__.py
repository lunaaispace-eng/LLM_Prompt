"""LLM Prompt — Custom ComfyUI nodes for prompt generation via local GGUF and remote APIs.

Two nodes shipped:
  - LLMPrompt         : local GGUF inference via llama-cpp-python
  - LLMPromptAPI      : OpenAI-compatible HTTP — LM Studio, Gemini, Grok,
                        OpenAI, OpenRouter, or any custom endpoint.

Both share the same system prompt presets (.md files), canvas profile
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

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]

WEB_DIRECTORY = "./web"
