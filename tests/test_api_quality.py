"""LLM Prompt (API): quality levels and the last-run figures for the node face.

Send functions are recorders; no network, CLI or key is touched.
"""
import contextlib
import io
import unittest
from unittest import mock

import _comfy

REPLY = "[POSITIVE]\nA\n[NEGATIVE]\nB"
SENDS = ("_send_chat_completion", "_send_gemini_native", "_send_claude_cli",
         "_send_codex_cli", "_send_grok_cli")


class ApiQualityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _comfy.load("llm_prompt_api_node")

    def _write(self, provider, model, quality, notes=None, **kw):
        calls = []

        def make(name):
            def fn(*args, **kwargs):
                calls.append((name, kwargs))
                if notes:
                    self.mod._note_run(**notes)
                return REPLY
            return fn
        patches = {n: make(n) for n in SENDS}
        patches["_resolve_api_key"] = lambda *a, **k: "k"
        with mock.patch.multiple(self.mod, **patches), contextlib.redirect_stdout(io.StringIO()):
            self.mod.write_prompt_api(provider=provider, model_name=model, user_prompt="x",
                                      quality=quality, **kw)
        self.assertEqual(len(calls), 1, calls)
        return calls[0]

    def test_mapping_per_provider(self):
        q = self.mod._quality_settings
        self.assertEqual(q("Gemini", "gemini-3.6-flash", "fast"), {"gemini_thinking_level": "minimal"})
        self.assertEqual(q("Gemini", "gemini-3.6-flash", "ultra"), {"gemini_thinking_level": "high"})
        # No "minimal" on 3.8-flash: levels shift up so Fast and Normal differ.
        self.assertEqual(q("Gemini", "gemini-3.8-flash", "fast"), {"gemini_thinking_level": "low"})
        self.assertEqual(q("Gemini", "gemini-3.8-flash", "normal"), {"gemini_thinking_level": "medium"})
        self.assertEqual(q("Gemini", "gemini-3.8-flash", "ultra"), {"gemini_thinking_level": "high"})
        self.assertEqual(q("Gemini", "gemma-4-31b-it", "normal"), {})
        self.assertEqual(q("Grok (xAI)", "grok-4.7", "fast"), {"reasoning_effort": "low"})
        self.assertEqual(q("Grok (xAI)", "grok-4.7", "ultra"), {"reasoning_effort": "xhigh"})
        self.assertEqual(q("OpenAI", "gpt-6-luna", "fast"), {"reasoning_effort": "none"})
        self.assertEqual(q("OpenAI", "gpt-6-luna", "quality"), {"reasoning_effort": "medium"})
        self.assertEqual(q("Claude (Max)", "claude-sonnet-5-5", "normal"), {"reasoning_effort": "medium"})
        self.assertEqual(q("Custom", "anything", "ultra"), {})
        self.assertIsNone(q("OpenAI", "gpt-6-luna", "custom"))
        self.assertIsNone(q("OpenAI", "gpt-6-luna", ""))

    def test_level_overrides_widgets(self):
        name, kw = self._write("OpenAI", "gpt-6-luna", "normal", reasoning_effort="high")
        self.assertEqual(kw["reasoning_effort"], "low")
        name, kw = self._write("Gemini", "gemini-3.6-flash", "quality", gemini_thinking_level="None")
        self.assertEqual(kw["thinking_level"], "medium")
        self.assertTrue(kw["include_thoughts"])

    def test_custom_keeps_widgets(self):
        name, kw = self._write("OpenAI", "gpt-6-luna", "custom", reasoning_effort="high")
        self.assertEqual(kw["reasoning_effort"], "high")
        name, kw = self._write("Gemini", "gemini-3.6-flash", "custom", gemini_thinking_level="high")
        self.assertEqual(kw["thinking_level"], "high")
        self.assertFalse(kw["include_thoughts"])

    def test_run_stats_reset_and_filled(self):
        self.mod._note_run(seconds=99, served="stale")
        self._write("OpenAI", "gpt-6-luna", "fast", notes={"seconds": 1.5, "tokens_out": 12})
        stats = dict(self.mod._run_stats())
        self.assertEqual(stats["seconds"], 1.5)
        self.assertEqual(stats["tokens_out"], 12)
        self.assertEqual(stats["quality"], "fast")
        self.assertEqual(stats["level"], "none")
        self.assertNotIn("served", stats)

    def test_node_returns_ui_stats(self):
        calls = []

        def send(*a, **k):
            calls.append(k)
            self.mod._note_run(seconds=2.0, reasoning="thought about it")
            return REPLY
        with mock.patch.multiple(self.mod, _send_chat_completion=send,
                                 _resolve_api_key=lambda *a, **k: "k"), \
                contextlib.redirect_stdout(io.StringIO()):
            out = self.mod.LLMPromptAPINode.execute(
                provider="OpenAI", model_name="gpt-6-luna", server_url="", model_filter="all",
                system_prompt="None", custom_system_prompt="", user_prompt="x", output_format="text",
                split_output=True, max_tokens=4096, temperature=0.7, top_p=0.9, top_k=0,
                presence_penalty=0.0, frequency_penalty=0.0, seed=0, stop_sequences="",
                gemini_thinking_budget=0, gemini_thinking_level="None", enable_caching=False,
                disable_thinking=True, timeout_seconds=120, quality="ultra")
        ui = out.ui
        self.assertEqual(ui["llm_stats"][0]["seconds"], 2.0)
        self.assertEqual(ui["llm_stats"][0]["quality"], "ultra")
        self.assertEqual(ui["llm_stats"][0]["provider"], "OpenAI")
        self.assertEqual(ui["llm_reasoning"], ["thought about it"])
        self.assertNotIn("reasoning", ui["llm_stats"][0])
        self.assertEqual(calls[0]["reasoning_effort"], "high")

    def test_missing_quality_runs_as_custom(self):
        import inspect
        sig = inspect.signature(self.mod.LLMPromptAPINode.execute)
        self.assertEqual(sig.parameters["quality"].default, "custom")


if __name__ == "__main__":
    unittest.main()
