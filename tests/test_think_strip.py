"""LLM Prompt (GGUF): `_strip_think_blocks` with thinking ON.

The chat template pre-fills the opening tag into the prompt, so a thinking reply has only a
closing tag. Reasoning written as numbered bold steps ("1. **Analyze ...**") used to be
caught by the meta-commentary filter, which ran to end-of-text and took the answer with it.
"""
import unittest

import _comfy

REASONING = (
    "Here's a thinking process:\n\n"
    "1.  **Analyze User Input:**\n   - Subject: lighthouse keeper\n\n"
    "2.  **Draft:**\n   A keeper on a cliff.\n\n"
    "3.  **Final Check against Constraints:**\n   - Ready.\n"
)
ANSWER = "[POSITIVE]\nA lone lighthouse keeper on a storm-battered cliff at night, brass lantern glowing."


class ThinkStripTests(unittest.TestCase):
    def setUp(self):
        self.node = _comfy.load("llm_prompt_node")

    def test_closing_tag_only_keeps_answer(self):
        out = self.node._strip_think_blocks(REASONING + "</think>\n\n" + ANSWER)
        self.assertEqual(out, ANSWER)

    def test_both_tags_keep_answer(self):
        out = self.node._strip_think_blocks("<think>\n" + REASONING + "</think>\n" + ANSWER)
        self.assertEqual(out, ANSWER)

    def test_gemma_channel_keeps_answer(self):
        out = self.node._strip_think_blocks("<|channel>thought\n" + REASONING + "<channel|>" + ANSWER)
        self.assertEqual(out, ANSWER)

    def test_reasoning_still_extracted(self):
        raw = REASONING + "</think>\n\n" + ANSWER
        self.assertIn("Analyze User Input", self.node._extract_reasoning(raw))

    def test_trailing_meta_after_answer_still_stripped(self):
        out = self.node._strip_think_blocks(
            ANSWER + "\n\n4. **Final Review against Constraints:**\n  * Format: ok")
        self.assertEqual(out, ANSWER)


class ReasoningBudgetTests(unittest.TestCase):
    """`_invoke` passes the budget only with thinking on and known tags; bumps min_p 0."""

    def setUp(self):
        from unittest import mock
        self.node = _comfy.load("llm_prompt_node")
        self.runner = self.node._LLMRunner()
        self.runner.llm = mock.MagicMock()
        self.runner.llm.create_chat_completion.return_value = {
            "choices": [{"message": {"content": "r</think>\n" + ANSWER}, "finish_reason": "stop"}],
            "usage": {}}
        self.runner.loaded_model_name_lower = "model.gguf"

    def _kw(self, arch, budget, thinking_off=False, min_p=0.0):
        self.runner.loaded_model_arch = arch
        self.runner._invoke(system_prompt="s", user_prompt="u", media_content=[], max_tokens=100,
                            temperature=1.0, top_p=0.95, repetition_penalty=1.0, seed=1, top_k=20,
                            min_p=min_p, disable_thinking=thinking_off, reasoning_budget=budget)
        return self.runner.llm.create_chat_completion.call_args.kwargs

    def test_qwen_budget(self):
        kw = self._kw("qwen35", 512)
        self.assertEqual((kw["reasoning_budget"], kw["reasoning_start"], kw["reasoning_end"]),
                         (512, "<think>", "</think>"))
        self.assertTrue(kw["reasoning_start_in_prompt"])
        self.assertGreater(kw["min_p"], 0.0)

    def test_gemma4_tags(self):
        kw = self._kw("gemma4", 256, min_p=0.05)
        self.assertEqual((kw["reasoning_start"], kw["reasoning_end"]), ("<|channel>thought", "<channel|>"))
        self.assertEqual(kw["min_p"], 0.05)

    def test_unlimited_thinking_off_unknown_arch(self):
        for kw in (self._kw("qwen35", -1), self._kw("qwen35", 512, thinking_off=True),
                   self._kw("llama", 512)):
            self.assertNotIn("reasoning_budget", kw)
            self.assertEqual(kw["min_p"], 0.0)


class JsonPromptSplitTests(unittest.TestCase):
    """A Krea-style {"prompt": ...} answer reaches `positive` without its JSON wrapper."""

    def setUp(self):
        self.oc = _comfy.load("output_cleaner")

    def test_prompt_key_unwrapped(self):
        pos, neg = self.oc.split_positive_negative('{\n  "prompt": "A raw photo, a keeper."\n}', True)
        self.assertEqual((pos, neg), ("A raw photo, a keeper.", ""))

    def test_positive_negative_keys_still_win(self):
        pos, neg = self.oc.split_positive_negative('{"positive": "a", "negative": "b", "prompt": "c"}', True)
        self.assertEqual((pos, neg), ("a", "b"))

    def test_split_off_keeps_raw_json(self):
        raw = '{"prompt": "x"}'
        self.assertEqual(self.oc.split_positive_negative(raw, False)[0], raw)


class Gemma4ThinkingHandlerTests(unittest.TestCase):
    """The text-only Gemma 4 handler renders the GGUF template with enable_thinking=true."""

    TEMPLATE = ("{{ bos_token }}{% if enable_thinking %}<|think|>{% endif %}"
                "{% for m in messages %}{{ m['content'] }}{% endfor %}"
                "{% if add_generation_prompt %}<|turn>model\n"
                "{% if not enable_thinking | default(false) %}<|channel>thought\n<channel|>{% endif %}{% endif %}")

    def test_template_rendered_with_thinking(self):
        from unittest import mock
        node = _comfy.load("llm_prompt_node")
        llm = mock.MagicMock()
        llm.metadata = {"tokenizer.chat_template": self.TEMPLATE}
        llm.token_eos.return_value, llm.token_bos.return_value, llm.token_eot.return_value = 1, 2, -1
        llm._model.token_get_text.side_effect = lambda t: {1: "<eos>", 2: "<bos>"}[t]
        captured = {}
        with mock.patch("llama_cpp.llama_chat_format.chat_formatter_to_chat_completion_handler",
                        side_effect=lambda f: captured.setdefault("f", f)):
            node._gemma4_thinking_handler(llm)
        prompt = captured["f"](messages=[{"role": "user", "content": "hi"}]).prompt
        self.assertIn("<|think|>", prompt)
        self.assertNotIn("<|channel>thought\n<channel|>", prompt)

    def test_no_template_returns_none(self):
        from unittest import mock
        node = _comfy.load("llm_prompt_node")
        llm = mock.MagicMock()
        llm.metadata = {}
        self.assertIsNone(node._gemma4_thinking_handler(llm))


if __name__ == "__main__":
    unittest.main()
