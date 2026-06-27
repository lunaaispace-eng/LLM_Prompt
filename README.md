# LLM Prompt

ComfyUI nodes for local and API-based prompt generation, built around a Markdown system-prompt library and reliable positive/negative output splitting.

This pack is aimed at image and video generation workflows where an LLM turns a short idea, style block, image, video, or reference input into a cleaner generation prompt.

## Nodes

| Node | What it does |
| --- | --- |
| `LLM Prompt` | Local GGUF prompt generation through `llama-cpp-python`. Supports Qwen, Gemma, Llama-style models, vision projectors, image/reference/video/audio inputs, model-family presets, and model caching. |
| `LLM Prompt (API)` | API prompt generation through Gemini native REST, xAI Grok, or a custom OpenAI-compatible endpoint. Uses the same prompt presets and output splitter as the local node. |
| `Grok Image (API Key)` | Direct xAI Grok Imagine text-to-image using your own `XAI_API_KEY`. |
| `Grok Image Edit (API Key)` | Direct xAI Grok Imagine image edit. |
| `Grok Video (API Key)` | Direct xAI Grok Imagine text/image-to-video. |
| `Grok Reference-to-Video (API Key)` | Direct xAI Grok Imagine video generation from reference images. |
| `Grok Video Edit (API Key)` | Direct xAI Grok Imagine video edit. |
| `Grok Video Extend (API Key)` | Direct xAI Grok Imagine video extension. |

## Highlights

- Local GGUF and cloud/API prompt generation in one pack.
- Shared `prompts/*.md` preset library.
- Hardened `[POSITIVE]` / `[NEGATIVE]` output contract.
- Robust output cleaner for thinking blocks, code fences, role prefixes, planning text, JSON wrappers, and prompt labels.
- Automatic local model scanning from ComfyUI `models/LLM`.
- Automatic mmproj pairing for local vision GGUF models.
- Model-family sampling presets for Qwen, Gemma, Gemini, Grok, SuperGemma, and Llama.
- Guarded `llama_cpp` import so API/Grok nodes can still load if the local GGUF wheel is missing or broken.
- API keys are read from environment variables or `.env`; they are not saved in workflow JSON.
- Basic/Advanced UI split for both local and API nodes.
- No SAM/bbox input workflow in the LLM nodes. That feature was intentionally removed.

## Installation

Clone into your ComfyUI custom nodes folder:

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/lunaaispace-eng/LLM_Prompt.git
```

Restart ComfyUI after installing or updating Python files. Hard-refresh the browser after frontend JavaScript changes.

## Requirements

| Package | Needed for | Notes |
| --- | --- | --- |
| `llama-cpp-python` | `LLM Prompt` local GGUF node | Recommended: JamePeng's fork/build with Gemma4, Qwen35, and Qwen3VL handlers. |
| `torch`, `numpy`, `Pillow` | Tensor/image handling | Usually already present in ComfyUI. |
| `soundfile`, `torchaudio` | Audio input | Optional; used only when audio is connected. |
| `PyYAML` | Prompt preset frontmatter | Optional; without it, filenames become preset labels. |
| `google-genai` | Gemini native API | Needed by `LLM Prompt (API)` when using Gemini. |

Windows users who want the local GGUF node should read the dedicated install guide:
[docs/llama_cpp_windows_install.md](docs/llama_cpp_windows_install.md).
The repo also includes a read-only doctor / explicit installer script:
`tools/llama_cpp_windows_doctor.py`.

## Model Folder

Local GGUF models should live under a folder ComfyUI exposes as `LLM`, usually:

```text
ComfyUI/models/LLM/
|-- model.gguf
|-- mmproj-model.gguf
|-- vendor/
|   |-- another-model.gguf
|   `-- mmproj-another-model.gguf
```

The node scans recursively. Files with `mmproj` in the name are treated as projectors, not model entries. When several projectors are in the same folder, the closest filename match is preferred.

## LLM Prompt

The local node runs GGUF models through `llama-cpp-python`.

Basic widgets:

| Widget | Purpose |
| --- | --- |
| `model_name` | Local `.gguf` model from `models/LLM`. |
| `system_prompt` | Preset loaded from `prompts/*.md`. |
| `custom_system_prompt` | Overrides the selected preset when non-empty. |
| `user_prompt` | The idea, subject, or request to transform. |
| `output_format` | `text`, `json`, or `list`. |
| `split_output` | Splits model output into `positive` and `negative`. |
| `auto_settings` | Applies recommended sampling and thinking controls for known model families. |
| `disable_thinking` | Disables/strips reasoning where supported. |
| `temperature`, `max_tokens`, `n_ctx`, `seed`, `keep_model_loaded` | Main generation/runtime controls. |

Advanced widgets:

```text
top_p
top_k
min_p
repetition_penalty
presence_penalty
frequency_penalty
preserve_thinking
device
n_gpu_layers
image_min_tokens
image_max_tokens
video_fps
verbose_logging
```

Optional inputs:

| Input | Purpose |
| --- | --- |
| `style` | Style text from another node. |
| `width` / `height` | Adds a canvas-format hint to the prompt context. |
| `image` | Vision input. |
| `reference_image` | Style/reference image input. |
| `video` | Frame batch input. |
| `audio` | Audio input for compatible Gemma-style models. |

Outputs:

| Output | Type |
| --- | --- |
| `positive` | `STRING` |
| `negative` | `STRING` |

## LLM Prompt API

The API node uses the same prompt library and splitter, but sends requests to remote or local API servers.

Built-in providers:

