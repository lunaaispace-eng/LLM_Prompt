import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from luna_imaging.capabilities import caps_for, provider_for  # noqa: E402
from luna_imaging.http import ProviderError, with_retries  # noqa: E402


class CoreScaffold(unittest.TestCase):
    def test_no_comfy_or_torch_import(self):
        code = (f"import sys; sys.path.insert(0, {ROOT!r}); import luna_imaging; assert 'torch' not in sys.modules "
                "and 'comfy_api' not in sys.modules and 'numpy' not in sys.modules")
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT)
        self.assertEqual(r.returncode, 0)

    def test_provider_for(self):
        self.assertEqual(provider_for("gpt-image-2.5-flare"), "openai")
        self.assertEqual(provider_for("nano-banana-pro-preview"), "gemini")
        self.assertEqual(provider_for("grok-imagine-image-2.0"), "xai")
        self.assertRaises(ValueError, provider_for, "dall-e-3")

    def test_caps_values(self):
        self.assertIs(caps_for("gpt-image-2").transparent, False)
        self.assertIs(caps_for("gpt-image-2.5-flare").native_mask, True)
        self.assertEqual(caps_for("gemini-2.5-flash-image").max_inputs, 3)
        self.assertEqual(caps_for("gemini-3.1-flash-image-preview").max_inputs, 14)
        self.assertEqual(caps_for("grok-imagine-image-pro").max_inputs, 3)
        self.assertEqual(caps_for("grok-imagine-image-2.0").max_inputs, 5)
        self.assertIs(caps_for("gpt-image-1.5").free_size, False)

    def test_with_retries_stops_on_400(self):
        calls = []

        def fn():
            calls.append(1)
            raise ProviderError("x", status=400)

        with self.assertRaises(ProviderError):
            with_retries(fn, 3)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
