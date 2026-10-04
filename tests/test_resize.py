import os
import subprocess
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import Image  # noqa: E402

from luna_imaging import resize  # noqa: E402
from luna_imaging.resize import (  # noqa: E402
    DEFAULT_ITEM, DIRECTOR_RATIOS, MODES, RATIO_PRESETS, apply_state, parse_state, plan_resize,
    ratio_presets, snap,
)


def st(**kw):
    s = dict(DEFAULT_ITEM)
    s.update(kw)
    return s


# (input size, state overrides, expected output size), worked by hand from the Asset Loader code.
CASES = [
    ((1000, 500), dict(), (1000, 500)),
    ((1000, 500), dict(ratio="1:1", mode="longest_side", longest_side=300, snap=64), (320, 320)),
    ((500, 500), dict(mode="longest_side", longest_side=1024), (500, 500)),
    ((500, 500), dict(mode="longest_side", longest_side=1024, allow_upscale=True), (1024, 1024)),
    ((1000, 700), dict(snap=32), (992, 704)),
    ((20, 20), dict(snap=64), (64, 64)),
    ((4000, 3000), dict(mode="max_mp", max_mp=2.0), (1633, 1225)),
    ((200, 300), dict(ratio="1:1"), (200, 200)),
    ((300, 200), dict(ratio="1:1"), (200, 200)),
    ((100, 50), dict(ratio="1:1", ratio_action="pad"), (100, 100)),
    ((100, 400), dict(mode="scale_factor", scale_factor=0.5), (50, 200)),
    ((100, 400), dict(mode="scale_factor", scale_factor=2.0), (100, 400)),
    ((100, 400), dict(mode="scale_factor", scale_factor=2.0, allow_upscale=True), (200, 800)),
    ((1600, 900), dict(ratio="19.5:9"), (1600, 738)),
    ((1000, 1000), dict(ratio="garbage:x"), (1000, 1000)),
]


class Defaults(unittest.TestCase):
    def test_defaults_exact(self):
        self.assertEqual(DEFAULT_ITEM, {
            "mode": "off", "max_mp": 2.0, "longest_side": 1024, "scale_factor": 1.0, "ratio": "",
            "ratio_action": "crop", "crop_anchor": "center", "pad_color": "#000000", "snap": 0,
            "allow_upscale": False,
        })
        self.assertEqual(MODES, ("off", "max_mp", "longest_side", "scale_factor"))


class Parse(unittest.TestCase):
    def test_parse_all_then_items_win(self):
        raw = '{"all": {"mode": "max_mp", "snap": 32}, "items": [{"snap": 64}, {}, "junk"]}'
        out = parse_state(raw, 4)
        self.assertEqual([s["snap"] for s in out], [64, 32, 32, 32])
        self.assertEqual({s["mode"] for s in out}, {"max_mp"})
        self.assertEqual(out[0]["ratio"], "")

    def test_parse_accepts_dict(self):
        out = parse_state({"all": {"snap": 8}}, 2)
        self.assertEqual([s["snap"] for s in out], [8, 8])

    def test_parse_unknown_keys_dropped(self):
        out = parse_state('{"all": {"bogus": 1, "snap": 8}, "items": [{"resample": "nearest"}]}', 1)
        self.assertEqual(out[0], st(snap=8))

    def test_parse_malformed_falls_back(self):
        for raw in ("{not json", "[1, 2]", None, "", "   ", '{"items": ["x"]}', '{"all": 5}', 7):
            with self.subTest(raw=raw):
                self.assertEqual(parse_state(raw, 2), [st(), st()])

    def test_parse_returns_independent_dicts(self):
        a, b = parse_state(None, 2)
        a["snap"] = 99
        self.assertEqual(b["snap"], 0)
        self.assertEqual(DEFAULT_ITEM["snap"], 0)


class Snap(unittest.TestCase):
    def test_snap_multiple_and_min_step(self):
        self.assertEqual(snap(1000, 32), 992)
        self.assertEqual(snap(700, 32), 704)
        self.assertEqual(snap(20, 64), 64)
        self.assertEqual(snap(0, 0), 1)
        self.assertEqual(snap(5, 1), 5)
        self.assertEqual(snap(0, 1), 1)


