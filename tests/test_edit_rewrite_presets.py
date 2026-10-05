"""A7: the three cloud edit-rewrite presets (prompts/edit_rewrite_*.md).

Titles must match `presets_map.EDIT_REWRITE` (the Director picks the preset by target model), each file
teaches through at least three worked examples (preset canon: few-shot over rules), and none shares a
12-word run with the Madame AI library (licence guard; `.docx` read through `word/document.xml`, `.zip`
and images skipped). `madame_ngrams` / `shared_runs` are reused by test_generate_presets.py.
"""
import functools
import os
import re
import unittest
import zipfile

import _comfy
from luna_director.presets_map import EDIT_REWRITE

PROMPTS = os.path.join(_comfy.ROOT, "prompts")
FILES = ("edit_rewrite_gpt_image.md", "edit_rewrite_gemini_image.md", "edit_rewrite_grok_imagine.md")
MADAME = r"D:\Library\madame-ai"
RUN = 12
TEXT_EXT = (".md", ".txt", ".yaml", ".csv")


def read(name: str) -> str:
    with open(os.path.join(PROMPTS, name), encoding="utf-8") as f:
        return f.read()


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _ngrams(text: str) -> set[tuple[str, ...]]:
    w = _words(text)
    return {tuple(w[i:i + RUN]) for i in range(len(w) - RUN + 1)}


def _library_text(path: str) -> str:
    if path.lower().endswith(".docx"):
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        return re.sub(r"<[^>]+>", " ", xml.replace("</w:p>", "\n"))
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


@functools.lru_cache(maxsize=1)
def madame_ngrams() -> frozenset:
    grams: set = set()
    for root, _, files in os.walk(MADAME):
        for name in files:
            if name.lower().endswith(TEXT_EXT + (".docx",)):
                try:
                    grams |= _ngrams(_library_text(os.path.join(root, name)))
                except (OSError, KeyError, zipfile.BadZipFile):
                    continue
    return frozenset(grams)


def shared_runs(text: str) -> list[str]:
    return [" ".join(g) for g in _ngrams(text) & madame_ngrams()]


class EditRewritePresets(unittest.TestCase):
    def test_every_mapped_title_exists(self):
        node = _comfy.load("llm_prompt_node")
        titles = node.load_system_prompts()
        for title in set(EDIT_REWRITE.values()):
            self.assertTrue(title in titles, f"preset missing: {title}")

    def test_each_has_three_or_more_examples(self):
        for name in FILES:
            n = len(re.findall(r"^### Example", read(name), re.MULTILINE))
            self.assertGreaterEqual(n, 3, name)

    def test_no_madame_text(self):
        if not os.path.isdir(MADAME):
            self.skipTest("Madame AI library not present")
        for name in FILES:
            self.assertEqual(shared_runs(read(name)), [], name)


if __name__ == "__main__":
    unittest.main()
