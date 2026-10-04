import ast
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from luna_imaging.sizes import openai_size  # noqa: E402
from luna_imaging.cost import (  # noqa: E402
    GEMINI_IMAGE_PRICES, OPENAI_RATES, gemini_cost, openai_cost,
    openai_cost_line, xai_cost)

FLARE = "gpt-image-2.5-flare"


class SizeTests(unittest.TestCase):
    def test_openai_sizes_regression(self):
        cases = [
            ((FLARE, "16:9", "1K"), "1360x768"),
            ((FLARE, "16:9", "4K"), "3840x2160"),
            ((FLARE, "21:9", "2K"), "3136x1344"),
            ((FLARE, "1:3", "1K"), "592x1776"),
            (("gpt-image-1.5", "16:9", "2K"), "1536x1024"),
            (("gpt-image-2", "auto", "auto"), "auto"),
        ]
        for (m, ar, res), want in cases:
            with self.subTest(m=m, ar=ar, res=res):
                self.assertEqual(openai_size(m, ar, res, 0, 0, []), want)

    def test_wired_size_and_note(self):
        notes = []
        self.assertEqual(openai_size(FLARE, "auto", "auto", 1000, 600, notes), "1040x640")
        self.assertEqual(len(notes), 1)

    def test_fixed_size_note(self):
        notes = []
        openai_size("gpt-image-1.5", "16:9", "2K", 0, 0, notes)
        self.assertEqual(notes, ["gpt-image-1.5 has fixed sizes only - 2K ignored"])


class CostTests(unittest.TestCase):
    USAGE = {"input_tokens_details": {"text_tokens": 15, "image_tokens": 0},
             "output_tokens_details": {"image_tokens": 196}}

    def test_openai_cost(self):
        self.assertEqual(round(openai_cost(FLARE, self.USAGE), 4), 0.0060)
        self.assertIsNone(openai_cost("other-model", self.USAGE))
        self.assertIsNone(openai_cost(FLARE, {}))
        self.assertIn("gpt-image-2.5", OPENAI_RATES)

    def test_openai_cost_line(self):
        self.assertEqual(openai_cost_line(FLARE, self.USAGE),
                         "$0.0060  [in 15 text + 0 image, out 196 image tokens]")
        self.assertEqual(openai_cost_line("other", self.USAGE), "n/a")
        self.assertEqual(openai_cost_line(FLARE, None), "n/a")

    def test_xai_cost(self):
        self.assertAlmostEqual(xai_cost({"usage": {"cost_in_usd_ticks": 500000000}}), 0.05)
        self.assertIsNone(xai_cost({"usage": {}}))
        self.assertIsNone(xai_cost({}))

    def test_gemini_cost(self):
        self.assertAlmostEqual(gemini_cost("gemini-3.1-flash-image-preview", "2K", 2), 0.202)
        self.assertIsNone(gemini_cost("unknown-model", "1K", 1))
        self.assertIsNone(gemini_cost("gemini-3.1-flash-image-preview", "8K", 1))
        # longest prefix: lite-image has its own key
        self.assertAlmostEqual(gemini_cost("gemini-3.1-flash-lite-image-preview", "1K", 1), 0.0336)
        self.assertIsNone(gemini_cost("gemini-3.1-flash-lite-image-preview", "2K", 1))
        self.assertAlmostEqual(gemini_cost("nano-banana-pro-preview", "4K", 1), 0.24)
        self.assertIn("gemini-3-pro-image", GEMINI_IMAGE_PRICES)


class NodeSourceTests(unittest.TestCase):
    def test_openai_node_uses_core(self):
        path = os.path.join(ROOT, "openai_image_node.py")
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)
        defs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        self.assertIn("from .luna_imaging", src)
        for gone in ("_fit_free", "_snap16", "_ar_float", "_resolve_size",
                     "_rates_for", "_cost_line", "_free_size"):
            self.assertNotIn(gone, defs)


if __name__ == "__main__":
    unittest.main()
