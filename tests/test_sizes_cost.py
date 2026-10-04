import ast
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from luna_imaging.sizes import RESOLUTIONS, nearest_aspect, openai_size  # noqa: E402
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

    # F1: the Studio offers 0.5K / 1.5K; on the 2.x free-size models they map to the next known
    # budget up (0.5K -> 1K, 1.5K -> 2K) with a note instead of a KeyError.
    def test_studio_only_resolutions_map_up(self):
        for model in (FLARE, "gpt-image-2"):
            for res, used in (("0.5K", "1K"), ("1.5K", "2K")):
                with self.subTest(model=model, res=res):
                    notes = []
                    got = openai_size(model, "16:9", res, 0, 0, notes)
                    self.assertEqual(got, openai_size(model, "16:9", used, 0, 0, []))
                    self.assertEqual(notes, [f"resolution {res} is not an OpenAI budget - used {used}"])
        notes = []
        self.assertEqual(openai_size("gpt-image-1.5", "1:1", "0.5K", 0, 0, notes), "1024x1024")
        self.assertEqual(notes, ["gpt-image-1.5 has fixed sizes only - 0.5K ignored"])

    def test_gpt_image_node_options_unchanged(self):
        # Sizes for the GPT Image node's own RESOLUTIONS options as produced at 635247c
        # (before F1). They must stay identical, with no new notes.
        self.assertEqual(list(RESOLUTIONS), ["auto", "1K", "2K", "4K"])
        table = [
            ((FLARE, "auto"), ("auto", "1024x1024", "2048x2048", "2880x2880")),
            ((FLARE, "1:1"), ("1024x1024", "1024x1024", "2048x2048", "2880x2880")),
            ((FLARE, "16:9"), ("1360x768", "1360x768", "2736x1536", "3840x2160")),
            ((FLARE, "9:16"), ("768x1360", "768x1360", "1536x2736", "2160x3840")),
            ((FLARE, "21:9"), ("1568x672", "1568x672", "3136x1344", "3840x1648")),
            (("gpt-image-2", "auto"), ("auto", "1024x1024", "2048x2048", "2880x2880")),
            (("gpt-image-2", "16:9"), ("1360x768", "1360x768", "2736x1536", "3840x2160")),
            (("gpt-image-1.5", "auto"), ("auto", "auto", "auto", "auto")),
            (("gpt-image-1.5", "16:9"), ("1536x1024", "1536x1024", "1536x1024", "1536x1024")),
            (("gpt-image-1.5", "9:16"), ("1024x1536", "1024x1536", "1024x1536", "1024x1536")),
        ]
        for (model, ar), sizes in table:
            for res, want in zip(RESOLUTIONS, sizes):
                with self.subTest(model=model, ar=ar, res=res):
                    notes = []
                    self.assertEqual(openai_size(model, ar, res, 0, 0, notes), want)
                    if model != "gpt-image-1.5":
                        self.assertEqual(notes, [])

    def test_nearest_aspect(self):
        gem = ["auto", "1:1", "3:2", "16:9", "21:9", "9:16"]
        self.assertEqual(nearest_aspect(1000, 600, gem), "16:9")
        self.assertEqual(nearest_aspect(600, 1000, gem), "9:16")
        self.assertEqual(nearest_aspect(512, 512, gem), "1:1")
        self.assertEqual(nearest_aspect(2520, 1080, gem), "21:9")
        self.assertEqual(nearest_aspect(1500, 1000, gem), "3:2")


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
    def test_core_docstring_states_pillow_dependency(self):
        import luna_imaging  # F10
        self.assertIn("Pillow >= 10.3", luna_imaging.__doc__)
        self.assertIn("ImageMath.lambda_eval", luna_imaging.__doc__)

    def test_openai_node_uses_core(self):
        path = os.path.join(ROOT, "openai_image_node.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
        defs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        self.assertIn("from .luna_imaging", src)
        for gone in ("_fit_free", "_snap16", "_ar_float", "_resolve_size",
                     "_rates_for", "_cost_line", "_free_size"):
            self.assertNotIn(gone, defs)


if __name__ == "__main__":
    unittest.main()
