# LLM Prompt — ComfyUI nodes for AI prompt generation

Local GGUF and cloud-API prompt-writing nodes for ComfyUI, with model-family auto-tuning, multimodal inputs, and a `.md`-based preset library.

---

## What's in this pack

| Node | What it does |
|---|---|
| **LLM Prompt** | Local prompt generation via GGUF vision/text models (llama-cpp). Qwen 2.5/3/3.5/3.6 and Gemma 3/4 with automatic family detection. Vision + video + audio inputs. |
| **LLM Prompt (API)** | Cloud prompt generation via OpenAI-compatible endpoints. LM Studio, Google Gemini (native), xAI Grok, OpenRouter, or any custom endpoint. |
| **Grok Imagine (API Key)** | Image and video generation via xAI's Grok Imagine using your own `XAI_API_KEY` (no ComfyUI credits, no proxy). |

All three share the same `prompts/*.md` preset library and the same bulletproof positive/negative output split.

---

## Highlights

- **One node for every local LLM family.** A small adapter registry inside the node detects the model from its filename and picks the right llama-cpp chat handler — `Gemma4ChatHandler`, `Qwen35ChatHandler`, `Qwen3VLChatHandler`, or a fallback chain — so you don't need separate nodes per model.
- **Handler-level thinking control.** `enable_thinking` / `force_reasoning` / `preserve_thinking` are passed directly to the chat handler at load time, not patched in at inference. Reliable thinking-off across Qwen and Gemma.
- **`auto_settings`** automatically applies the official recommended sampling for the detected family (Qwen Unsloth defaults, Gemma Google defaults). One toggle, correct settings every run.
- **Bulletproof positive/negative split.** The node injects an authoritative `[POSITIVE]` / `[NEGATIVE]` marker contract. A hardened multi-format parser accepts brackets, labels, the legacy `|` pipe, or JSON — and strips all markers from the output. No more "the model dropped my pipe" failures.
- **Multimodal inputs.** Image + reference image (style transfer) + video (FPS-subsampled, context-budget-aware) + audio (Gemma-4 audio projector).
- **System prompts live in `prompts/*.md` files** — edit, fork, or add your own without touching code.
- **V3 schema** with a Basic/Advanced UI split (collapse toggle in both ComfyUI frontends).

---

