"""LLM Prompt (API): the LM Studio route (unload rule, context, reasoning) and audio routing.

_lmstudio_request is faked; no server, CLI or key is touched.
"""
import contextlib
import io
import unittest
from unittest import mock

import _comfy

MODELS = [
    {"type": "llm", "key": "melody", "loaded_instances": [],
     "capabilities": {"vision": False, "reasoning": {"allowed_options": ["off", "on"], "default": "on"}}},
    {"type": "llm", "key": "busy", "loaded_instances": [{"id": "busy"}],
     "capabilities": {"vision": True, "reasoning": {"allowed_options": ["off", "on"]}}},
    {"type": "llm", "key": "plain", "loaded_instances": [], "capabilities": {"vision": False}},
    {"type": "embedding", "key": "nomic-embed", "loaded_instances": []},
]


class FakeLMStudio:
    def __init__(self, load_s=10.9):
        self.calls = []
        self.load_s = load_s

    def __call__(self, base_url, path, api_key, body=None, timeout=5.0):
        self.calls.append((path, body))
        if path == "/api/v1/models":
            return {"models": MODELS}
        if path == "/api/v1/chat":
            stats = {"input_tokens": 10, "total_output_tokens": 5, "tokens_per_second": 80.0}
            if self.load_s is not None and body["model"] != "busy":
                stats["model_load_time_seconds"] = self.load_s
            return {"model_instance_id": body["model"], "stats": stats,
                    "output": [{"type": "reasoning", "content": "hmm"},
                               {"type": "message", "content": "[POSITIVE]\nA\n[NEGATIVE]\nB"}]}
        if path == "/api/v1/models/unload":
            return {"instance_id": body["instance_id"]}
        raise AssertionError(path)

    def chat(self):
        return next(b for p, b in self.calls if p == "/api/v1/chat")

    def unloaded(self):
        return [b["instance_id"] for p, b in self.calls if p == "/api/v1/models/unload"]


class LMStudioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _comfy.load("llm_prompt_api_node")

    def _write(self, model, fake=None, **kw):
        fake = fake or FakeLMStudio()
        with mock.patch.object(self.mod, "_lmstudio_request", fake), \
             mock.patch.object(self.mod, "_check_load", return_value={}), \
             contextlib.redirect_stdout(io.StringIO()):
            pos, neg, log = self.mod.write_prompt_api(provider="LM Studio", model_name=model,
                                                      user_prompt="x", custom_system_prompt="sys", **kw)
        return fake, pos, neg

    def test_loads_with_context_and_unloads_what_it_loaded(self):
        fake, pos, neg = self._write("melody", quality="normal")
        body = fake.chat()
        self.assertEqual(body["context_length"], 8192)
        self.assertEqual(body["reasoning"], "on")  # off / on model: a thinking level becomes "on"
        self.assertTrue(body["system_prompt"].startswith("sys"))
        self.assertFalse(body["store"])
        self.assertEqual(fake.unloaded(), ["melody"])
        self.assertEqual((pos, neg), ("A", "B"))
        stats = self.mod._run_stats()
        self.assertEqual(stats["model_state"], "unloaded")
        self.assertEqual(stats["reasoning"], "hmm")

    def test_already_loaded_model_is_left_alone(self):
        fake, _, _ = self._write("busy", quality="fast", context_length=16384)
        body = fake.chat()
        self.assertNotIn("context_length", body)  # would load a second copy
        self.assertEqual(body["reasoning"], "off")
        self.assertEqual(fake.unloaded(), [])
        self.assertEqual(self.mod._run_stats()["model_state"], "already loaded, kept")

    def test_unload_switch_off_keeps_model(self):
        fake, _, _ = self._write("melody", unload_after_run=False)
        self.assertEqual(fake.unloaded(), [])
        self.assertEqual(self.mod._run_stats()["model_state"], "loaded by this run, kept")

    def test_model_without_reasoning_gets_none(self):
        fake, _, _ = self._write("plain", quality="ultra")
        self.assertNotIn("reasoning", fake.chat())

    def test_auto_settings_sends_family_values(self):
        fake, _, _ = self._write("gemma-4-e4b", quality="fast")
        body = fake.chat()
        self.assertEqual((body["temperature"], body["top_p"], body["top_k"]), (1.0, 0.95, 64))
        self.assertEqual(body["repeat_penalty"], 1.0)

    def test_server_down_message(self):
        def down(base_url, path, api_key, body=None, timeout=5.0):
            raise RuntimeError("LM Studio's server does not answer at http://localhost:1234")
        with self.assertRaisesRegex(RuntimeError, "does not answer"):
            self._write("melody", fake=down)

    def test_list_has_only_llms(self):
        with mock.patch.object(self.mod, "_lmstudio_request", FakeLMStudio()), \
             mock.patch.object(self.mod, "_check_load", return_value={}), \
             mock.patch.object(self.mod, "_MODEL_CHECK_FILE"):
            self.mod._MODEL_LIST_CACHE_TS.clear()
            self.assertEqual(self.mod._get_models_for_provider("LM Studio"), ["melody", "busy", "plain"])


class AudioRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _comfy.load("llm_prompt_api_node")

    def _audio(self, seconds=0.5):
        import torch
        return {"waveform": torch.zeros(1, 1, int(16000 * seconds)), "sample_rate": 16000}

    def test_wav_encoding(self):
        clips = self.mod._audio_to_wav(self._audio())
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0][:4], b"RIFF")
        self.assertEqual(self.mod._audio_to_wav(None), [])

    def test_audio_refused_where_untested(self):
        for provider, model in (("Grok (xAI)", "grok-4.7"), ("OpenAI", "gpt-6-luna"),
                                ("Codex (ChatGPT)", "gpt-6-luna"), ("LM Studio", "melody"),
                                ("Claude (Max)", "claude-sonnet-5-5")):
            with self.subTest(provider=provider), self.assertRaisesRegex(ValueError, "cannot take audio"):
                self.mod.write_prompt_api(provider=provider, model_name=model, user_prompt="x",
                                          audio=self._audio())

    def test_audio_reaches_gemini_and_agy(self):
        calls = {}

        def rec(name):
            def fn(*a, **k):
                calls[name] = (a, k)
                return "A"
            return fn
        with mock.patch.multiple(self.mod, _send_gemini_native=rec("gemini"), _send_agy_cli=rec("agy"),
                                 _resolve_api_key=lambda *a, **k: "k"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.mod.write_prompt_api(provider="Gemini", model_name="gemini-3.8-flash", user_prompt="x",
                                      audio=self._audio())
            self.mod.write_prompt_api(provider="Gemini (Antigravity)", model_name="gemini-3.8-flash",
                                      user_prompt="x", audio=self._audio())
        self.assertEqual(len(calls["gemini"][1]["audio"]), 1)
        self.assertEqual(calls["agy"][1]["audio"][0][1], "wav")

    def test_gemma_refuses_audio(self):
        with self.assertRaisesRegex(ValueError, "Gemma"):
            self.mod.write_prompt_api(provider="Gemini", model_name="gemma-4-31b-it", user_prompt="x",
                                      audio=self._audio())


if __name__ == "__main__":
    unittest.main()
