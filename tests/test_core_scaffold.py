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
        code = (f"import sys, pkgutil, importlib; sys.path.insert(0, {ROOT!r}); import luna_imaging; "
                "[importlib.import_module(m.name) for m in "
                "pkgutil.walk_packages(luna_imaging.__path__, 'luna_imaging.')]; "
                "assert 'torch' not in sys.modules "
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

    def test_retries_5xx_then_success_and_429(self):
        from unittest import mock
        import luna_imaging.http as h
        for status in (503, 429):
            seq = [ProviderError("x", status=status), ProviderError("x", status=status), "ok"]
            calls = []

            def fn():
                calls.append(1)
                v = seq[len(calls) - 1]
                if isinstance(v, Exception):
                    raise v
                return v

            with mock.patch.object(h, "_sleep") as sl:
                self.assertEqual(with_retries(fn, 2), "ok")
            self.assertEqual(len(calls), 3)
            self.assertEqual([c.args[0] for c in sl.call_args_list], [1, 2])

    def test_http_exception_becomes_provider_error(self):
        import http.client
        from unittest import mock
        import luna_imaging.http as h
        with mock.patch.object(h.urllib.request, "urlopen", side_effect=http.client.IncompleteRead(b"")):
            with self.assertRaises(ProviderError) as cm:
                h.post_json("http://x", {}, {}, 1)
        self.assertIsNone(cm.exception.status)


if __name__ == "__main__":
    unittest.main()
