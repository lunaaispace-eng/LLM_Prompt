# LLM Prompt

ComfyUI nodes for local and API-based prompt generation, built around a Markdown system-prompt library and reliable positive/negative output splitting.

This pack is aimed at image and video generation workflows where an LLM turns a short idea, style block, image, video, or reference input into a cleaner generation prompt.

### API video input

Connect a ComfyUI **VIDEO** output to `LLM Prompt (API)`'s `video` socket. To
assemble frames and a soundtrack, use **Create Video** with images, audio, and
the correct source frame rate. An image batch alone contains no audio.

- `video_input_mode = auto` sends native video to Gemini, including embedded
  audio; other providers receive sampled still images. `native_video` explicitly
  requires Gemini. `sampled_frames` sends silent stills with any provider.
- `gemini_video_fps` controls Gemini's native visual sampling rate (default **2
  fps**, range 0.1–24). It does not change playback speed or remove audio.
- `video_sample_frames` caps the evenly spaced still frames in sampled mode
  (default **32**, range 1–256). `vision_mp` applies to these stills and reference
  images, not the native video.
- The existing `frames` socket still describes the **target clip length for H3
  validation**; it does not control input sampling.

Native mode exports the logical clip as MP4/H.264, retaining its audio and trim,
uploads it through Gemini Files, waits for processing, and deletes that temporary
upload after generation (including error paths). Higher sampling rates/counts
increase token use and cost. `timeout_seconds` also bounds the processing wait
and individual SDK HTTP requests, rather than the entire export/upload/generation
sequence. The `log` output identifies native video versus silent sampled frames.
Image batches remain supported alongside video, in their original image order.

Requires a video-capable Gemini model and `google-genai` with `VideoMetadata.fps`
support. See [Google's video input documentation](https://ai.google.dev/gemini-api/docs/generate-content/video-understanding).

## Nodes

| Node | What it does |
| --- | --- |
| `LLM Prompt` | Local GGUF prompt generation through `llama-cpp-python`. Supports Qwen, Gemma, Llama-style models, vision projectors, image/reference/video/audio inputs, model-family presets, and model caching. |
| `LLM Prompt (API)` | API prompt generation through Gemini native REST, xAI Grok, or a custom OpenAI-compatible endpoint. Uses the same prompt presets and output splitter as the local node. |
| `Gemini Image (API Key)` | Google Gemini image generation and editing (Nano Banana / Nano Banana Pro / Nano Banana 2) using your own `GEMINI_API_KEY`. Live model list, up to 4K, reference-image editing, multimodal text + thought-image outputs. |
| `Gemini Omni Video (API Key)` | Google Gemini Omni text/image-to-video and video edit over the Interactions API, with your own `GEMINI_API_KEY`. |
| `GPT Image (API Key)` | OpenAI GPT Image generation and editing (gpt-image-2.5 flare / sunburst, gpt-image-2, 1.x) with your own `OPENAI_API_KEY`: free sizes up to 3840x2160, multi-image edit, MASK inpainting, transparent background. |
| `Luna Image Studio (API Key)` | One image node for OpenAI, Gemini and Grok on your own keys: generate, edit, compose, inpaint (MASK and/or bbox region, on every provider) and outpaint. Outside pixels always stay the original's; cost in `info`. |
| `Grok Image (API Key)` | Direct xAI Grok Imagine text-to-image using your own `XAI_API_KEY`. |
| `Grok Image Edit (API Key)` | Direct xAI Grok Imagine image edit. |
| `Grok Video (API Key)` | Direct xAI Grok Imagine text/image-to-video. |
| `Grok Reference-to-Video (API Key)` | Direct xAI Grok Imagine video generation from up to 14 reference images and up to 3 voices. |
| `Grok Video Frames (API Key)` | Direct xAI Grok Imagine video from a first frame, last frame and/or keyframes. |
| `Grok Video Edit (API Key)` | Direct xAI Grok Imagine video edit. |
| `Grok Video Extend (API Key)` | Direct xAI Grok Imagine video extension. |

## Highlights

- Local GGUF and cloud/API prompt generation in one pack.
- Shared `prompts/*.md` preset library.
- Hardened `[POSITIVE]` / `[NEGATIVE]` output contract.
- Robust output cleaner for thinking blocks, code fences, role prefixes, planning text, JSON wrappers, and prompt labels.
- Automatic local model scanning from ComfyUI `models/LLM`.
- Automatic mmproj pairing for local vision GGUF models.
- Model family detected from the GGUF `general.architecture` header, not the filename, so repackaged quants route correctly.
- Model-family sampling presets for Qwen, Gemma, Gemini, Grok, SuperGemma, and Llama.
- Reasoning is surfaced on the `log` output when thinking is enabled, instead of being silently discarded.
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
| `load_mmproj` | Whether to pair the model with its vision projector. `auto` (default) loads it only when image/video/audio is connected, which frees ~0.6-1.1 GB of VRAM and the reserved image-token budget on text-only runs. `always` keeps it loaded so bypassing an image node does not change the load signature and force a full model reload. `never` is text-only regardless. VL models always keep their projector. |

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
| `log` | `STRING` - h3 validation report when `validate=h3`, and the model's reasoning when thinking is enabled. Empty otherwise. |

## LLM Prompt API

The API node uses the same prompt library and splitter, but sends requests to remote or local API servers.

Built-in providers:

| Provider | Endpoint behavior | Auth |
| --- | --- | --- |
| `Gemini` | Native Gemini REST through `google-genai`. Default provider. | `GEMINI_API_KEY`, `GOOGLE_API_KEY`, or `GOOGLE_GEMINI_API_KEY`. |
| `Grok (xAI)` | xAI OpenAI-compatible chat endpoint. | `XAI_API_KEY` or `GROK_API_KEY`. |
| `OpenAI` | OpenAI Chat Completions, dropdown limited to the GPT-5.6 and GPT-6 families. | `OPENAI_API_KEY`. |
| `Claude (Max)` | Your Claude Max **subscription** through the Claude Code CLI (`claude -p`) — no API key. | The CLI's own login. |
| `Codex (ChatGPT)` | Your ChatGPT **subscription** through the Codex CLI (`codex exec`), read-only sandbox. | The CLI's own login. |
| `Grok (SuperGrok)` | Your SuperGrok **subscription** through the Grok CLI; the API key is hidden from it so it bills the grok.com login. | The CLI's own login. |
| `Custom` | User-supplied OpenAI-compatible `server_url`. Use this for OpenRouter, LM Studio, llama.cpp server, vLLM, Ollama-compatible gateways, etc. | Optional, depending on server. |

The three subscription providers run the vendor's own CLI headless (the CLI must be installed and logged
in). They use that subscription's quota, take a few seconds of CLI start-up per call (6-9 s measured), ignore
the sampling sliders, and map `reasoning_effort` to the CLI's effort setting. Images are supported on all three;
Grok downscales them to fit Windows' command-line limit.

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

