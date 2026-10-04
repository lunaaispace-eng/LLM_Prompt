import io
import os
import random
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import Image, ImageChops  # noqa: E402

from luna_imaging.masks import (  # noqa: E402
    boxes_to_mask, composite, expand_box, fit_mask, mask_bbox, openai_alpha_mask, outpaint_canvas,
)


def _noise(size, seed=1):
    rnd = random.Random(seed)
    img = Image.new("RGB", size)
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(size[0] * size[1])])
    return img


def _rect_mask(size, box):
    m = Image.new("L", size, 0)
    m.paste(255, box)
    return m


class MaskMaths(unittest.TestCase):
    def test_fit_mask_resizes_nearest(self):
        m = _rect_mask((64, 64), (0, 0, 32, 32))
        out = fit_mask(m, (128, 128))
        self.assertEqual(out.size, (128, 128))
        self.assertEqual(mask_bbox(out), (0, 0, 64, 64))
        self.assertEqual(set(out.getdata()), {0, 255})

    def test_bbox_none_for_empty(self):
        self.assertIsNone(mask_bbox(Image.new("L", (10, 10), 0)))

    def test_expand_box_clamps(self):
        self.assertEqual(expand_box((10, 10, 20, 20), 1.0, (25, 25)), (0, 0, 25, 25))

    def test_boxes_to_mask_normalized(self):
        m = boxes_to_mask([[0.5, 0.5, 0.5, 0.5]], (100, 100), True)
        self.assertEqual(mask_bbox(m), (50, 50, 100, 100))

    def test_openai_alpha_inverts(self):
        m = _rect_mask((16, 16), (0, 0, 8, 16))
        png = openai_alpha_mask(m, (16, 16))
        img = Image.open(io.BytesIO(png))
        self.assertEqual(img.mode, "RGBA")
        self.assertEqual(img.getpixel((2, 2))[3], 0)
        self.assertEqual(img.getpixel((12, 2))[3], 255)

    def test_outpaint_canvas(self):
        img = Image.new("RGB", (100, 50), (1, 2, 3))
        canvas, mask = outpaint_canvas(img, (10, 0, 30, 0))
        self.assertEqual(canvas.size, (140, 50))
        self.assertEqual(mask.size, (140, 50))
        self.assertEqual(mask.getpixel((5, 5)), 255)
        self.assertEqual(mask.getpixel((50, 25)), 0)
        self.assertEqual(mask.getpixel((135, 25)), 255)
        self.assertEqual(canvas.getpixel((50, 25)), (1, 2, 3))

    def test_composite_outside_box_unchanged(self):
        base = _noise((64, 64))
        patch = Image.new("RGB", (10, 10), (255, 0, 0))
        box = (16, 16, 48, 48)
        out = composite(base, patch, box, _rect_mask((64, 64), box), 0)
        self.assertEqual(out.mode, "RGB")
        diff = ImageChops.difference(out, base)
        for x in range(64):
            for y in range(64):
                if not (16 <= x < 48 and 16 <= y < 48):
                    self.assertEqual(diff.getpixel((x, y)), (0, 0, 0))
        self.assertEqual(out.getpixel((30, 30)), (255, 0, 0))

    def test_composite_rgba_patch_on_rgb_base(self):
        base = _noise((64, 64))
        patch = Image.new("RGBA", (20, 20), (0, 255, 0, 10))
        box = (16, 16, 48, 48)
        out = composite(base, patch, box, _rect_mask((64, 64), box), 4)
        self.assertEqual(out.mode, "RGB")

    def test_composite_feather_never_touches_outside_mask(self):
        base = _noise((64, 64))
        patch = Image.new("RGB", (40, 40), (200, 10, 30))
        mask = _rect_mask((64, 64), (24, 24, 40, 40))
        out = composite(base, patch, (8, 8, 56, 56), mask, 16)
        for x in range(64):
            for y in range(64):
                if mask.getpixel((x, y)) == 0:
                    self.assertEqual(out.getpixel((x, y)), base.getpixel((x, y)))
        self.assertEqual(out.getpixel((32, 32)), (200, 10, 30))


if __name__ == "__main__":
    unittest.main()