class Apply(unittest.TestCase):
    def test_off_returns_same_object(self):
        img = Image.new("RGB", (30, 20))
        self.assertIs(apply_state(img, st()), img)

    def test_order_aspect_size_snap(self):
        img = Image.new("RGB", (1000, 500))
        s = st(ratio="1:1", mode="longest_side", longest_side=300, snap=64)
        plan = plan_resize(1000, 500, s)
        self.assertEqual(plan.crop_box, (250, 0, 750, 500))  # crop 500x500 first
        self.assertEqual(plan.out_size, (320, 320))  # 300x300, then snapped
        self.assertEqual(apply_state(img, s).size, (320, 320))

    def test_no_upscale_by_default(self):
        img = Image.new("RGB", (500, 500))
        self.assertIs(apply_state(img, st(mode="longest_side", longest_side=1024)), img)
        out = apply_state(img, st(mode="longest_side", longest_side=1024, allow_upscale=True))
        self.assertEqual(out.size, (1024, 1024))

    def test_max_mp(self):
        self.assertEqual(apply_state(Image.new("RGB", (4000, 3000)), st(mode="max_mp", max_mp=2.0)).size,
                         (1633, 1225))

    def test_ratio_tolerance(self):
        # 1000x999 is 1.001 off 1:1 by 0.001001 -> crops; 1000x1000.5 style within tolerance does not.
        plan = plan_resize(10000, 9999, st(ratio="1:1"))  # |1.0001 - 1| = 0.0001 <= 0.001
        self.assertIsNone(plan.crop_box)
        self.assertFalse(plan.changed)
        img = Image.new("RGB", (10000, 9999))
        self.assertEqual(apply_state(img, st(ratio="1:1")).size, (10000, 9999))
        self.assertEqual(plan_resize(1010, 1000, st(ratio="1:1")).crop_box, (5, 0, 1005, 1000))

    def test_crop_named_anchors(self):
        # 200x300 to 1:1 -> 200x200 box, vertical placement varies.
        cases = {"center": (0, 50, 200, 250), "top": (0, 0, 200, 200), "bottom": (0, 100, 200, 300),
                 "top-left": (0, 0, 200, 200), "left": (0, 50, 200, 250), "bottom-right": (0, 100, 200, 300)}
        for anchor, box in cases.items():
            with self.subTest(anchor=anchor):
                self.assertEqual(plan_resize(200, 300, st(ratio="1:1", crop_anchor=anchor)).crop_box, box)
        # 300x200 to 1:1 -> 200x200 box, horizontal placement varies.
        cases = {"center": (50, 0, 250, 200), "left": (0, 0, 200, 200), "right": (100, 0, 300, 200),
                 "top-left": (0, 0, 200, 200), "bottom-right": (100, 0, 300, 200), "top": (50, 0, 250, 200)}
        for anchor, box in cases.items():
            with self.subTest(anchor=anchor, wide=True):
                self.assertEqual(plan_resize(300, 200, st(ratio="1:1", crop_anchor=anchor)).crop_box, box)

    def test_crop_pixels_follow_box(self):
        img = Image.new("RGB", (300, 200), (0, 0, 0))
        img.paste((255, 0, 0), (0, 0, 100, 200))
        out = apply_state(img, st(ratio="1:1", crop_anchor="left"))
        self.assertEqual(out.size, (200, 200))
        self.assertEqual(out.getpixel((50, 100)), (255, 0, 0))
        self.assertEqual(out.getpixel((150, 100)), (0, 0, 0))

    def test_pad_centred_with_colour(self):
        img = Image.new("RGB", (100, 50), (10, 20, 30))
        s = st(ratio="1:1", ratio_action="pad", pad_color="#ff8000")
        plan = plan_resize(100, 50, s)
        self.assertEqual((plan.pad_size, plan.pad_offset, plan.crop_box), ((100, 100), (0, 25), None))
        out = apply_state(img, s)
        self.assertEqual(out.size, (100, 100))
        self.assertEqual(out.getpixel((50, 10)), (255, 128, 0))
        self.assertEqual(out.getpixel((50, 90)), (255, 128, 0))
        self.assertEqual(out.getpixel((50, 25)), (10, 20, 30))
        self.assertEqual(out.getpixel((50, 74)), (10, 20, 30))

    def test_pad_tall_image(self):
        plan = plan_resize(50, 100, st(ratio="1:1", ratio_action="pad"))
        self.assertEqual((plan.pad_size, plan.pad_offset), ((100, 100), (25, 0)))

    def test_pad_bad_colour_falls_back_to_black(self):
        out = apply_state(Image.new("RGB", (100, 50), (9, 9, 9)), st(ratio="1:1", ratio_action="pad", pad_color="nope"))
        self.assertEqual(out.getpixel((0, 0)), (0, 0, 0))

    def test_free_anchor_fraction(self):
        base = plan_resize(200, 300, st(ratio="1:1", crop_anchor="top-left")).crop_box
        self.assertEqual(plan_resize(200, 300, st(ratio="1:1", crop_anchor={"x": 0, "y": 0})).crop_box, base)
        bottom_right = plan_resize(200, 300, st(ratio="1:1", crop_anchor="bottom-right")).crop_box
        self.assertEqual(plan_resize(200, 300, st(ratio="1:1", crop_anchor={"x": 1, "y": 1})).crop_box,
                         bottom_right)
        self.assertEqual(plan_resize(200, 300, st(ratio="1:1", crop_anchor={"x": 0.5, "y": 0.25})).crop_box,
                         (0, 25, 200, 225))
        self.assertEqual(plan_resize(300, 200, st(ratio="1:1", crop_anchor={"x": 0.5, "y": 0.5})).crop_box,
                         (50, 0, 250, 200))
        # out of range clamps
        self.assertEqual(plan_resize(200, 300, st(ratio="1:1", crop_anchor={"x": -3, "y": 9})).crop_box,
                         (0, 100, 200, 300))
        # unusable fractions fall back to the centre
        self.assertEqual(plan_resize(200, 300, st(ratio="1:1", crop_anchor={"x": "a"})).crop_box,
                         (0, 50, 200, 250))

    def test_lanczos_only(self):
        seen = []
        real = Image.Image.resize

        def spy(self, size, resample=None, *a, **k):
            seen.append(resample)
            return real(self, size, resample, *a, **k)

        with mock.patch.object(Image.Image, "resize", spy):
            apply_state(Image.new("RGB", (400, 300)), st(mode="longest_side", longest_side=200))
            apply_state(Image.new("RGB", (400, 300)), st(snap=64))
        self.assertEqual(seen, [Image.LANCZOS, Image.LANCZOS])

    def test_plan_matches_apply(self):
        for size, over, expected in CASES:
            with self.subTest(size=size, over=over):
                s = st(**over)
                out = apply_state(Image.new("RGB", size), s)
                self.assertEqual(out.size, expected)
                self.assertEqual(plan_resize(size[0], size[1], s).out_size, out.size)

    def test_plan_changed_flag(self):
        self.assertFalse(plan_resize(10, 10, st()).changed)
        self.assertTrue(plan_resize(10, 20, st(ratio="1:1")).changed)
        self.assertTrue(plan_resize(10, 10, st(snap=64)).changed)
        self.assertFalse(plan_resize(64, 64, st(snap=64)).changed)

    def test_pad_output_is_rgb(self):
        out = apply_state(Image.new("RGB", (100, 50)), st(ratio="1:1", ratio_action="pad"))
        self.assertEqual(out.mode, "RGB")


class Presets(unittest.TestCase):
    def test_ratio_presets_merge(self):
        self.assertEqual(RATIO_PRESETS, ("", "1:1", "2:3", "3:2", "9:16", "16:9"))
        self.assertEqual(DIRECTOR_RATIOS, ("4:3", "3:4", "21:9"))
        self.assertEqual(ratio_presets(), ["", "1:1", "2:3", "3:2", "9:16", "16:9", "4:3", "3:4", "21:9"])
        self.assertEqual(ratio_presets(["1:1", "4:5", "5:4", "21:9", "19.5:9"]),
                         ["", "1:1", "2:3", "3:2", "9:16", "16:9", "4:3", "3:4", "21:9", "4:5", "5:4", "19.5:9"])


class Isolation(unittest.TestCase):
    def test_no_comfy_import(self):
        code = (f"import sys; sys.path.insert(0, {ROOT!r}); import luna_imaging.resize; "
                "assert 'torch' not in sys.modules and 'comfy_api' not in sys.modules "
                "and 'numpy' not in sys.modules and 'folder_paths' not in sys.modules")
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
