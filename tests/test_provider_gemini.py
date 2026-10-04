import base64
import io
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import Image

from luna_imaging.http import ProviderError
from luna_imaging.providers import gemini
from luna_imaging.types import EditRequest


def _b64(color=(255, 0, 0)):
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), color).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _img():
    return Image.new("RGB", (4, 4), (10, 20, 30))


class Fake:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, body, timeout))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def ok(parts=None, usage=None):
    parts = parts or [{"inlineData": {"mimeType": "image/png", "data": _b64()}}]
    return {"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}],
            "usageMetadata": usage or {"totalTokenCount": 5}}


def req(**kw):
    d = dict(provider="gemini", model="gemini-3.1-flash-image", operation="edit",
             prompt="make it blue", images=[_img()], aspect_ratio="auto",
             resolution="auto")
    d.update(kw)
    return EditRequest(**d)


def run(r, *responses):
    fake = Fake(*(responses or (ok(),)))
    with mock.patch("luna_imaging.providers.gemini.post_json", fake):
        res = gemini.run(r, "KEY")
    return res, fake


class GeminiTests(unittest.TestCase):
    def test_body_order_images_then_text(self):
        _, fake = run(req(images=[_img(), _img()]))
        url, headers, body, _ = fake.calls[0]
        self.assertTrue(url.endswith("/v1beta/models/gemini-3.1-flash-image:generateContent"))
        self.assertEqual(headers["x-goog-api-key"], "KEY")
        parts = body["contents"][0]["parts"]
        self.assertEqual(body["contents"][0]["role"], "user")
        self.assertEqual(len(parts), 3)
        self.assertIn("inline_data", parts[0])
        self.assertEqual(parts[0]["inline_data"]["mime_type"], "image/png")
        self.assertIn("inline_data", parts[1])
        self.assertEqual(parts[-1], {"text": "make it blue"})
        self.assertEqual(body["generationConfig"]["responseModalities"], ["TEXT", "IMAGE"])

    def test_image_config_only_when_set(self):
        _, fake = run(req(aspect_ratio="auto", resolution="auto"))
        self.assertNotIn("imageConfig", fake.calls[0][2]["generationConfig"])
        _, fake = run(req(aspect_ratio="16:9", resolution="2K"))
        self.assertEqual(fake.calls[0][2]["generationConfig"]["imageConfig"],
                         {"aspectRatio": "16:9", "imageSize": "2K"})

    def test_resolution_clamped_with_note(self):
        res, fake = run(req(model="gemini-2.5-flash-image", resolution="4K"))
        cfg = fake.calls[0][2]["generationConfig"]["imageConfig"]
        self.assertEqual(cfg["imageSize"], "1K")
        self.assertTrue(any("clamped" in i for i in res.info))

    def test_thought_parts_skipped(self):
        parts = [{"thought": True, "inlineData": {"mimeType": "image/png", "data": _b64()}},
                 {"text": "hello"},
                 {"inline_data": {"mime_type": "image/png", "data": _b64()}}]
        res, _ = run(req(), ok(parts))
        self.assertEqual(len(res.images), 1)
        self.assertEqual(res.images[0].mode, "RGBA")
        self.assertEqual(res.text, "hello")

    def test_too_many_refs_raises(self):
        r = req(model="gemini-2.5-flash-image", images=[_img() for _ in range(4)])
        with mock.patch("luna_imaging.providers.gemini.post_json", Fake(ok())):
            with self.assertRaises(ValueError) as cm:
                gemini.run(r, "KEY")
        self.assertIn("at most 3", str(cm.exception))

    def test_safety_block_none(self):
        _, fake = run(req())
        s = fake.calls[0][2]["safetySettings"]
        self.assertEqual(len(s), 4)
        self.assertEqual({x["threshold"] for x in s}, {"BLOCK_NONE"})
        self.assertEqual({x["category"] for x in s}, {
            "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT"})

    def test_no_image_raises_with_reason(self):
        blocked = {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}
        with self.assertRaises(ProviderError) as cm:
            run(req(), blocked)
        self.assertIn("PROHIBITED_CONTENT", str(cm.exception))
        fin = {"candidates": [{"finishReason": "IMAGE_SAFETY"}]}
        with self.assertRaises(ProviderError) as cm:
            run(req(), fin)
        self.assertIn("IMAGE_SAFETY", str(cm.exception))

    def test_n_sequential_calls_cost_usage(self):
        res, fake = run(req(n=3, resolution="2K"))
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(len(res.images), 3)
        self.assertAlmostEqual(res.cost_usd, 0.101 * 3)
        self.assertIn("totalTokenCount", res.usage)

    def test_auto_resolution_prices_at_1k(self):
        res, _ = run(req())
        self.assertAlmostEqual(res.cost_usd, 0.067)

    def test_generate_without_images(self):
        _, fake = run(req(operation="generate", images=[]))
        self.assertEqual(fake.calls[0][2]["contents"][0]["parts"], [{"text": "make it blue"}])


if __name__ == "__main__":
    unittest.main()
