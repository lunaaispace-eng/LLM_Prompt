"""LLM Prompt — Custom ComfyUI node for prompt generation via local GGUF models.

Single node, .md system prompts, vision support, Qwen 3.5 optimized.
"""

from .llm_prompt_node import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]

WEB_DIRECTORY = None