## Gemini Image (API Key)

One node for every Google Gemini image model, calling Google directly with the same
`GEMINI_API_KEY` the `LLM Prompt (API)` node uses — no ComfyUI credits, no proxy, and the key
is never stored in the workflow. The `prompt` input is a plain string, so the text output of
`LLM Prompt (API)` wires straight into it: write the prompt with Gemini, render it with Gemini.

**Model list is live.** The dropdown is built from `ListModels` on your own key and cached for
24 hours in `.gemini_models.json`; new Google image models appear on their own. `refresh_models`
forces a re-query, and `model: auto` picks the best one your key can see.

Models seen on a current key:

| Model | Sizes | Reference images | Thinking |
| --- | --- | --- | --- |
| `gemini-3-pro-image` (Nano Banana Pro) | 1K / 2K / 4K | 14 | always on |
| `gemini-3.1-flash-image` (Nano Banana 2) | 0.5K / 1K / 2K / 4K | 14 | `minimal` / `high` |
| `gemini-3.1-flash-lite-image` | 1K | 14 | none |
| `gemini-2.5-flash-image` (Nano Banana, legacy) | 1K | 3 | none |

Aspect ratios: `1:1 2:3 3:2 3:4 4:3 4:5 5:4 9:16 16:9 21:9`, or `auto` to let the model decide
(which also lets an edit inherit the aspect of its reference image). Asking a model for a size
it does not support is clamped down with a console note instead of failing the run.

**Safety.** `safety` defaults to `block_none`, sending `BLOCK_NONE` on all four configurable
categories — the same setting the `LLM Prompt (API)` node uses for Gemini. Google's hard
server-side filter still applies and cannot be changed from here; when it fires, the error
carries `block_reason` (the prompt was rejected) and/or `finish_reason` (the image was).

**Editing and reference images.** `reference_images` is a growing list of sockets: connect one
and the next appears, up to 14 — the documented per-request ceiling. Each slot also accepts a
batch, so a Batch Images node still works and its frames are expanded in place. References are
sent before the instruction, which is the order Google's own editing examples use.

The 14 above is the *total*; Google publishes tighter budgets per reference kind, inside that
total. Exceeding one of these does not error — the result just degrades:

