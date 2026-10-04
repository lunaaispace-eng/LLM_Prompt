import os
import random
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import Image, ImageChops  # noqa: E402

from luna_imaging import studio  # noqa: E402
from luna_imaging.types import EditRequest, EditResult  # noqa: E402

GREEN = (0, 255, 0)
GEMINI = "gemini-3-pro-image-preview"
OPENAI = "gpt-image-2"


def _noise(size, seed=1):
    rnd = random.Random(seed)
    img = Image.new("RGB", size)
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(size[0] * size[1])])
    return img


def _rect_mask(size, box):
    m = Image.new("L", size, 0)
    m.paste(255, box)
    return m


class _Fake:
    """Records calls; returns solid green RGBA images (`size` fixed, else the size of images[0] sent)."""

    def __init__(self, size=None, count=1, alpha=255):
        self.calls = []
        self.size = size
        self.count = count
        self.alpha = alpha

    def __call__(self, req, key, mask=None):
        self.calls.append((req, key, mask))
        size = self.size or (req.images[0].size if req.images else (32, 32))
        imgs = [Image.new("RGBA", size, GREEN + (self.alpha,)) for _ in range(self.count)]
        return EditResult(images=imgs, info=["size : fake"])


def _outside_equal(test, out, orig, mask):
    """Every pixel where `mask` is 0 equals the original."""
    diff = ImageChops.difference(out.convert("RGB"), orig.convert("RGB"))
    outside = ImageChops.multiply(diff.convert("L"), ImageChops.invert(mask.convert("L")))
    test.assertIsNone(outside.getbbox(), "pixels outside the region changed")


def _outside_box_equal(test, out, orig, box):
    W, H = orig.size
    m = Image.new("L", (W, H), 0)
    m.paste(255, box)
    _outside_equal(test, out, orig, m)