## Installation

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/lunaaispace-eng/LLM_Prompt.git
cd LLM_Prompt
# Restart ComfyUI
```

### Requirements

| Package | Why | Notes |
|---|---|---|
| `llama-cpp-python` | Local GGUF inference | **Use JamePeng's fork** — it includes `Gemma4ChatHandler`, `Qwen35ChatHandler`, `Qwen3VLChatHandler` that the official abetlen builds lack. https://github.com/JamePeng/llama-cpp-python |
| `torch`, `numpy`, `Pillow` | Tensor / image handling | Comes with ComfyUI. |
| `soundfile`, `torchaudio` | Audio input (Gemma-4) | Optional; imported lazily only when an audio input is connected. |
| `PyYAML` | Preset frontmatter parsing | Optional; presets work without it, just without title override. |

### Where to put your models

```
ComfyUI/models/LLM/
├── my-model.gguf
├── mmproj-my-model.gguf        ← multimodal projector (vision)
├── some-vendor/
│   ├── another-model.gguf
│   └── mmproj-another-model.gguf
└── ...
```

The node scans recursively. mmproj projectors are auto-paired with their matching `.gguf` (longest common filename prefix when multiple candidates exist).

---

## Quick start

1. Drop a **LLM Prompt** node onto your canvas.
2. Pick a model from `model_name` (e.g. `gemma-4-26B-A4B-it-...gguf` or `Qwen3-VL-...gguf`).
3. Pick a preset from `system_prompt` (e.g. *Prompt Architect*, *SDXL*).
4. Type your idea in `user_prompt`.
5. Leave `auto_settings` **ON** — it picks the right sampling for your model family.
6. Connect the `positive` and `negative` outputs to your sampler.

That's it. The node loads the model, generates the prompt, splits positive/negative, and caches the model in VRAM for the next run.

---

## The LLM Prompt node (local GGUF)

### Basic widgets (always visible)

| Widget | Purpose |
|---|---|
| `model_name` | GGUF model from `models/LLM`. |
| `system_prompt` | Preset from `prompts/*.md`. |
| `custom_system_prompt` | Override the preset with your own instructions (leave empty to use preset). |
| `user_prompt` | Your idea/subject. |
| `output_format` | `text` (default), `json`, or `list` (numbered scenes). |
| `split_output` | Split the model's positive and negative into the two node outputs. When ON, injects the `[POSITIVE]`/`[NEGATIVE]` marker contract for clean parsing. Turn OFF for single-output presets (Qwen-Image, Z-Image, Flux). |
| `auto_settings` | Apply officially-recommended sampling for the detected family. **Recommended ON.** |
| `disable_thinking` | Skip the model's reasoning block (faster, recommended for prompt writing). |
| `temperature`, `max_tokens`, `n_ctx`, `seed`, `keep_model_loaded` | Standard controls. |

### Advanced widgets (collapsed by default)

`top_p`, `top_k`, `min_p`, `repetition_penalty`, `presence_penalty`, `frequency_penalty`, `preserve_thinking`, `device`, `n_gpu_layers`, `image_min_tokens`, `image_max_tokens`, `video_fps`, `verbose_logging`.

All have reference values in their hover tooltips (e.g. *"Qwen ~0.7, Gemma ~1.0"*).

When `auto_settings` is ON, the family preset overrides the sampler widgets — they only matter when `auto_settings` is OFF.

### Optional inputs (sockets)

| Input | Use case |
|---|---|
| `style` | A style description string from an external node. |
| `width` / `height` | Drives an aspect-ratio composition profile (framing, lens, depth-of-field hints) added to the user prompt. |
| `image` | Vision analysis. |
| `reference_image` | Style transfer — paired with the `Style Transfer Prompt` preset. |
| `video` | `[F,H,W,3]` image batch; FPS-subsampled with token-budget guard. |
| `audio` | Gemma-4 audio projector input. 16 kHz mono, ≤ 60 s. |

### Outputs

- `positive` — the positive prompt (markers stripped).
- `negative` — the negative prompt, or empty if the preset doesn't produce one.

### How auto-settings works

The detected family applies these official defaults:

| Family | temp | top_p | top_k | min_p | presence | rep |
|---|---|---|---|---|---|---|
| Gemma 3/4 | 1.0 | 0.95 | 64 | 0.0 | 0.0 | 1.0 |
| Qwen 3-VL | 0.7 | 0.9 | 20 | 0.05 | 0.0 | 1.1 |
| Qwen 3.5/3.6 dense (text) | 0.7 | 0.8 | 20 | 0.0 | 1.5 | 1.05 |
| Qwen 3.6 MoE "a3b" | 0.6 | 0.95 | 20 | 0.0 | 0.0 | 1.05 |

Plus thinking is forced **off** (never useful for prompt writing) and a no-think ChatML template is installed on text-only Qwen 3.5/3.6 to hard-disable it.

### Vision handler selection (adapter chain)

| Model name matches | Handler chain |
|---|---|
| `gemma-4*` / `gemma4*` | `Gemma4ChatHandler` → `Gemma3ChatHandler` → text-only |
| `gemma-3*` / `gemma3*` | `Gemma3ChatHandler` → text-only |
| `qwen3.5*` / `qwen3.6*` / `qwen35*` / `qwen36*` | `Qwen35ChatHandler` → `Qwen3VLChatHandler` → `Qwen25VLChatHandler` → text-only |
| `qwen*vl*` | `Qwen3VLChatHandler` → `Qwen25VLChatHandler` → text-only |
| anything else | `Qwen3VLChatHandler` → `Qwen25VLChatHandler` → text-only |

If a handler fails to construct (e.g. the latest Gemma-4 QAT mmproj that breaks `Gemma4ChatHandler` in JamePeng 0.3.39), the chain falls through gracefully instead of hard-failing.

---

## The LLM Prompt (API) node

Same preset library, same output behavior, but talking to a cloud or local-server LLM via OpenAI-compatible REST.

Built-in providers:

| Provider | Endpoint | Auth env var |
|---|---|---|
| **LM Studio (local)** | `http://localhost:1234/v1` | none |
| **Gemini** | Native REST (`generativelanguage.googleapis.com`) | `GEMINI_API_KEY` / `GOOGLE_API_KEY` / `GOOGLE_GEMINI_API_KEY` |
| **Grok (xAI)** | `https://api.x.ai/v1` | `XAI_API_KEY` / `GROK_API_KEY` |
| **Custom** | You supply `server_url` | optional `api_key` widget |

Set your keys via `.env` in the node's folder (gitignored) or as system environment variables. The node will auto-discover them.

Vision inputs (`image`, `video`) work on any provider whose model supports it (Gemini 2.5/3, GPT-4o, Claude 3, Grok-4-vision, Llava, Qwen-VL via OpenRouter, etc.). The model dropdown filters non-chat models (embeddings, image-gen, TTS) automatically.

---

## Grok Imagine (API Key) node

Generates images and short videos through xAI's Grok Imagine endpoint using your own `XAI_API_KEY`. No ComfyUI credits, no proxy — direct to xAI. Loaded only if `comfy_api` exposes VIDEO support, so it auto-skips on older ComfyUI builds.

---

## The system prompt library

All presets live in `prompts/*.md`. Each one becomes an option in the `system_prompt` dropdown.

### File format

```markdown
---
title: My Preset Name
---

You are a Visual Prompt Architect for ...

[your full system prompt here]
```

The YAML `title:` line sets the dropdown label. Without frontmatter, the filename (with `_` → spaces) is used.

### The [POSITIVE] / [NEGATIVE] output contract

When `split_output` is **ON**, the node injects this contract at the end of the system prompt:

```
OUTPUT FORMAT — use these exact markers, each on its own line:
[POSITIVE]
<the full positive prompt>
[NEGATIVE]
<the full negative prompt>
Write nothing before [POSITIVE] and nothing after the negative prompt.
```

The hardened parser then accepts (and strips) any of:
1. `[POSITIVE] ... [NEGATIVE] ...` markers
2. `Positive prompt: ... Negative prompt: ...` labels
3. The legacy `pos | neg` pipe
4. JSON `{"positive": ..., "negative": ...}`

…and falls back to "whole text = positive, negative empty" if none match. **No prompt is ever lost.**

For single-output presets (no negative), set `split_output` **OFF** — the marker contract isn't injected and the whole response goes to the positive socket.

### Bundled presets

| Preset | Target | Output format |
|---|---|---|
| **Prompt Architect** | Universal (SDXL, Flux, Chroma, Z-Image, etc.) | Prose paragraph + narrative negative |
| **Prompt Architect Dynamic** | Universal, with adaptive section promotion | Prose paragraph + narrative negative |
| **Prompt Architect Negative / Dynamic Negative** | Same as above, structured for prompts that produce both | `[POSITIVE]` / `[NEGATIVE]` markers |
| **Prompt Architect Labels** | LLM-encoder models (Qwen-Image, HiDream) | Labeled sections |
| **Prompt Architect Negative Labels / Dynamic Negative Labels** | LLM-encoder models, with negative | Labeled sections + negative |
| **SDXL** | SDXL family | Tag-style + negative |
| **Illustrious** | Illustrious / NoobAI | Booru tags + weighted emphasis |
| **Pony** | Pony Diffusion | `score_9, score_8_up...` tag prefix |
| **Qwen Image 2512** | Qwen-Image (Qwen 2.7 LLM TE) | Labeled sections, no negative, no quality buzzwords |
| **Z-Image** | Z-Image / Z-Image Turbo (Qwen 3.4 LLM TE) | Prose, positive-only safety constraints, EN/CN text rendering |
| **Style Author** | Generate style-library JSON entries | JSON |

### Encoder → preset cheat-sheet

| Model encoder | Recommended preset(s) |
|---|---|
| **CLIP** (SDXL, Pony, Illustrious) | SDXL / Illustrious / Pony |
| **T5** (Flux, Chroma, SD3.5) | Prompt Architect (prose) |
| **LLM encoder, weaker** (Qwen 2.7 → Qwen-Image) | Qwen 2512 (labeled) |
| **LLM encoder, stronger** (Qwen 3.4 → Z-Image, Gemma 2 → HiDream) | Z-Image preset, or Prompt Architect (prose) |

---

## Performance notes

- **Model caching:** the loaded model stays in VRAM between runs as long as the load signature is unchanged. Toggle `keep_model_loaded` OFF to free VRAM after each run.
- **ComfyUI-integrated unload:** when the node frees VRAM it also calls `comfy.model_management.unload_all_models()` + `soft_empty_cache()`, so it cooperates with the rest of your graph.
- **Vision token budget:** `image_min_tokens` / `image_max_tokens` control visual resolution per image (Qwen-VL handlers). Higher = finer detail, more VRAM.
- **Video sampling:** FPS-based, capped at 30 frames, with automatic reduction if frames × `~258 tokens` would exceed 70 % of `n_ctx`.

---

## Troubleshooting

**"No GGUF models found"** — check that your files are in a folder ComfyUI lists for `LLM` (typically `ComfyUI/models/LLM/`). The dropdown shows files grouped by their top-level subfolder.

**"Vision handler: failed for all candidates, running text-only"** — the model's mmproj is incompatible with every handler the adapter tried. Common cause: the **latest Gemma-4 QAT mmproj** that breaks `Gemma4ChatHandler` in JamePeng 0.3.39. Use a non-QAT variant, or wait for 0.3.40.

**`unrecognized arguments: force_reasoning`** — your `llama-cpp-python` is too old. Update to the latest JamePeng wheel.

**Model "thinks" for hundreds of tokens before answering** — `auto_settings` should be ON (forces thinking off) and `disable_thinking` should be checked. For aggressive uncensored fine-tunes that ignore both, the node also salvages the final draft paragraph from the reasoning blob.

**`positive` output contains `[POSITIVE]` or `[NEGATIVE]` text** — shouldn't happen with the hardened parser, but if it does, please file an issue with the raw output text.

**Console is too chatty** — turn off `verbose_logging` (Advanced). Only warnings and errors print by default.

---

## Credits

- **JamePeng** for the continuously-updated [`llama-cpp-python` fork](https://github.com/JamePeng/llama-cpp-python) that exposes Gemma-4 / Qwen-3.5 / Qwen-3-VL chat handlers — this node is built on top of it.
- **Duffy Nodes** for the multimodal-analyzer reference architecture that informed the V3 schema migration and the `reference_image` / audio input design.
- **Unsloth** for the published Qwen 3/3.5/3.6 sampling recommendations baked into `auto_settings`.
- **Google** for the Gemma 4 official sampling defaults.

---

## License

MIT — see [LICENSE](LICENSE).

Note: this project depends on ComfyUI (GPL-3.0). If you redistribute a bundled package containing both, that combined work is governed by the GPL.
