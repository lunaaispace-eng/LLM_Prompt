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
        self.assertEqual(set(out.tobytes()), {0, 255})

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

    def test_composite_transparent_patch_keeps_original(self):
        base = _noise((64, 64))
        patch = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        box = (16, 16, 48, 48)
        out = composite(base, patch, box, _rect_mask((64, 64), box), 0)
        self.assertIsNone(ImageChops.difference(out, base).getbbox())

    def test_composite_half_transparent_patch(self):
        base = Image.new("RGB", (64, 64), (0, 0, 0))
        patch = Image.new("RGBA", (32, 32), (255, 255, 255, 128))
        box = (16, 16, 48, 48)
        out = composite(base, patch, box, _rect_mask((64, 64), box), 0)
        self.assertEqual(out.getpixel((30, 30)), (128, 128, 128))
        self.assertEqual(out.getpixel((2, 2)), (0, 0, 0))

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

    def test_composite_small_island_full_strength_beside_big_region(self):
        base = _noise((200, 200))
        patch = Image.new("RGB", (200, 200), (200, 10, 30))
        mask = _rect_mask((200, 200), (0, 0, 160, 160))
        mask.paste(255, (180, 180, 184, 184))
        out = composite(base, patch, (0, 0, 200, 200), mask, 16)
        got = out.getpixel((182, 182))
        self.assertTrue(all(abs(a - b) <= 5 for a, b in zip(got, (200, 10, 30))), got)
        self.assertEqual(out.getpixel((80, 80)), (200, 10, 30))
        self.assertEqual(out.getpixel((170, 100)), base.getpixel((170, 100)))

    def test_composite_region_unchanged_by_distant_region(self):
        base = _noise((200, 200))
        patch = Image.new("RGB", (200, 200), (200, 10, 30))
        a = _rect_mask((200, 200), (10, 10, 30, 30))
        b = a.copy()
        b.paste(255, (170, 170, 190, 190))
        oa = composite(base, patch, (0, 0, 200, 200), a, 16)
        ob = composite(base, patch, (0, 0, 200, 200), b, 16)
        self.assertEqual(oa.crop((0, 0, 100, 100)).tobytes(), ob.crop((0, 0, 100, 100)).tobytes())

    def test_composite_box_past_base_is_clamped(self):
        base = _noise((64, 64))
        patch = Image.new("RGB", (80, 80), (255, 0, 0))
        out = composite(base, patch, (32, 32, 96, 96), _rect_mask((64, 64), (32, 32, 64, 64)), 0)
        self.assertEqual(out.size, (64, 64))
        self.assertEqual(out.getpixel((63, 63)), (255, 0, 0))
        self.assertEqual(out.getpixel((10, 10)), base.getpixel((10, 10)))

    def test_composite_large_region_speed(self):
        import time
        base = Image.new("RGB", (2048, 2048), (5, 5, 5))
        patch = Image.new("RGB", (2048, 2048), (250, 0, 0))
        t0 = time.time()
        composite(base, patch, (0, 0, 2048, 2048), _rect_mask((2048, 2048), (256, 256, 1792, 1792)), 64)
        # Generous bound (about 0.5 s measured): catches a pathological slowdown, cannot flake.
        self.assertLess(time.time() - t0, 10.0)


if __name__ == "__main__":
    unittest.main()
