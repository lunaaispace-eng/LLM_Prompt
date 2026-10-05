"""Luna house style: director.css tokens match the copied luna_theme, and the kit files are present."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")

TOKENS = ("accent", "bg", "panel", "border", "text", "muted", "danger", "radius",
          "input", "processing", "output")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _luna_values(text):
    block = re.search(r"export const LUNA = \{([^}]+)\}", text)
    if not block:
        return {}
    out = {}
    for key, raw in re.findall(r"(\w+)\s*:\s*([^,\n]+)", block.group(1)):
        out[key] = raw.strip().strip('"').strip("'")
    return out


def _css_values(text):
    out = {}
    for key, raw in re.findall(r"--([A-Za-z0-9_-]+)\s*:\s*([^;]+)", text):
        out[key] = raw.strip().strip('"').strip("'")
    return out


def _same(js_value, css_value):
    js, css = js_value.lower(), css_value.lower()
    if css == js:
        return True
    if css.endswith("px") and css[:-2] == js:
        return True
    return False


class DirectorStyle(unittest.TestCase):
    def test_director_css_tokens_match_luna_theme(self):
        theme_path = os.path.join(WEB, "luna_theme.mjs")
        css_path = os.path.join(WEB, "director", "director.css")
        self.assertTrue(os.path.isfile(theme_path), theme_path)
        self.assertTrue(os.path.isfile(css_path), css_path)
        luna = _luna_values(_read(theme_path))
        css = _css_values(_read(css_path))
        for key in TOKENS:
            self.assertIn(key, luna, key)
            self.assertIn(key, css, key)
            self.assertTrue(_same(luna[key], css[key]), f"{key}: theme {luna[key]!r} css {css[key]!r}")

    def test_luna_kit_files_present(self):
        for name in ("luna_theme.mjs", "luna_help.mjs", "luna_collapse.mjs"):
            path = os.path.join(WEB, name)
            self.assertTrue(os.path.isfile(path), path)
            self.assertGreater(os.path.getsize(path), 0, path)


if __name__ == "__main__":
    unittest.main()
