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
        # D4: crop-mode outpaint sends the whole canvas with the outpaint prompt
        sent, _, sent_mask = fake.calls[0]
        self.assertEqual(sent.images[0].size, (150, 50))
        self.assertIsNone(sent_mask)
        self.assertEqual(sent.prompt, studio.OUTPAINT_PREFIX + "a red ball")
        self.assertTrue(sent.prompt.startswith("Extend this image into the grey border areas"))
        self.assertEqual(sent.aspect_ratio, "auto")
        self.assertEqual(sent.images[0].getpixel((140, 25)), (127, 127, 127))
        self.assertEqual(sent.images[0].getpixel((10, 10)), img.getpixel((10, 10)))

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

    def test_outpaint_crop_mode_on_openai_sends_whole_canvas(self):
        fake = _Fake(size=(160, 64))  # API-snapped size, not the canvas
        img = _noise((100, 50), seed=4)
        req = self._req(OPENAI, operation="outpaint", images=[img], outpaint=(0, 0, 50, 0),
                        mask_mode="crop")
        with mock.patch("luna_imaging.providers.openai.run", fake):
            res, _ = studio.run(req, "k")
        sent, _, sent_mask = fake.calls[0]
        self.assertIsNone(sent_mask)
        self.assertEqual(sent.images[0].size, (150, 50))
        self.assertEqual((sent.width, sent.height), (150, 50))
        self.assertTrue(sent.prompt.startswith(studio.OUTPAINT_PREFIX))
        out = res.images[0].convert("RGB")
        self.assertIsNone(ImageChops.difference(out.crop((0, 0, 100, 50)), img).getbbox())
        self.assertEqual(out.getpixel((120, 25)), GREEN)

    # D6: OpenAI native outpaint - canvas + mask + width/height at canvas size; the original
    # area of the output equals the original exactly.
    def test_outpaint_native_openai(self):
        fake = _Fake(size=(144, 64))  # API-snapped size, not the canvas
        img = _noise((100, 50), seed=5)
        req = self._req(OPENAI, operation="outpaint", images=[img], outpaint=(10, 5, 30, 0))
        with mock.patch("luna_imaging.providers.openai.run", fake):
            res, used = studio.run(req, "k")
        sent, _, sent_mask = fake.calls[0]
        self.assertEqual(sent.images[0].size, (140, 55))
        self.assertEqual((sent.width, sent.height), (140, 55))
        self.assertEqual(sent.prompt, "a red ball")
        self.assertIsNotNone(sent_mask)
        self.assertEqual(sent_mask.size, (140, 55))
        self.assertEqual(sent_mask.getpixel((2, 2)), 255)      # new area
        self.assertEqual(sent_mask.getpixel((60, 30)), 0)      # original area
        self.assertEqual(sent.images[0].getpixel((60, 30)), img.getpixel((50, 25)))
        self.assertIn("mode : native", res.info)
        out = res.images[0].convert("RGB")
        self.assertEqual(out.size, (140, 55))
        self.assertIsNone(ImageChops.difference(out.crop((10, 5, 110, 55)), img).getbbox())
        self.assertEqual(out.getpixel((2, 2)), GREEN)
        self.assertEqual(out.getpixel((135, 50)), GREEN)
        self.assertEqual(used.getpixel((2, 2)), 255)

    # F3: transparent is not applied to region edits (the patch is composited onto the original).
    def test_transparent_not_sent_for_region_edits(self):
        for op, kw in (("inpaint", {}), ("outpaint", {"outpaint": (0, 0, 10, 0)})):
            with self.subTest(op=op):
                fake = _Fake(alpha=0)
                req = self._req("gpt-image-2.5-flare", operation=op, background="transparent", **kw)
                with mock.patch("luna_imaging.providers.openai.run", fake):
                    res, _ = studio.run(req, "k")
                self.assertEqual(fake.calls[0][0].background, "auto")
                self.assertTrue(any("transparent background is not applied to region edits" in i
                                    for i in res.info), res.info)
                self.assertEqual(req.background, "transparent")  # caller's request not mutated
        fake = _Fake()
        req = self._req("gpt-image-2.5-flare", operation="generate", images=[], background="transparent")
        with mock.patch("luna_imaging.providers.openai.run", fake):
            studio.run(req, "k")
        self.assertEqual(fake.calls[0][0].background, "transparent")

    # F4: region edits always send the size of the image actually sent; wired width/height
    # are ignored with a note.
    def test_region_edit_ignores_wired_size(self):
        cases = [("auto", (60, 60)), ("crop", (30, 30))]
        for mode, size in cases:
            with self.subTest(mode=mode):
                fake = _Fake()
                req = self._req(OPENAI, mask_mode=mode, width=1024, height=576, aspect_ratio="21:9")
                with mock.patch("luna_imaging.providers.openai.run", fake):
                    res, _ = studio.run(req, "k")
                sent = fake.calls[0][0]
                self.assertEqual((sent.width, sent.height), size)
                self.assertEqual(sent.aspect_ratio, "auto")
                self.assertTrue(any(i.startswith("width/height ignored for inpaint") for i in res.info),
                                res.info)
                self.assertEqual(res.images[0].size, (60, 60))
        fake = _Fake()
        req = self._req(OPENAI, mask_mode="crop", width=1024, height=0)  # only one side wired
        with mock.patch("luna_imaging.providers.openai.run", fake):
            res, _ = studio.run(req, "k")
        self.assertEqual((fake.calls[0][0].width, fake.calls[0][0].height), (30, 30))
        self.assertTrue(any("width/height ignored" in i for i in res.info))
        fake = _Fake()
        img = _noise((40, 20), seed=6)
        req = self._req(GEMINI, operation="outpaint", images=[img], outpaint=(0, 0, 20, 0),
                        width=512, height=512)
        with mock.patch("luna_imaging.providers.gemini.run", fake):
            res, _ = studio.run(req, "k")
        self.assertEqual(res.images[0].size, (60, 20))
        self.assertTrue(any("width/height ignored for outpaint" in i for i in res.info))

    def test_unwired_region_edit_has_no_size_note(self):
        fake = _Fake()
        with mock.patch("luna_imaging.providers.gemini.run", fake):
            res, _ = studio.run(self._req(GEMINI), "k")
        self.assertFalse(any("width/height" in i for i in res.info), res.info)

    # F4: generate / edit / compose with wired width x height: Gemini / Grok get their nearest
    # aspect when aspect_ratio is auto; OpenAI gets the request unchanged (openai_size sizes it).
    def test_wired_size_picks_nearest_aspect(self):
        imgs2 = [_noise((20, 20), seed=1), _noise((20, 20), seed=2)]
        cases = [
            ("gemini", GEMINI, "generate", [], "auto", "16:9"),
            ("gemini", GEMINI, "edit", [self.img], "auto", "16:9"),
            ("gemini", GEMINI, "edit", [self.img], "4:3", "4:3"),     # explicit aspect kept
            ("xai", "grok-imagine-image-2.0", "compose", imgs2, "auto", "16:9"),
            ("xai", "grok-imagine-image-2.0", "generate", [], "auto", "16:9"),
            ("xai", "grok-imagine-image-2.0", "edit", [self.img], "auto", "auto"),  # 1 input: none
        ]
        for prov, model, op, images, ar, want in cases:
            with self.subTest(model=model, op=op, ar=ar, n=len(images)):
                fake = _Fake()
                req = self._req(model, operation=op, images=images, width=1000, height=600,
                                aspect_ratio=ar)
                with mock.patch(f"luna_imaging.providers.{prov}.run", fake):
                    res, used = studio.run(req, "k")
                self.assertIsNone(used)
                self.assertEqual(fake.calls[0][0].aspect_ratio, want)
                noted = any(i == f"width/height 1000x600 -> aspect_ratio {want}" for i in res.info)
                self.assertEqual(noted, ar == "auto" and want != "auto", res.info)
        fake = _Fake()
        req = self._req("grok-imagine-image-2.0", operation="generate", images=[],
                        width=900, height=2000)
        with mock.patch("luna_imaging.providers.xai.run", fake):
            studio.run(req, "k")
        self.assertEqual(fake.calls[0][0].aspect_ratio, "9:20")  # xAI list, not Gemini's
        fake = _Fake()
        req = self._req(OPENAI, operation="generate", images=[], width=1000, height=600)
        with mock.patch("luna_imaging.providers.openai.run", fake):
            studio.run(req, "k")
        self.assertIs(fake.calls[0][0], req)

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