| Provider | Endpoint behavior | Auth |
| --- | --- | --- |
| `Gemini` | Native Gemini REST through `google-genai`. Default provider. | `GEMINI_API_KEY`, `GOOGLE_API_KEY`, or `GOOGLE_GEMINI_API_KEY`. |
| `Grok (xAI)` | xAI OpenAI-compatible chat endpoint. | `XAI_API_KEY` or `GROK_API_KEY`. |
| `Custom` | User-supplied OpenAI-compatible `server_url`. Use this for OpenRouter, LM Studio, llama.cpp server, vLLM, Ollama-compatible gateways, etc. | Optional, depending on server. |

The node intentionally has no `api_key` widget. Keys are read from process environment variables or `.env` files so workflow JSON does not leak credentials.

Preferred `.env` location:

```text
ComfyUI/.env
```

Fallback `.env` location:

```text
ComfyUI/custom_nodes/LLM_Prompt/.env
```

API-specific controls include:

```text
provider
model_name
server_url
model_filter
gemini_thinking_budget
gemini_thinking_level
enable_caching
timeout_seconds
stop_sequences
```

`gemini_thinking_level` is for Gemini 3 Pro style models and overrides `gemini_thinking_budget` when set to `low`, `medium`, or `high`.

## Grok Imagine API Key Nodes

These nodes call xAI directly with your own key:

```text
XAI_API_KEY=xai-...
```

or:

```text
GROK_API_KEY=xai-...
```

They do not use ComfyUI credits or a proxy.

Available Grok media nodes:

- `Grok Image (API Key)`
- `Grok Image Edit (API Key)`
- `Grok Video (API Key)`
- `Grok Reference-to-Video (API Key)`
- `Grok Video Edit (API Key)`
- `Grok Video Extend (API Key)`

## Prompt Presets

Preset files live in:

```text
prompts/*.md
```

Current library size: 30 Markdown presets.

Each file can include YAML frontmatter:

```markdown
---
title: My Preset Name
---

System prompt text...
```

If no title is provided, the filename is converted into a display label.

Current preset files:

```text
Backgrounds.md
Chroma.md
Chroma_Negative.md
Chroma_dynamic.md
Chroma_dynamic_Negative.md
Ideogram4 Architect v4.md
Ideogram_Prompt.md
Juggernaut Z_Image.md
Krea2.md
PromptArchitectDynamicNegativeLabels.md
PromptArchitectLabels.md
PromptArchitectNegativeLabels.md
Prompt_Architect_Detailed.md
Z-Image_Prompt_Architect.md
describe_image.md
illustrious.md
image_analysis.md
image_caption.md
image_edit.md
multi_image_compose.md
pony.md
prompt Architect.md
prompt_architect_dynamic.md
prompt_architect_dynamic_negative.md
prompt_architect_negative.md
reverse_engineered_prompt.md
sdxl.md
style_transfer_prompt.md
tags.md
z_image.md
```

## Positive / Negative Split

When `split_output` is enabled, the node appends an output contract asking the model to use:

```text
[POSITIVE]
...
[NEGATIVE]
...
```

The parser accepts:

- `[POSITIVE]` / `[NEGATIVE]` markers
- `Positive prompt:` / `Negative prompt:` labels
- JSON objects with `positive` and `negative` fields
- legacy pipe-separated `positive|negative`

All markers and labels are stripped from the final outputs. If no split is found, the whole response goes to `positive` and `negative` is empty.

## Advanced UI

`web/llm_prompt_advanced.js` adds an `Advanced` toggle for `LLMPrompt` and `LLMPromptAPI`.

The current extension owns the fold behavior in both classic canvas and modern Vue/DOM node modes. It hides advanced widgets through both legacy widget hiding and Vue-compatible `options.hidden`, then nudges the frontend to re-render.

The button is appended at the end of `node.widgets` to avoid corrupting saved widget positions.

## Frontend Helpers

| File | Purpose |
| --- | --- |
| `web/llm_prompt_advanced.js` | Advanced widget folding for local/API nodes. |
| `web/llm_prompt_presets.js` | Model-family sampler autofill and callback self-healing. |
| `web/llm_prompt_api.js` | API provider/model dropdown refresh and Gemini-only widget visibility. |

## Removed Feature

The former SAM/bbox input workflow was removed from both LLM nodes.

Removed from `LLM Prompt` and `LLM Prompt (API)`:

- `bboxes` input
- `bbox_min_score` input
- `_sam_region_block` prompt injection

Ideogram-style bbox generation/output in other node packs is unrelated and unaffected.

## Troubleshooting

**No GGUF models found**
Check that `.gguf` files are in `ComfyUI/models/LLM` or another folder registered with ComfyUI as an LLM model path.

**Local node fails because llama-cpp is missing or broken**
The pack should still load API/Grok nodes. The local node raises a clear error only when it needs to load a GGUF model.

**API key not found**
Set the key before launching ComfyUI, or add it to `ComfyUI/.env`.

**Advanced button looks stale after update**
Hard-refresh the browser with `Ctrl+Shift+R`.

**Python node changes do not appear**
Restart ComfyUI fully. Browser refresh is not enough for Python changes.

**Prompt output still contains labels or markers**
That should be stripped by `output_cleaner.py`. Keep the raw model output for debugging if it happens.

## Credits

Special thanks to:

- [Duffy Nodes](https://github.com/elmarkrueger/Duffy_Nodes) for the architecture reference around multimodal handlers, thinking controls, V3 schema patterns, reference image handling, and audio input design.
- [JamePeng llama-cpp-python](https://github.com/JamePeng/llama-cpp-python) for the Gemma/Qwen handler support this node relies on.
- Unsloth for Qwen sampling recommendations.
- Google for Gemma/Gemini model defaults and APIs.

## License

MIT. See [LICENSE](LICENSE).

This project depends on ComfyUI. If redistributed as a bundled package with ComfyUI, the combined distribution may be subject to ComfyUI's GPL-3.0 license terms.
