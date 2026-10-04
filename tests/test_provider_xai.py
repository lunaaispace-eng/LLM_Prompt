import base64
import io
import os
import sys
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import Image

from luna_imaging.http import ProviderError
from luna_imaging.providers import xai
from luna_imaging.types import EditRequest


def _b64():
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), (1, 2, 3)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


class Fake:
    def __init__(self):
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, body, timeout))
        return {"data": [{"b64_json": _b64(), "revised_prompt": "rp"}],
                "usage": {"cost_in_usd_ticks": 500000000}}


def _req(model="grok-imagine-image-2.0", n_img=0, **kw):
    return EditRequest(provider="xai", model=model, operation="edit" if n_img else "generate",
                       prompt="p", images=[Image.new("RGB", (4, 4)) for _ in range(n_img)], **kw)


class XaiTests(unittest.TestCase):
    def run_req(self, req):
        fake = Fake()
        with mock.patch.object(xai, "post_json", fake):
            res = xai.run(req, "k")
        return fake, res

    def test_edit_cap_quality_model(self):
        with self.assertRaisesRegex(ValueError, "at most 3"):
            self.run_req(_req("grok-imagine-image-quality", 4))

    def test_quality_not_sent_to_pro(self):
        fake, res = self.run_req(_req("grok-imagine-image-pro", quality="medium"))
        self.assertNotIn("quality", fake.calls[0][2])
        fake, _ = self.run_req(_req(quality="medium"))
        self.assertEqual(fake.calls[0][2]["quality"], "medium")

    def test_aspect_auto_omitted_on_generate(self):
        fake, _ = self.run_req(_req(aspect_ratio="auto"))
        body = fake.calls[0][2]
        self.assertNotIn("aspect_ratio", body)
        self.assertTrue(fake.calls[0][0].endswith("/images/generations"))
        self.assertEqual(body["response_format"], "b64_json")
        self.assertEqual(body["resolution"], "1k")

    def test_single_image_edit_forces_auto_aspect(self):
        fake, res = self.run_req(_req(n_img=1, aspect_ratio="16:9"))
        url, _, body, _ = fake.calls[0]
        self.assertTrue(url.endswith("/images/edits"))
        self.assertNotIn("aspect_ratio", body)
        self.assertTrue(body["images"][0]["url"].startswith("data:image/png;base64,"))
        self.assertTrue(any("aspect" in i for i in res.info))
        fake, _ = self.run_req(_req(n_img=2, aspect_ratio="16:9"))
        self.assertEqual(fake.calls[0][2]["aspect_ratio"], "16:9")

    # F7: resolution auto is omitted silently; a quality that is not sent says so.
    def test_resolution_auto_omitted_without_note(self):
        fake, res = self.run_req(_req(resolution="auto"))
        self.assertNotIn("resolution", fake.calls[0][2])
        self.assertEqual(res.info, [])
        fake, res = self.run_req(_req(resolution="4K"))
        self.assertNotIn("resolution", fake.calls[0][2])
        self.assertTrue(any("4K" in i and "omitted" in i for i in res.info), res.info)
        fake, res = self.run_req(_req(resolution="1.5K"))
        self.assertEqual(fake.calls[0][2]["resolution"], "1.5k")
        self.assertEqual(res.info, [])

    def test_unsent_quality_noted(self):
        for q in ("high", "xhigh", "max"):
            with self.subTest(q=q):
                fake, res = self.run_req(_req(quality=q))
                self.assertNotIn("quality", fake.calls[0][2])
                self.assertTrue(any(f"quality '{q}' not sent" in i for i in res.info), res.info)
        fake, res = self.run_req(_req(quality="auto"))
        self.assertNotIn("quality", fake.calls[0][2])
        self.assertEqual(res.info, [])
        fake, res = self.run_req(_req("grok-imagine-image-pro", quality="low"))
        self.assertNotIn("quality", fake.calls[0][2])
        self.assertTrue(any("no quality field" in i for i in res.info), res.info)

    # D1: seed is never sent (Ruling R8), even when it is in extra.
    def test_seed_never_sent(self):
        fake, _ = self.run_req(_req(extra={"seed": 1}))
        self.assertNotIn("seed", fake.calls[0][2])

    # D2: URL fallback - browser User-Agent, Bearer retry on 403, failures as ProviderError.
    def _url_run(self, urlopen):
        def post(url, headers, body, timeout):
            return {"data": [{"url": "https://imgen.x.ai/abc.png"}],
                    "usage": {"cost_in_usd_ticks": 10}}

        with mock.patch.object(xai, "post_json", post), \
                mock.patch.object(xai.urllib.request, "urlopen", urlopen):
            return xai.run(_req(), "xai-key")

    def test_url_fallback_user_agent_and_bearer_retry(self):
        png = base64.b64decode(_b64())
        seen = []

        def urlopen(r, timeout=None):
            seen.append(r)
            if len(seen) == 1:
                raise urllib.error.HTTPError(r.full_url, 403, "Forbidden", {}, None)
            return io.BytesIO(png)

        res = self._url_run(urlopen)
        self.assertEqual(len(res.images), 1)
        self.assertEqual(res.images[0].mode, "RGBA")
        self.assertEqual(len(seen), 2)
        self.assertIn("Mozilla", seen[0].get_header("User-agent"))
        self.assertIsNone(seen[0].get_header("Authorization"))
        self.assertEqual(seen[1].get_header("Authorization"), "Bearer xai-key")
        self.assertIn("Mozilla", seen[1].get_header("User-agent"))

    def test_url_fallback_failures_are_provider_errors(self):
        def refused(r, timeout=None):
            raise urllib.error.URLError(ConnectionRefusedError("refused"))

        def not_found(r, timeout=None):
            raise urllib.error.HTTPError(r.full_url, 404, "Not Found", {}, None)

        def garbage(r, timeout=None):
            return io.BytesIO(b"this is not an image")

        for fn, text in ((refused, "refused"), (not_found, "HTTP 404"), (garbage, "cannot read")):
            with self.subTest(fn=fn.__name__):
                with self.assertRaises(ProviderError) as cm:
                    self._url_run(fn)
                self.assertIn(text, str(cm.exception))

    def test_cost_from_ticks(self):
        _, res = self.run_req(_req())
        self.assertEqual(res.cost_usd, 0.05)
        self.assertEqual(res.usage["cost_in_usd_ticks"], 500000000)
        self.assertEqual(res.images[0].mode, "RGBA")
        self.assertEqual(res.text, "rp")


if __name__ == "__main__":
    unittest.main()
