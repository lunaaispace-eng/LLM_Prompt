"""LLM Prompt (API): characterization of `execute` and the extracted `write_prompt_api` core.

Director plan, task A1. Every send function is replaced by a recorder and the key resolver
returns "k", so no test reaches a network, a CLI or a real key on this machine.
"""
import base64
import contextlib
import copy
import hashlib
import inspect
import io
import json
import unittest
from unittest import mock

import _comfy

REPLY = "[POSITIVE]\nA\n[NEGATIVE]\nB"
SYS = "You write image prompts."
SENDS = ("_send_chat_completion", "_send_gemini_native", "_send_claude_cli",
         "_send_codex_cli", "_send_grok_cli")

# Pinned from the pre-extraction `execute` (commit ad4d37d). Image data URLs are replaced by
# "<PNG WxH MODE md5-of-pixels>" so the pin holds the pixels, not one zlib's byte stream.
EXPECTED_MESSAGES = [
    {
        "role": "system",
        "content": (
            "You write image prompts.\n\nReturn only the final prompt text. No preface, no "
            "explanations, no JSON, no markdown fences.\n\nIf your instructions produce a NEGATIVE "
            "prompt, format your entire answer EXACTLY like this, each marker on its own line:\n"
            "[POSITIVE]\n<the full positive prompt>\n[NEGATIVE]\n<the full negative prompt>\n"
            "Write nothing before [POSITIVE] and nothing after the negative prompt. If there is no "
            "negative prompt, write [POSITIVE] then your prompt and stop."),
    },
    {
        "role": "user",
        "content": [
            {"type": "text",
             "text": "CANVAS FORMAT: 1:1 square (8x8).\n\nUSER REQUEST:\na red fox in snow"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,<PNG 8x8 RGB d222d3f89806b92abb09f06d7dd3dd5b>"}},
        ],
    },
]


def _image():
    import torch
    return (torch.arange(192, dtype=torch.float32).reshape(1, 8, 8, 3) / 191.0)


def _describe_png(b64: str) -> str:
    from PIL import Image
    im = Image.open(io.BytesIO(base64.b64decode(b64)))
    assert im.format == "PNG", im.format
    return f"<PNG {im.size[0]}x{im.size[1]} {im.mode} {hashlib.md5(im.tobytes()).hexdigest()}>"


def _norm(obj):
    """Deep copy with every base64 PNG data URL replaced by its pixel description."""
    obj = copy.deepcopy(obj)

    def walk(o):
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [walk(v) for v in o]
        if isinstance(o, str) and o.startswith("data:image/png;base64,"):
            return "data:image/png;base64," + _describe_png(o.split(",", 1)[1])
        return o
    return walk(obj)


def _node_kw(**over):
    kw = dict(provider="OpenAI", model_name="gpt-5.6-luna", server_url="", model_filter="all",
              system_prompt="None", custom_system_prompt=SYS, user_prompt="a red fox in snow",
              output_format="text", split_output=True, max_tokens=4096, temperature=0.7,
              top_p=0.9, top_k=0, presence_penalty=0.0, frequency_penalty=0.0, seed=0,
              stop_sequences="", gemini_thinking_budget=0, gemini_thinking_level="None",
              enable_caching=False, disable_thinking=True, timeout_seconds=120)
    kw.update(over)
    return kw


class _Recorder:
    def __init__(self, reply=REPLY):
        self.reply = reply
        self.calls = []

    def make(self, name):
        def fn(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self.reply
        return fn

    def one(self):
        assert len(self.calls) == 1, self.calls
        return self.calls[0]

    def messages(self):
        name, args, kwargs = self.one()
        return kwargs["messages"] if "messages" in kwargs else args[1]


class WriterApiCoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _comfy.load("llm_prompt_api_node")
        cls.Node = cls.mod.LLMPromptAPINode

    @contextlib.contextmanager
    def _patched(self, reply=REPLY, key="k"):
        rec = _Recorder(reply)
        patches = {name: rec.make(name) for name in SENDS}
        patches["_resolve_api_key"] = lambda *a, **k: key
        patches["_gemini_create_cached_content"] = lambda **k: None
        with mock.patch.multiple(self.mod, **patches), contextlib.redirect_stdout(io.StringIO()):
            yield rec

    def _schema(self):
        # Live model lists are patched out: no network, and no key read from this machine.
        with mock.patch.object(self.mod, "_resolve_api_key", return_value="k"), \
             mock.patch.object(self.mod, "_fetch_models_from_server", return_value=[]), \
             contextlib.redirect_stdout(io.StringIO()):
            return self.Node.define_schema()

    def _execute(self, reply=REPLY, key="k", **kw):
        with self._patched(reply, key) as rec:
            out = self.Node.execute(**_node_kw(**kw))
        return out, rec

    # ---- Step 1: characterization of today's execute ---------------------------------

    def test_messages_identical_openai(self):
        _, rec = self._execute(provider="OpenAI", image=_image())
        self.assertEqual(rec.one()[0], "_send_chat_completion")
        self.assertEqual(_norm(rec.messages()), EXPECTED_MESSAGES)

    def test_messages_identical_gemini(self):
        _, rec = self._execute(provider="Gemini", model_name="gemini-3.6-flash", image=_image())
        self.assertEqual(rec.one()[0], "_send_gemini_native")
        self.assertEqual(_norm(rec.messages()), EXPECTED_MESSAGES)

    def test_cli_route_claude(self):
        for key in ("k", None):  # a subscription route needs no API key
            with self.subTest(key=key):
                out, rec = self._execute(key=key, provider="Claude (Max)",
                                         model_name="claude-sonnet-5-5", image=_image(),
                                         timeout_seconds=90, reasoning_effort="medium")
                name, args, kwargs = rec.one()
                self.assertEqual(name, "_send_claude_cli")
                self.assertEqual(kwargs, {})
                self.assertEqual(len(args), 4)
                self.assertEqual(args[0], "claude-sonnet-5-5")
                self.assertEqual(_norm(args[1]), EXPECTED_MESSAGES)
                self.assertEqual(args[2:], (90.0, "medium"))
                self.assertIsInstance(args[2], float)
                self.assertEqual(out.args, ("A", "B", ""))

    def test_cli_routes_codex_and_supergrok(self):
        for provider, model, send in (("Codex (ChatGPT)", "gpt-6-luna", "_send_codex_cli"),
                                      ("Grok (SuperGrok)", "grok-4.7", "_send_grok_cli")):
            with self.subTest(provider=provider):
                out, rec = self._execute(key=None, provider=provider, model_name=model,
                                         image=_image())
                name, args, _ = rec.one()
                self.assertEqual(name, send)
                self.assertEqual(_norm(args[1]), EXPECTED_MESSAGES)
                self.assertEqual(args[2:], (120.0, "low"))
                self.assertEqual(out.args, ("A", "B", ""))

    def test_split_output(self):
        out, _ = self._execute()
        self.assertEqual(out.args, ("A", "B", ""))
        out, _ = self._execute(split_output=False)
        self.assertEqual(out.args[1], "")
        self.assertIn("A", out.args[0])

    def test_canvas_fallback_uses_image_dims(self):
        canvas = self.mod._build_canvas_profile(8, 8)
        self.assertTrue(canvas)
        _, rec = self._execute(image=_image(), width=0, height=0)
        text = rec.messages()[1]["content"][0]["text"]
        self.assertTrue(text.startswith(canvas + "\n\n"), text[:200])
        # wired dims win over the image's
        _, rec = self._execute(image=_image(), width=1536, height=640)
        text = rec.messages()[1]["content"][0]["text"]
        self.assertTrue(text.startswith(self.mod._build_canvas_profile(1536, 640)))
        # no image, no dims -> no canvas block
        _, rec = self._execute()
        self.assertEqual(rec.messages()[1]["content"], "USER REQUEST:\na red fox in snow")

    def test_text_only_with_style_context_and_dims(self):
        _, rec = self._execute(style="ink wash", context="fox, winter", width=1024, height=1024)
        text = rec.messages()[1]["content"]
        canvas = self.mod._build_canvas_profile(1024, 1024)
        self.assertEqual(text, f"{canvas}\n\nSTYLE:\nink wash\n\nREFERENCE CONTEXT:\nfox, winter"
                               f"\n\nUSER REQUEST:\na red fox in snow")

    def test_chat_completion_call_openai_custom_grok(self):
        cases = (("OpenAI", "", "https://api.openai.com/v1"),
                 ("Custom", "http://localhost:1234/v1/", "http://localhost:1234/v1"),
                 ("Grok (xAI)", "", "https://api.x.ai/v1"))
        for provider, url, base in cases:
            with self.subTest(provider=provider):
                _, rec = self._execute(provider=provider, server_url=url, model_name="m-1",
                                       stop_sequences="END, ---", seed=7, auto_settings=False)
                name, args, kw = rec.one()
                self.assertEqual(name, "_send_chat_completion")
                self.assertEqual(args, ())
                kw = dict(kw)
                kw.pop("messages")
                self.assertEqual(kw, dict(
                    provider=provider, base_url=base, api_key="k", model="m-1",
                    sampling={"temperature": 0.7, "top_p": 0.9, "top_k": 0, "presence_penalty": 0.0,
                              "frequency_penalty": 0.0, "max_tokens": 4096, "seed": 7},
                    output_format="text", stop_sequences=["END", "---"], timeout=120.0,
                    extra_body=None, response_format_override=None, reasoning_effort="low"))

    def test_grok_pair_schema(self):
        reply = json.dumps({"positive": "P", "negative": "N"})
        out, rec = self._execute(reply=reply, provider="Grok (xAI)", model_name="grok-4.7",
                                 custom_system_prompt="Write positive | negative prompts.")
        kw = rec.one()[2]
        self.assertEqual(kw["response_format_override"]["json_schema"]["name"], "prompt_pair")
        self.assertIn('"positive" field', kw["messages"][0]["content"])
        self.assertEqual(out.args, ("P", "N", ""))

    def test_gemini_native_call(self):
        _, rec = self._execute(provider="Gemini", model_name="gemini-3.6-flash",
                               gemini_thinking_budget=5, gemini_thinking_level="high",
                               auto_settings=False)
        kw = dict(rec.one()[2])
        kw.pop("messages")
        self.assertEqual(kw, dict(
            base_url="https://generativelanguage.googleapis.com/v1beta", api_key="k",
            model="gemini-3.6-flash",
            sampling={"temperature": 0.7, "top_p": 0.9, "top_k": 0, "presence_penalty": 0.0,
                      "frequency_penalty": 0.0, "max_tokens": 4096, "seed": 0},
            output_format="text", stop_sequences=[], thinking_budget=5, thinking_level="high",
            cached_content_name=None, timeout=120.0, video=None, video_fps=2.0))

    def test_output_formats_json_and_list(self):
        for fmt, tail in (("json", "valid JSON object"), ("list", "numbered list")):
            with self.subTest(fmt=fmt):
                out, rec = self._execute(output_format=fmt)
                self.assertIn(tail, rec.messages()[0]["content"])
                self.assertEqual(out.args[0], "A")  # split still parses the raw reply

    def test_native_video_gemini(self):
        video = object()
        out, rec = self._execute(provider="Gemini", model_name="gemini-3.6-flash", video=video,
                                 gemini_video_fps=1.5)
        kw = rec.one()[2]
        self.assertIs(kw["video"], video)
        self.assertEqual(kw["video_fps"], 1.5)
        self.assertEqual(out.args[2], "Video: native Gemini clip with embedded audio if present; "
                                      "sampling 1.5 fps.")

    def test_sampled_video_frames(self):
        import torch
        clip = torch.rand(10, 8, 8, 3)
        out, rec = self._execute(provider="OpenAI", video=clip, video_sample_frames=4,
                                 image=_image())
        content = rec.messages()[1]["content"]
        self.assertEqual(len(content), 1 + 1 + 4)  # text, image, 4 frames
        self.assertEqual(out.args[2], "Video: 4 sampled still frames; audio is not sent.")
        # the frames line stays ahead of the validation report
        out, _ = self._execute(video=clip, video_sample_frames=4, validate="h3")
        positive, findings = self.mod.validate_h3("A", None)
        self.assertEqual(out.args[2], "Video: 4 sampled still frames; audio is not sent.\n"
                         + self.mod.format_log(positive, None, findings))

    def test_write_prompt_api_refuses_non_native_video(self):
        with self._patched():
            with self.assertRaisesRegex(ValueError, "only for native Gemini"):
                self.mod.write_prompt_api(provider="OpenAI", model_name="gpt-5.6-luna",
                                          video=object())

    def test_video_route_errors(self):
        with self.assertRaisesRegex(ValueError, "only by the Gemini provider"):
            self._execute(video=object(), video_input_mode="native_video")
        with self.assertRaisesRegex(ValueError, "Gemini video-capable model"):
            self._execute(provider="Gemini", model_name="gemma-4", video=object())
        with self.assertRaisesRegex(ValueError, "Unknown video input mode"):
            self._execute(video_input_mode="bogus")
        with self.assertRaisesRegex(RuntimeError, "Unknown provider"):
            self._execute(provider="Nope")

    def test_missing_key_message(self):
        with self.assertRaisesRegex(RuntimeError, "OpenAI requires an API key"):
            self._execute(key=None)

    def test_validate_h3_log(self):
        out, _ = self._execute(validate="h3")
        positive, findings = self.mod.validate_h3("A", None)
        self.assertEqual(out.args, (positive, "B", self.mod.format_log(positive, None, findings)))

    # ---- Step 3: the extracted core ----------------------------------------------------

    def test_write_prompt_api_matches_execute(self):
        img = _image()
        b64 = self.mod._tensor_to_base64_png(img[0])
        for provider, model in (("OpenAI", "gpt-5.6-luna"), ("Gemini", "gemini-3.6-flash"),
                                ("Claude (Max)", "claude-sonnet-5-5")):
            with self.subTest(provider=provider):
                out, rec_node = self._execute(provider=provider, model_name=model, image=img)
                with self._patched() as rec_core:
                    res = self.mod.write_prompt_api(
                        provider=provider, model_name=model, custom_system_prompt=SYS,
                        user_prompt="a red fox in snow", images_b64=[b64], width=8, height=8)
                self.assertEqual(_norm(rec_core.calls), _norm(rec_node.calls))
                self.assertEqual(rec_core.calls[0][2].get("messages", None),
                                 rec_node.calls[0][2].get("messages", None))
                self.assertEqual(res, ("A", "B", ""))
                self.assertEqual(res, out.args)

    def test_write_prompt_api_text_only_dims_drive_canvas(self):
        with self._patched() as rec:
            self.mod.write_prompt_api(provider="OpenAI", model_name="gpt-5.6-luna",
                                      user_prompt="x", width=1536, height=640)
        self.assertTrue(rec.messages()[-1]["content"].startswith(
            self.mod._build_canvas_profile(1536, 640)))

    def test_defaults_match_schema(self):
        params = inspect.signature(self.mod.write_prompt_api).parameters
        self.assertEqual(list(params), [
            "provider", "model_name", "system_prompt", "custom_system_prompt", "user_prompt",
            "context", "style", "width", "height", "images_b64", "output_format", "split_output",
            "max_tokens", "temperature", "top_p", "top_k", "presence_penalty",
            "frequency_penalty", "seed", "stop_sequences", "gemini_thinking_budget",
            "gemini_thinking_level", "enable_caching", "disable_thinking", "timeout_seconds",
            "auto_settings", "reasoning_effort", "server_url", "validate", "frames", "video",
            "video_input_mode", "video_sample_frames", "gemini_video_fps"])
        self.assertTrue(all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values()))
        schema = self._schema()
        widgets = {i.id: getattr(i, "default", None) for i in schema.inputs}
        compared = []
        for name, p in params.items():
            if p.default is inspect.Parameter.empty or widgets.get(name) is None:
                continue
            with self.subTest(name=name):
                self.assertEqual(p.default, widgets[name])
                self.assertIs(type(p.default), type(widgets[name]))
            compared.append(name)
        self.assertGreaterEqual(len(compared), 25, compared)

    def test_schema_inputs_unchanged(self):
        schema = self._schema()
        self.assertEqual([i.id for i in schema.inputs], [
            "provider", "model_name", "server_url", "model_filter", "system_prompt",
            "custom_system_prompt", "user_prompt", "output_format", "split_output", "max_tokens",
            "seed", "temperature", "top_p", "top_k", "presence_penalty", "frequency_penalty",
            "stop_sequences", "gemini_thinking_budget", "gemini_thinking_level", "enable_caching",
            "disable_thinking", "timeout_seconds", "vision_mp", "validate", "auto_settings",
            "reasoning_effort", "style", "context", "width", "height", "image", "video", "frames",
            "video_input_mode", "video_sample_frames", "gemini_video_fps"])


if __name__ == "__main__":
    unittest.main()
