import base64
import io
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import Image

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

    def test_cost_from_ticks(self):
        _, res = self.run_req(_req())
        self.assertEqual(res.cost_usd, 0.05)
        self.assertEqual(res.usage["cost_in_usd_ticks"], 500000000)
        self.assertEqual(res.images[0].mode, "RGBA")
        self.assertEqual(res.text, "rp")


if __name__ == "__main__":
    unittest.main()