class Studio(unittest.TestCase):
    def setUp(self):
        self.img = _noise((60, 60))
        self.mask = _rect_mask((60, 60), (20, 20, 40, 40))

    def _req(self, model, operation="inpaint", **kw):
        kw.setdefault("images", [self.img])
        if operation == "inpaint":
            kw.setdefault("mask", self.mask)
        return EditRequest(provider=studio.provider_for(model), model=model, operation=operation,
                           prompt="a red ball", **kw)

    # --- brief tests ---------------------------------------------------------------------------

    def test_empty_mask_raises_before_call(self):
        fake = _Fake()
        req = self._req(GEMINI, mask=Image.new("L", (60, 60), 0))
        with mock.patch("luna_imaging.providers.gemini.run", fake):
            with self.assertRaises(ValueError) as cm:
                studio.run(req, "k")
        self.assertIn("mask is empty", str(cm.exception))
        self.assertEqual(fake.calls, [])

    def test_inpaint_crop_outside_unchanged(self):
        fake = _Fake()
        req = self._req(GEMINI)
        with mock.patch("luna_imaging.providers.gemini.run", fake):
            res, used = studio.run(req, "k")
        out = res.images[0]
        self.assertEqual(out.size, self.img.size)
        # expanded box: 20 px side * 0.25 = 5 px each way
        _outside_box_equal(self, out, self.img, (15, 15, 45, 45))
        _outside_equal(self, out, self.img, self.mask)
        self.assertEqual(out.convert("RGB").getpixel((30, 30)), GREEN)
        sent, _, sent_mask = fake.calls[0]
        self.assertIsNone(sent_mask)
        self.assertEqual(sent.images[0].size, (30, 30))
        self.assertEqual(sent.prompt, "Edit only the marked region of the first image: a red ball")
        self.assertEqual(sent.aspect_ratio, "auto")
        self.assertIn("mode : crop", res.info)
        self.assertEqual(used.size, (60, 60))
        self.assertEqual(used.mode, "L")

    def test_inpaint_native_openai_outside_unchanged(self):
        fake = _Fake(size=(64, 64))  # a fully green image, not the size it was sent
        req = self._req(OPENAI)
        with mock.patch("luna_imaging.providers.openai.run", fake):
            res, used = studio.run(req, "k")
        out = res.images[0]
        self.assertEqual(out.size, (60, 60))
        _outside_equal(self, out, self.img, self.mask)
        self.assertEqual(out.convert("RGB").getpixel((30, 30)), GREEN)
        sent, _, sent_mask = fake.calls[0]
        self.assertIsNotNone(sent_mask)
        self.assertEqual(sent_mask.size, (60, 60))
        self.assertIs(sent.images[0], self.img)
        self.assertEqual((sent.width, sent.height), (60, 60))
        self.assertEqual(sent.prompt, "a red ball")
        self.assertIn("mode : native", res.info)

    def test_patch_size_mismatch_resized(self):
        fake = _Fake(size=(7, 7))
        req = self._req(GEMINI)
        with mock.patch("luna_imaging.providers.gemini.run", fake):
            res, _ = studio.run(req, "k")
        self.assertEqual(fake.calls[0][0].images[0].size, (30, 30))
        self.assertEqual(res.images[0].size, self.img.size)
        _outside_equal(self, res.images[0], self.img, self.mask)

    def test_outpaint_grows_canvas(self):
        fake = _Fake()
        img = _noise((100, 50), seed=2)
        req = self._req(GEMINI, operation="outpaint", images=[img], outpaint=(0, 0, 50, 0))
        with mock.patch("luna_imaging.providers.gemini.run", fake):
            res, used = studio.run(req, "k")
        self.assertEqual(res.images[0].size, (150, 50))
        self.assertEqual(used.size, (150, 50))
        self.assertEqual(res.images[0].convert("RGB").getpixel((140, 25)), GREEN)

    def test_too_many_images(self):
        fake = _Fake()
        imgs = [_noise((20, 20), seed=i) for i in range(6)]
        req = self._req("grok-imagine-image-2.0", operation="edit", images=imgs)
        with mock.patch("luna_imaging.providers.xai.run", fake):
            with self.assertRaises(ValueError) as cm:
                studio.run(req, "k")
        self.assertIn("at most 5", str(cm.exception))
        self.assertEqual(str(cm.exception), "grok-imagine-image-2.0 takes at most 5 images")
        self.assertEqual(fake.calls, [])

    def test_generate_with_images_rejected(self):
        fake = _Fake()
        req = self._req(GEMINI, operation="generate")
        with mock.patch("luna_imaging.providers.gemini.run", fake):
            with self.assertRaises(ValueError):
                studio.run(req, "k")
        self.assertEqual(fake.calls, [])

    # --- controller rulings --------------------------------------------------------------------

    def test_native_forced_on_gemini_falls_back_to_crop(self):
        fake = _Fake()
        req = self._req(GEMINI, mask_mode="native")
        with mock.patch("luna_imaging.providers.gemini.run", fake):
            res, _ = studio.run(req, "k")
        self.assertEqual(fake.calls[0][0].images[0].size, (30, 30))
        self.assertIn("mode : crop", res.info)
        self.assertTrue(any("native" in s and "crop" in s and s != "mode : crop" for s in res.info), res.info)
        _outside_equal(self, res.images[0], self.img, self.mask)

    def test_outpaint_keeps_original_pixels(self):
        fake = _Fake()
        img = _noise((100, 50), seed=3)
        req = self._req(GEMINI, operation="outpaint", images=[img], outpaint=(10, 5, 30, 0), feather_px=16)
        with mock.patch("luna_imaging.providers.gemini.run", fake):
            res, _ = studio.run(req, "k")
        out = res.images[0].convert("RGB")
        self.assertEqual(out.size, (140, 55))
        self.assertIsNone(ImageChops.difference(out.crop((10, 5, 110, 55)), img).getbbox())
        self.assertEqual(out.getpixel((2, 2)), GREEN)

    def test_n2_composites_both(self):
        fake = _Fake(count=2)
        req = self._req(GEMINI, n=2)
        with mock.patch("luna_imaging.providers.gemini.run", fake):
            res, _ = studio.run(req, "k")
        self.assertEqual(len(res.images), 2)
        for out in res.images:
            self.assertEqual(out.size, self.img.size)
            _outside_equal(self, out, self.img, self.mask)
            self.assertEqual(out.convert("RGB").getpixel((30, 30)), GREEN)

    # --- extra edges ---------------------------------------------------------------------------

    def test_crop_mode_on_openai_sends_no_mask(self):
        fake = _Fake()
        req = self._req(OPENAI, mask_mode="crop")
        with mock.patch("luna_imaging.providers.openai.run", fake):
            res, _ = studio.run(req, "k")
        sent, _, sent_mask = fake.calls[0]
        self.assertIsNone(sent_mask)
        self.assertEqual(sent.images[0].size, (30, 30))
        self.assertEqual((sent.width, sent.height), (30, 30))
        self.assertIn("mode : crop", res.info)
        _outside_equal(self, res.images[0], self.img, self.mask)

    def test_caller_request_not_mutated(self):
        fake = _Fake()
        req = self._req(OPENAI)
        with mock.patch("luna_imaging.providers.openai.run", fake):
            studio.run(req, "k")
        self.assertEqual((req.width, req.height), (0, 0))
        self.assertEqual(req.prompt, "a red ball")
        self.assertEqual(req.images, [self.img])

    def test_mask_at_other_resolution(self):
        fake = _Fake()
        small = _rect_mask((30, 30), (10, 10, 20, 20))  # same region at half resolution
        req = self._req(GEMINI, mask=small)
        with mock.patch("luna_imaging.providers.gemini.run", fake):
            res, used = studio.run(req, "k")
        self.assertEqual(used.size, (60, 60))
        _outside_equal(self, res.images[0], self.img, self.mask)

    def test_transparent_result_on_rgb_original(self):
        fake = _Fake(alpha=0)
        req = self._req(OPENAI)
        with mock.patch("luna_imaging.providers.openai.run", fake):
            res, _ = studio.run(req, "k")
        self.assertEqual(res.images[0].mode, "RGB")
        _outside_equal(self, res.images[0], self.img, self.mask)

    def test_refs_follow_crop(self):
        fake = _Fake()
        ref = _noise((20, 20), seed=9)
        req = self._req(GEMINI, images=[self.img, ref])
        with mock.patch("luna_imaging.providers.gemini.run", fake):
            studio.run(req, "k")
        sent = fake.calls[0][0]
        self.assertEqual(len(sent.images), 2)
        self.assertIs(sent.images[1], ref)

    def test_generate_passthrough(self):
        fake = _Fake()
        req = self._req(GEMINI, operation="generate", images=[])
        with mock.patch("luna_imaging.providers.gemini.run", fake):
            res, used = studio.run(req, "k")
        self.assertIsNone(used)
        self.assertEqual(res.info, ["size : fake"])
        self.assertIs(fake.calls[0][0], req)

    def test_validation_messages(self):
        fake = _Fake()
        cases = [
            self._req(GEMINI, operation="edit", images=[]),
            self._req(GEMINI, operation="compose", images=[]),
            self._req(GEMINI, operation="inpaint", mask=None),
            self._req(GEMINI, operation="inpaint", images=[]),
            self._req(GEMINI, operation="outpaint", outpaint=(0, 0, 0, 0)),
            self._req(GEMINI, operation="outpaint", images=[], outpaint=(0, 0, 10, 0)),
            self._req(GEMINI, operation="outpaint", outpaint=(-5, 0, 10, 0)),
            self._req(GEMINI, operation="upscale"),
        ]
        with mock.patch("luna_imaging.providers.gemini.run", fake):
            for req in cases:
                with self.subTest(op=req.operation):
                    with self.assertRaises(ValueError):
                        studio.run(req, "k")
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