| Model | Objects (high-fidelity) | Character consistency | Style references |
| --- | --- | --- | --- |
| `gemini-3-pro-image` | 6 | 5 | not supported |
| `gemini-3.1-flash-image` | 10 | 4 | 3 |
| `gemini-3.1-flash-lite-image` | 14 | not supported | not supported |

**Multimodal outputs.** A Gemini image response is a stream of parts, not an image: one call can
return interleaved text and several images, and on the thinking models some of those images are
interim drafts the model made while composing. They come out separately:

| Output | Contents |
| --- | --- |
| `image` | The final render(s). |
| `thought_images` | The interim composition drafts. Requires `include_thought_images` — without it the API sends none at all. Black 64×64 placeholder when empty. |
| `text` | Anything the model said alongside the image. |
| `info` | Model, settings actually sent, sizes, timings, and every clamp/ignore note. |

`batch_count` is N sequential calls with the seed stepped by one, because Gemini returns one
image per call — so N images cost N times as much.

## GPT Image (API Key)

OpenAI's Images API called directly with `OPENAI_API_KEY` — no Codex CLI, no ComfyUI credits.
With no reference image it generates; connect any image to `reference_images` and it edits.
The inputs follow `Gemini Image (API Key)`: `prompt`, `model`, `aspect_ratio`, `resolution`,
`batch_count`, `seed`, growing `reference_images`, plus optional `width` / `height` from a
size node (the output is then resized to exactly that size).

| Model | Sizes | Quality | Transparent | `input_fidelity` |
| --- | --- | --- | --- | --- |
| `gpt-image-2.5-flare` (fast default) | any WxH, /16, up to 3840 | low … high, `xhigh`, `max` | yes | no |
| `gpt-image-2.5-sunburst` (precision edits) | any WxH, /16, up to 3840 | low … high, `xhigh`, `max` | yes | no |
| `gpt-image-2` | any WxH, /16, up to 3840 | low … high | **no** | no |
| `gpt-image-1.5`, `gpt-image-1`, `chatgpt-image-latest` | 1024², 1536x1024, 1024x1536 | low … high | yes | yes |
| `gpt-image-1-mini` | same three | low … high | yes | no |

`resolution` is a pixel budget for the free-size models (1K ≈ 1 MP, 2K ≈ 4 MP, 4K = 3840x2160);
sizes are snapped to the API's rules. On the fixed-size models the nearest of the three sizes is
used. Settings a model cannot take are lowered or dropped with a note in `info`, not failed.

**Editing.** Every connected image goes to `/v1/images/edits` (live: 101 images accepted on the
2.x models). The first image is the one being edited; the rest are references. Connect a ComfyUI
`MASK` — from the mask editor, SAM or any segmentation node — to repaint only the white area of
the first image (`invert_mask` flips it). The `alpha` output carries the transparency when
`background` is `transparent`. There is no seed in the API; `seed` only re-runs the node.

**Billing is by tokens, not per image.** `info` prints the cost computed from the response's
`usage` (text in $5, image in $8, image out $30 per 1M on 2.5 and gpt-image-2). Output tokens
are fixed per size and quality — at 1024x1024 on 2.5: low 196 (~$0.006), medium 439 (~$0.013),
high 1756 (~$0.053), xhigh 3122 (~$0.094), max 7024 (~$0.21). Reference images bill as image
input tokens. `xhigh` / `max` are 2.5-only and are just more tokens and time, not a reasoning
mode: the Images API rejects every reasoning/thinking parameter.

**`prompt_rewriter` (opt-in, off by default)** sends the job through the Responses API: a
mainline model (`gpt-6-sol`, `gpt-5.6-sol`, …) reasons at `rewriter_effort`, rewrites the
prompt and calls the image model as a tool; the rewrite is shown in `info`. In a side-by-side
on a text-heavy infographic it gave no measurable gain in text accuracy over a direct call and
sometimes added unrequested detail, for extra mainline tokens and ~10 s — so it is a prompt
expander, not a quality switch. Mask edits always go direct.

## Luna Image Studio (API Key)

One image node for every provider, on your own keys: OpenAI GPT Image (`OPENAI_API_KEY`),
Gemini / Nano Banana (`GEMINI_API_KEY`) and Grok Imagine (`XAI_API_KEY`). The provider follows
the `model`; keys are read from env / `.env` only. The provider logic lives in `luna_imaging/`,
which has no ComfyUI or torch dependency.

| `operation` | Needs | What happens |
| --- | --- | --- |
| `generate` | no image | Text-to-image. |
| `edit` (default) | 1+ image | Whole-image edit; the other images are references. |
| `compose` | 1+ image | Combine the connected images into one. |
| `inpaint` | image + `mask` and/or `bboxes` | Change only the region of the first image. |
| `outpaint` | image + an `outpaint_*` margin | Extend the first image by left / top / right / bottom pixels. |

