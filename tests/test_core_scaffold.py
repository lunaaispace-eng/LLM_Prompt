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
        self.assertFalse(cm.exception.retryable)
        self.assertTrue(cm.exception.billed)
        self.assertIn("may still have been billed", str(cm.exception))

    # F5: only failures before the request reached the server are retried (a paid POST must
    # not bill twice); 429 / 5xx keep retrying.
    def _post_calls(self, side_effect, max_retries=2):
        from unittest import mock
        import luna_imaging.http as h
        calls = []

        def urlopen(req, timeout=None):
            calls.append(req)
            return side_effect()

        with mock.patch.object(h.urllib.request, "urlopen", urlopen), \
                mock.patch.object(h, "_sleep") as sl:
            with self.assertRaises(ProviderError) as cm:
                with_retries(lambda: h.post_json("http://x", {}, {}, 1), max_retries)
        return len(calls), sl.call_count, cm.exception

    def test_connect_failure_is_retried(self):
        import urllib.error

        def fail():
            raise urllib.error.URLError(ConnectionRefusedError("refused"))

        n, sleeps, err = self._post_calls(fail)
        self.assertEqual((n, sleeps), (3, 2))
        self.assertTrue(err.retryable)
        self.assertIsNone(err.status)
        self.assertFalse(err.billed)
        self.assertNotIn("billed", str(err))

    def test_read_failures_are_not_retried(self):
        import http.client

        def timeout():
            raise TimeoutError("timed out")

        def disconnected():
            raise http.client.RemoteDisconnected("closed")

        def reset():
            raise ConnectionResetError("reset")

        for fail in (timeout, disconnected, reset):
            with self.subTest(fail=fail.__name__):
                n, sleeps, err = self._post_calls(fail)
                self.assertEqual((n, sleeps), (1, 0))
                self.assertFalse(err.retryable)
                self.assertTrue(err.billed)
                self.assertIn("may still have been billed", str(err))

    def test_invalid_json_on_200_not_retried(self):
        import io as _io

        n, sleeps, err = self._post_calls(lambda: _io.BytesIO(b"<html>not json</html>"))
        self.assertEqual((n, sleeps), (1, 0))
        self.assertTrue(err.billed)
        self.assertIn("invalid JSON", str(err))
        self.assertIn("may still have been billed", str(err))

    def test_http_status_errors(self):
        import io as _io
        import urllib.error

        def http_error(code):
            def fail():
                raise urllib.error.HTTPError("http://x", code, "err", {}, _io.BytesIO(b'{"error": "e"}'))
            return fail

        n, sleeps, err = self._post_calls(http_error(400))
        self.assertEqual((n, sleeps, err.status, err.billed), (1, 0, 400, False))
        n, sleeps, err = self._post_calls(http_error(503))
        self.assertEqual((n, sleeps, err.status), (3, 2, 503))

    def test_status_none_default_not_retryable(self):
        calls = []

        def fn():
            calls.append(1)
            raise ProviderError("x")

        with self.assertRaises(ProviderError):
            with_retries(fn, 3)
        self.assertEqual(len(calls), 1)
        plain = ProviderError("x")
        self.assertFalse(plain.billed)
        self.assertFalse(plain.retryable)


if __name__ == "__main__":
    unittest.main()
