import base64
import io
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import Image

from luna_imaging.cost import openai_cost
from luna_imaging.providers import openai as prov
from luna_imaging.types import EditRequest


def _png_b64():
    buf = io.BytesIO()
    Image.new("RGBA", (1, 1), (1, 2, 3, 255)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


USAGE = {"input_tokens": 100, "output_tokens": 196,
         "input_tokens_details": {"text_tokens": 100, "image_tokens": 0},
         "output_tokens_details": {"image_tokens": 196, "text_tokens": 0}}


class Fake:
    def __init__(self):
        self.calls = []

    def json(self, url, headers, body, timeout):
        self.calls.append(("json", url, headers, body, timeout))
        return {"data": [{"b64_json": _png_b64()}], "usage": USAGE}

    def multipart(self, url, headers, fields, files, timeout):
        self.calls.append(("multipart", url, headers, fields, files, timeout))
        return {"data": [{"b64_json": _png_b64()}], "usage": USAGE}


def _run(req, mask=None):
    f = Fake()
    with mock.patch.object(prov, "post_json", f.json), \
            mock.patch.object(prov, "post_multipart", f.multipart):
        res = prov.run(req, "sk-test", mask)
    return res, f.calls[0]


def _req(**kw):
    base = dict(provider="openai", model="gpt-image-2.5-flare", operation="generate",
                prompt="a cat", aspect_ratio="16:9", resolution="1K")
    base.update(kw)
    return EditRequest(**base)


class OpenAIProviderTests(unittest.TestCase):
    def test_generate_hits_generations(self):
        res, call = _run(_req())
        self.assertEqual(call[0], "json")
        self.assertTrue(call[1].endswith("/images/generations"))
        self.assertEqual(call[3]["size"], "1360x768")
        self.assertNotIn("input_fidelity", call[3])
        self.assertEqual(call[2]["Authorization"], "Bearer sk-test")
        self.assertEqual(res.images[0].mode, "RGBA")
        self.assertIn("size : 1360x768", res.info)

    def test_edit_multipart_with_mask(self):
        imgs = [Image.new("RGB", (64, 64)), Image.new("RGB", (32, 32))]
        mask = Image.new("L", (64, 64), 0)
        mask.paste(255, (0, 0, 10, 10))
        res, call = _run(_req(operation="inpaint", images=imgs), mask)
        self.assertEqual(call[0], "multipart")
        self.assertTrue(call[1].endswith("/images/edits"))
        self.assertEqual([f[0] for f in call[4]], ["image[]", "image[]", "mask"])
        m = Image.open(io.BytesIO(call[4][2][2]))
        self.assertEqual(m.size, (64, 64))
        self.assertEqual(m.getpixel((0, 0))[3], 0)
        self.assertEqual(m.getpixel((40, 40))[3], 255)
        self.assertEqual(call[3]["n"], "1")

    def test_transparent_dropped_on_gpt_image_2(self):
        res, call = _run(_req(model="gpt-image-2", background="transparent"))
        self.assertEqual(call[3]["background"], "auto")
        self.assertTrue(any("transparent" in i for i in res.info))

    def test_transparent_jpeg_switches_to_png(self):
        res, call = _run(_req(background="transparent", extra={"output_format": "jpeg"}))
        self.assertEqual(call[3]["output_format"], "png")
        self.assertNotIn("output_compression", call[3])
        self.assertTrue(any("png" in i for i in res.info))

    def test_quality_clamped_on_1x(self):
        res, call = _run(_req(model="gpt-image-1.5", quality="max"))
        self.assertEqual(call[3]["quality"], "high")
        self.assertTrue(any("high" in i for i in res.info))

    def test_quality_kept_on_25(self):
        _, call = _run(_req(quality="max"))
        self.assertEqual(call[3]["quality"], "max")

    def test_input_fidelity_gate(self):
        img = [Image.new("RGB", (32, 32))]
        _, call = _run(_req(model="gpt-image-1.5", operation="edit", images=img,
                            extra={"input_fidelity": "high"}))
        self.assertEqual(call[3]["input_fidelity"], "high")
        res, call = _run(_req(operation="edit", images=img, extra={"input_fidelity": "high"}))
        self.assertNotIn("input_fidelity", call[3])
        self.assertTrue(any("input_fidelity" in i for i in res.info))

    def test_compression_and_moderation(self):
        _, call = _run(_req(extra={"output_format": "webp", "moderation": "auto"}))
        self.assertEqual(call[3]["output_compression"], 100)
        self.assertEqual(call[3]["moderation"], "auto")

    def test_cost_filled(self):
        res, _ = _run(_req())
        self.assertEqual(res.cost_usd, openai_cost("gpt-image-2.5-flare", USAGE))
        self.assertIsNotNone(res.cost_usd)
        self.assertEqual(res.usage, USAGE)

    def test_no_image_raises(self):
        f = Fake()
        f.json = lambda *a, **k: {"data": []}
        with mock.patch.object(prov, "post_json", f.json):
            with self.assertRaises(RuntimeError):
                prov.run(_req(), "k")


if __name__ == "__main__":
    unittest.main()