**Regions.** `mask_mode auto` uses the provider's native mask where it has one (OpenAI) and
otherwise crops the region (plus `crop_padding` of context), edits the crop and composites it
back. Either way the result is composited onto the original, so pixels outside the region always
stay the original's. `feather` blends inward only. A `MASK` of any resolution is fitted to the
first image; `invert_mask` flips the `MASK` only. The `mask` output is the region actually used
(1 = edited), or the alpha when `background` is `transparent` and the result has one.

**`bboxes`** is JSON on the first image: `[[x, y, w, h], ...]`. Values are pixels, or fractions
of the image when every value is ≤ 1 (`[[0, 0, 0.5, 0.5]]` = the top-left quarter). Boxes are
unioned with the `MASK`.

**Cost.** `info` ends with `cost : $x.xxxx` — OpenAI from the response's token usage, Gemini per
image by size, Grok from the cost xAI reports — or `cost : n/a` when it is not known. `seed`
only re-runs the node; it is not sent.

## Gemini Omni Video (API Key)

Google's Omni video model through the Interactions API (Omni rejects `generateContent`). Omni
outputs **video only** — 10 s, 24 fps, with audio; there is no image output and no duration
control. Aspect `16:9` / `9:16`; resolution `360p` / `720p` / `1080p` / `4k` (360p ≈ $0.34 per
clip, 720p ≈ $1.01).

- `generate` — text, or up to 3 `reference_images` (one image = image-to-video).
- `edit video` — wire an earlier Omni node's `interaction_id` into `previous_interaction_id` to
  edit that clip server-side. Editing an *uploaded* clip is not offered in the EEA, Switzerland
  or the UK and comes back as `content_blocked` there.
- `extend video` — needs the uploaded clip (Google rejects `previous_interaction_id` for
  extend), so the same regional limit applies.

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
- `Grok Video Frames (API Key)`
- `Grok Video Edit (API Key)`
- `Grok Video Extend (API Key)`

Per-model limits (checked against the live API, 2026-10-04):

| Model | Notes |
| --- | --- |
| `grok-imagine-image` | $0.02. Edit takes up to 5 input images. |
| `grok-imagine-image-2.0` | Resolution 1K / 1.5K / 2K, `quality` low / medium / auto; $0.04–0.08 by quality × resolution. Edit up to 5 images. |
| `grok-imagine-image-quality` (alias `-pro`) | Edit up to 3 images. **Retires 2026-11-02**, then served as 2.0 at quality low. |
| `grok-imagine-video-1.5` | Text/image-to-video up to 1080p ($0.08/s at 480p). Reference-to-video with up to **14** images and up to 3 voices, first/last frame and keyframes — all capped at 720p. |
| `grok-imagine-video-1.5-lite` | Cheaper 1.5 tier (measured $0.02/s 480p, $0.03/s 720p, $0.14/s 1080p): text/image-to-video only. |
| `grok-imagine-video` | $0.05/s. The only model xAI accepts for video edit (output ≤ 8.7 s, ≤ 720p) and extend (adds 2–10 s). |

- `Grok Video Frames (API Key)` (1.5): pin a `first_frame`, a `last_frame` (interpolates between
  the two) and up to 4 keyframes at given times (strictly inside the clip, 1/3 s grid).
- `Grok Reference-to-Video`: `voice_1..3` pick from xAI's 26 preset voices (or
  `custom_voice_ids`), referenced in the prompt as `<AUDIO_0>`…; images are `<IMAGE_0>`…; an
  optional `first_frame` is pinned as `<IMAGE_0>`.
- Video nodes have `generate_audio` (off strips the audio track). The console prints each job's
  cost from xAI's `cost_in_usd_ticks`. `seed` is not an xAI field; it only re-runs the node.

## Prompt Presets

Preset files live in:

```text
prompts/*.md
```

Current library size: 37 Markdown presets.

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
Krea2_Architect_Adult_NSFW.md
Krea2_Architect_Adult_NSFW_V1.md
Krea2_Architect_General.md
Krea2_Architect_General_V1.md
MinimaxH3_I2V_Architect.md
MinimaxH3_Ref2VA_Architect.md
MinimaxH3_T2V_Architect.md
MinimaxH3_i2v_Vision.md
MinimaxH3_ref2va_Vision.md
PromptArchitectDynamicNegativeLabels.md
PromptArchitectLabels.md
PromptArchitectNegativeLabels.md
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
