"""A7 (S10): the three cloud generation presets (prompts/generate_*.md).

The examples are the contract: every example output, run through `writer.parse_generate_output`, gives the
number of variants its input asks for (VARIANTS), each with all five sections filled, so the presets teach
exactly what the parser reads. Together the examples cover varied N >= 3, role-labelled references, an exact
text kept byte-identical in quotes, and a Refine. Same title and licence checks as the edit-rewrite presets.
"""
import os
import re
import unittest

import _comfy
from luna_director.presets_map import GENERATE
from luna_director.writer import SECTIONS, distinct_variants, parse_generate_output
from test_edit_rewrite_presets import MADAME, read, shared_runs

FILES = ("generate_gpt_image.md", "generate_gemini_image.md", "generate_grok_imagine.md")


def examples(text: str) -> list[tuple[str, str]]:
    """(input, output) per `### Example` block."""
    out = []
    for block in re.split(r"^### Example[^\n]*\n", text, flags=re.MULTILINE)[1:]:
        inp, _, outp = block.partition("\nOutput:\n")
        out.append((inp, outp.strip()))
    return out


def stated_variants(inp: str) -> int:
    m = re.search(r"^VARIANTS:\s*(\d+)", inp, re.MULTILINE)
    return int(m.group(1)) if m else 1


class GeneratePresets(unittest.TestCase):
    def test_every_generate_title_exists(self):
        node = _comfy.load("llm_prompt_node")
        titles = node.load_system_prompts()
        for title in set(GENERATE.values()):
            self.assertTrue(title in titles, f"preset missing: {title}")

    def test_each_has_four_or_more_examples(self):
        for name in FILES:
            self.assertGreaterEqual(len(examples(read(name))), 4, name)

    def test_examples_cover_the_features(self):
        for name in FILES:
            ex = examples(read(name))
            with self.subTest(name):
                self.assertTrue(any(len(re.findall(r"\[VARIANT \d+\]", o)) >= 3 for _, o in ex), "varied N>=3")
                self.assertTrue(any(re.search(r"reference '[^']+'", i) for i, _ in ex), "role-labelled refs")
                self.assertTrue(any("PREVIOUS PROMPT:" in i and "FEEDBACK:" in i for i, _ in ex), "refine")
                exact = [(m.group(1), o) for i, o in ex for m in [re.search(r'^EXACT TEXT: "(.+)"$', i, re.M)] if m]
                self.assertTrue(exact, "exact text example")
                for text, o in exact:
                    self.assertIn(f'"{text}"', o)

    def test_example_outputs_parse(self):
        for name in FILES:
            for n, (inp, outp) in enumerate(examples(read(name)), 1):
                with self.subTest(f"{name} example {n}"):
                    want = stated_variants(inp)
                    got = parse_generate_output(outp, want)
                    self.assertEqual(len(got), want)
                    self.assertEqual(len(re.findall(r"\[VARIANT \d+\]", outp)), want)
                    for v in got:
                        self.assertIsNotNone(v["sections"])
                        self.assertTrue(all(v["sections"][s] for s in SECTIONS), v["sections"])

    def test_example_variants_distinct(self):
        for name in FILES:
            for inp, outp in examples(read(name)):
                want = stated_variants(inp)
                if want > 1:
                    prompts = [v["positive"] for v in parse_generate_output(outp, want)]
                    self.assertTrue(distinct_variants(prompts), name)

    def test_no_madame_text(self):
        if not os.path.isdir(MADAME):
            self.skipTest("Madame AI library not present")
        for name in FILES:
            self.assertEqual(shared_runs(read(name)), [], name)


if __name__ == "__main__":
    unittest.main()
