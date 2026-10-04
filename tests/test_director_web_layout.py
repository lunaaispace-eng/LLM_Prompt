import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRECTOR = os.path.join(ROOT, "web", "director")


def _files(*exts):
    for base, _dirs, names in os.walk(DIRECTOR):
        for n in names:
            if not exts or n.endswith(exts):
                yield os.path.join(base, n)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class DirectorWebLayout(unittest.TestCase):
    def test_every_file_at_most_400_lines(self):
        for path in _files():
            self.assertLessEqual(len(_read(path).splitlines()), 400, path)

    def test_core_is_pure(self):
        core = os.path.join(DIRECTOR, "core")
        found = [os.path.join(core, n) for n in os.listdir(core) if n.endswith(".mjs")]
        self.assertTrue(found)
        for path in found:
            text = _read(path)
            self.assertNotIn("/scripts/", text, path)
            self.assertIsNone(re.search(r"\bdocument\.", text), path)
            self.assertIsNone(re.search(r"\bwindow\.", text), path)

    def test_no_js_under_director(self):
        self.assertEqual([p for p in _files(".js")], [])


if __name__ == "__main__":
    unittest.main()
