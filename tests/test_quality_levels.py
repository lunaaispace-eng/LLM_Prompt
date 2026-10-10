"""LLM Prompt (GGUF): the `quality` levels, thinking-aware auto sampling, run stats.

`_load_model` and `_invoke` are replaced by recorders, so no test loads a model.
"""
import unittest
from unittest import mock

import _comfy

QWEN = "Qwen3.6/Qwen3.6-27B-heretic-v2.Q4_K_M.gguf"


class QualityLevelTests(unittest.TestCase):
    def setUp(self):
        self.node = _comfy.load("llm_prompt_node")
        self.runner = self.node._LLMRunner()
        self.load = mock.patch.object(self.runner, "_load_model").start()
        self.invoke = mock.patch.object(self.runner, "_invoke", return_value="a prompt").start()
        self.addCleanup(mock.patch.stopall)

    def run_node(self, **over):
        kw = dict(model_name=QWEN, system_prompt="", custom_system_prompt="x", user_prompt="a cat",
                  output_format="text", split_output=True, max_tokens=2048, temperature=0.5, top_p=0.5,
                  top_k=5, min_p=0.1, repetition_penalty=1.2, device="auto", auto_settings=True,
                  disable_thinking=True, keep_model_loaded=True, seed=1, mtp_draft_tokens=0,
                  reasoning_budget=-1)
        kw.update(over)
        self.runner.generate(**kw)
        return self.load.call_args.kwargs, self.invoke.call_args.kwargs

    def test_fast_is_no_thinking_with_mtp(self):
        load, inv = self.run_node(quality="fast")
        self.assertTrue(inv["disable_thinking"])
        self.assertEqual(load["mtp_draft_tokens"], 3)
        self.assertEqual(inv["temperature"], 0.7)  # Qwen no-think

    def test_normal_thinks_with_budget_and_thinking_sampling(self):
        load, inv = self.run_node(quality="normal")
        self.assertFalse(inv["disable_thinking"])
        self.assertFalse(load["disable_thinking"])
        self.assertEqual(inv["reasoning_budget"], 1024)
        self.assertEqual((inv["temperature"], inv["top_p"]), (1.0, 0.95))

    def test_quality_and_ultra_budgets(self):
        self.assertEqual(self.run_node(quality="quality")[1]["reasoning_budget"], 2048)
        self.assertEqual(self.run_node(quality="ultra")[1]["reasoning_budget"], -1)

    def test_level_overrides_widgets_even_with_auto_off(self):
        load, inv = self.run_node(quality="normal", auto_settings=False)
        self.assertFalse(inv["disable_thinking"])
        self.assertEqual(load["mtp_draft_tokens"], 3)
        self.assertEqual(inv["temperature"], 0.5)  # sampling stays the user's

    def test_custom_with_auto_keeps_old_behaviour(self):
        load, inv = self.run_node(quality="custom", disable_thinking=False, mtp_draft_tokens=2)
        self.assertTrue(inv["disable_thinking"])  # auto forces thinking off on custom
        self.assertEqual(load["mtp_draft_tokens"], 2)

    def test_custom_without_auto_keeps_thinking_widget(self):
        _, inv = self.run_node(quality="custom", auto_settings=False, disable_thinking=False,
                               reasoning_budget=512)
        self.assertFalse(inv["disable_thinking"])
        self.assertEqual(inv["reasoning_budget"], 512)

    def test_generate_default_is_custom(self):
        # luna_writer_api calls generate() without `quality`: nothing may change for it.
        load, inv = self.run_node(auto_settings=False, disable_thinking=False, mtp_draft_tokens=1)
        self.assertFalse(inv["disable_thinking"])
        self.assertEqual(load["mtp_draft_tokens"], 1)


class SchemaAndStatsTests(unittest.TestCase):
    def setUp(self):
        self.node = _comfy.load("llm_prompt_node")

    def test_quality_is_last_widget_default_fast(self):
        inputs = self.node.LLMPromptNode.define_schema().inputs
        widgets = [i for i in inputs if not getattr(i, "optional", False)]
        self.assertEqual(widgets[-1].id, "quality")
        self.assertEqual(widgets[-1].default, "fast")
        self.assertEqual(widgets[-1].options, ["fast", "normal", "quality", "ultra", "custom"])

    def test_quant_from_filename(self):
        f = lambda s: self.node._QUANT_RE.findall(s)[-1].upper()  # noqa: E731
        self.assertEqual(f("Qwen3.6-27B-heretic-v2-Native-MTP-Preserved.Q4_K_M"), "Q4_K_M")
        self.assertEqual(f("Gemma4-26B-A4B-Uncensored-HauhauCS-Balanced-Q4_K_P"), "Q4_K_P")
        self.assertEqual(f("gemma-4-E4B-it-uncensored-Q8_0"), "Q8_0")
        self.assertEqual(self.node._QUANT_RE.findall("model-without-quant"), [])

    def test_execute_returns_stats_as_ui(self):
        stats = {"seconds": 1.5, "tok_s": 40.0}
        with mock.patch.object(self.node._RUNNER, "generate", return_value=("p", "n", "l")), \
                mock.patch.object(self.node._RUNNER, "last_stats", stats, create=True):
            out = self.node.LLMPromptNode.execute(model_name=QWEN)
        self.assertEqual(out.args, ("p", "n", "l"))
        self.assertEqual(out.ui, {"llm_stats": [stats]})


if __name__ == "__main__":
    unittest.main()
