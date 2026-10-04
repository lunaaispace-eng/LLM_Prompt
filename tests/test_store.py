import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import Image  # noqa: E402

from luna_director import store as store_mod  # noqa: E402
from luna_director.store import (  # noqa: E402
    ENTRY_KEYS, RefError, ResizeStateError, Store, project_slug, resolve_ref, validate_resize_item,
)
from luna_imaging.resize import DEFAULT_ITEM  # noqa: E402

GENERATE_KEYS = ("batch", "variant", "tier", "exact_text", "sections")


def png_bytes(size=(8, 6), color=(10, 20, 30), mode="RGB", fmt="PNG"):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, fmt)
    return buf.getvalue()


class _Clock:
    """Injected clock: each call returns the next of the given aware datetimes (the last repeats)."""

    def __init__(self, *times):
        self.times = list(times)

    def __call__(self):
        return self.times.pop(0) if len(self.times) > 1 else self.times[0]


class StoreBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.dirs = {"output": base / "output", "input": base / "input", "temp": base / "temp"}
        for d in self.dirs.values():
            d.mkdir()
        self.store = Store(self.dirs["output"] / "luna_director", self.dirs["input"] / "luna_director")

    def tearDown(self):
        self._tmp.cleanup()

    def add(self, project="proj", **kw):
        entry = {"status": "done", "prompt": "p"}
        entry.update(kw)
        return self.store.add_history(project, entry)


class History(StoreBase):
    def test_add_and_list_newest_first(self):
        a = self.add(prompt="first")
        b = self.add(prompt="second")
        c = self.add(prompt="third")
        self.assertEqual(len(a["id"]), 32)
        int(a["id"], 16)
        self.assertNotEqual(a["id"], b["id"])
        datetime.fromisoformat(a["ts"])
        self.assertEqual(a["project"], "proj")
        self.assertEqual(set(a), set(ENTRY_KEYS))
        self.assertEqual([e["id"] for e in self.store.list_history("proj")], [c["id"], b["id"], a["id"]])
        self.assertEqual([e["prompt"] for e in self.store.list_history("proj", limit=2)], ["third", "second"])
        self.assertEqual(self.store.list_history("other"), [])
        self.assertIsNone(a["package"])
        self.assertEqual((a["star"], a["note"], a["hidden"]), (False, "", False))

    def test_entry_keys_fixed(self):
        self.assertEqual(ENTRY_KEYS, (
            "id", "parent", "project", "ts", "engine", "model", "package", "operation", "request", "writer",
            "prompt", "negative", "seed", "params", "inputs", "outputs", "mode", "est_cost_usd", "cost_usd",
            "seconds", "status", "error", "star", "note", "hidden",
            "batch", "variant", "tier", "exact_text", "sections"))
        with self.assertRaises(ValueError):
            self.add(colour="red")
        with self.assertRaises(ValueError):
            self.add(status="running")
        with self.assertRaises(ValueError):
            self.add(tier="medium")

    def test_writer_and_params_round_trip(self):
        writer = {"provider": "Gemini", "model": "gemini-2.5-flash", "preset": "Edit Rewrite - Gemini",
                  "thinking": True, "negative_on": False, "positive": "pos", "negative": ""}
        params = {"quality": "high", "aspect_ratio": "1:1", "resolution": "1K", "background": "auto", "n": 1,
                  "mask_mode": "crop", "crop_padding": 32, "feather_px": 8, "outpaint": None}
        e = self.add(writer=writer, params=params)
        got = self.store.get_entry("proj", e["id"])
        self.assertEqual(got["params"], params)
        self.assertEqual(got["writer"], dict(writer, feedback="", variants_mode=None))
        self.assertIsNone(self.add()["writer"])

    def test_generate_keys_default_null_and_round_trip(self):
        edit = self.add(operation="inpaint")
        for k in GENERATE_KEYS:
            self.assertIsNone(edit[k], k)
            self.assertIsNone(self.store.get_entry("proj", edit["id"])[k], k)
        sections = {"subject": "a fox", "style": "ink", "composition": "centred", "lighting": "dusk",
                    "camera": "50mm"}
        gen = self.add(operation="generate", batch="b1", variant=2, tier="draft", exact_text='OPEN "24/7"',
                       sections=sections,
                       writer={"provider": "Gemini", "feedback": "warmer", "variants_mode": "varied"})
        got = self.store.get_entry("proj", gen["id"])
        self.assertEqual((got["batch"], got["variant"], got["tier"], got["exact_text"], got["sections"]),
                         ("b1", 2, "draft", 'OPEN "24/7"', sections))
        self.assertEqual((got["writer"]["feedback"], got["writer"]["variants_mode"]), ("warmer", "varied"))
        child = self.add(operation="edit", parent=gen["id"], batch="b1", variant=2, tier="final")
        self.assertEqual(self.store.get_entry("proj", child["id"])["parent"], gen["id"])
        for k in GENERATE_KEYS:
            with self.assertRaises(ValueError):
                self.store.update_history("proj", gen["id"], {k: None})
        self.assertEqual(self.store.get_entry("proj", gen["id"])["sections"], sections)

    def test_cancelled_entry_keeps_outputs(self):
        out = [{"name": "x.png", "subfolder": "luna_director/proj", "type": "output"}]
        e = self.add(status="cancelled", outputs=out, cost_usd=0.04)
        got = self.store.list_history("proj")[0]
        self.assertEqual(got["id"], e["id"])
        self.assertEqual(got["status"], "cancelled")
        self.assertEqual(got["outputs"], out)
        self.assertEqual(got["cost_usd"], 0.04)

    def test_patch_folds(self):
        e = self.add()
        self.store.update_history("proj", e["id"], {"star": True})
        r = self.store.update_history("proj", e["id"], {"note": "keep this", "hidden": True})
        self.assertEqual((r["star"], r["note"], r["hidden"]), (True, "keep this", True))
        self.store.update_history("proj", e["id"], {"star": False})
        got = self.store.list_history("proj")[0]
        self.assertEqual((got["star"], got["note"], got["hidden"]), (False, "keep this", True))
        self.assertEqual(got["prompt"], "p")
        lines = (self.store.root / "proj" / "history.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 4)  # one add line, three appended patch lines

    def test_patch_rejects_other_keys(self):
        e = self.add()
        for patch in ({"prompt": "x"}, {"cost_usd": 0}, {"star": True, "status": "error"}, {"id": "y"}):
            with self.assertRaises(ValueError):
                self.store.update_history("proj", e["id"], patch)
        for patch in ({"star": "yes"}, {"hidden": 1}, {"note": 5}):
            with self.assertRaises(ValueError):
                self.store.update_history("proj", e["id"], patch)
        with self.assertRaises(KeyError):
            self.store.update_history("proj", "0" * 32, {"star": True})
        got = self.store.get_entry("proj", e["id"])
        self.assertEqual((got["prompt"], got["star"], got["status"]), ("p", False, "done"))

    def test_get_entry_beyond_list_limit(self):
        old = self.add(prompt="old")
        for i in range(5):
            self.add(prompt=f"n{i}")
        self.store.update_history("proj", old["id"], {"star": True})
        self.assertNotIn(old["id"], [e["id"] for e in self.store.list_history("proj", limit=3)])
        got = self.store.get_entry("proj", old["id"])
        self.assertEqual((got["prompt"], got["star"]), ("old", True))
        self.assertIsNone(self.store.get_entry("proj", "f" * 32))
        self.assertIsNone(self.store.get_entry("nothing-here", old["id"]))

    def test_day_cost_sums_actual_only(self):
        local = datetime(2026, 10, 4, 23, 30).astimezone()
        self.store = Store(self.store.root, self.store.assets_root, clock=_Clock(local))
        self.add("proj-a", cost_usd=0.25, est_cost_usd=9.0)
        self.add("proj-a", status="cancelled", cost_usd=0.10)
        self.add("proj-a", est_cost_usd=5.0)                      # no actual cost: not counted
        self.add("proj-a", status="error", cost_usd=None)
        self.add("proj-b", cost_usd=0.05)                         # another project counts
        self.store = Store(self.store.root, self.store.assets_root, clock=_Clock(local + timedelta(days=1)))
        self.add("proj-b", cost_usd=1.0)                          # the next local day
        self.assertAlmostEqual(self.store.day_cost("2026-10-04"), 0.40)
        self.assertAlmostEqual(self.store.day_cost("2026-10-05"), 1.0)
        self.assertEqual(self.store.day_cost("2026-10-03"), 0.0)

    def test_day_is_local_date_of_ts(self):
        # An entry stamped in another offset counts on the local date of its instant.
        e = self.add(cost_usd=0.5)
        path = self.store.root / "proj" / "history.jsonl"
        line = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        instant = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
        line["entry"]["ts"] = instant.astimezone(timezone(timedelta(hours=-11))).isoformat()
        path.write_text(json.dumps(line) + "\n", encoding="utf-8")
        local_day = instant.astimezone().date().isoformat()
        self.assertAlmostEqual(self.store.day_cost(local_day), 0.5)
        self.assertEqual(self.store.get_entry("proj", e["id"])["cost_usd"], 0.5)

    def test_ledger_survives_partial_last_line(self):
        a = self.add(prompt="a")
        b = self.add(prompt="b")
        path = self.store.root / "proj" / "history.jsonl"
        with open(path, "ab") as f:
            f.write(b'{"op": "add", "entry": {"id": "trunc')
        self.assertEqual([e["id"] for e in self.store.list_history("proj")], [b["id"], a["id"]])
        c = self.add(prompt="c")   # an append after a torn line starts on a fresh line
        self.assertEqual([e["id"] for e in self.store.list_history("proj")], [c["id"], b["id"], a["id"]])
        self.store.update_history("proj", a["id"], {"star": True})
        self.assertTrue(self.store.get_entry("proj", a["id"])["star"])

    def test_project_names(self):
        self.assertEqual(project_slug("My Project: Ünïcode!!  v2"), "my-project-unicode-v2")
        self.assertEqual(project_slug("../../etc"), "etc")
        s = project_slug("x" * 100)
        self.assertEqual(len(s), 48)
        self.assertRegex(project_slug("--A  b--"), r"^[a-z0-9-]+$")
        self.assertEqual(project_slug("--A  b--"), "a-b")
        self.assertEqual(project_slug("!!!"), "default")
        self.assertIs(Store.project_slug, project_slug)
        for bad in ("../x", "A", "a b", "", "x" * 49):
            with self.assertRaises(RefError):
                self.store.add_history(bad, {"status": "done"})
            with self.assertRaises(RefError):
                self.store.list_history(bad)


class Refs(StoreBase):
    def test_resolve_ref_rejects_traversal(self):
        good = resolve_ref({"name": "a.png", "subfolder": "luna_director/proj", "type": "input"}, self.dirs)
        self.assertEqual(good, (self.dirs["input"] / "luna_director" / "proj" / "a.png").resolve())
        self.assertEqual(resolve_ref({"name": "a.png", "type": "output"}, self.dirs),
                         (self.dirs["output"] / "a.png").resolve())
        self.assertIs(Store.resolve_ref, resolve_ref)
        bad = [
            {"name": "a.png", "subfolder": "../..", "type": "input"},
            {"name": "..\\x.png", "subfolder": "", "type": "input"},
            {"name": "../x.png", "subfolder": "", "type": "input"},
            {"name": "x.png", "subfolder": "luna_director/../../..", "type": "output"},
            {"name": os.path.abspath(os.sep + "x.png"), "subfolder": "", "type": "input"},
            {"name": "", "subfolder": "", "type": "input"},
            {"name": "a.png", "subfolder": "", "type": "secret"},
            {"name": "a.png", "subfolder": "", "type": None},
            {"name": None, "subfolder": "", "type": "input"},
            {"subfolder": "", "type": "input"},
            "a.png",
        ]
        for ref in bad:
            with self.subTest(ref=ref):
                with self.assertRaises(RefError):
                    resolve_ref(ref, self.dirs)

    def test_save_png(self):
        ref = self.store.save_png("proj", Image.new("RGB", (4, 3), (1, 2, 3)), "abc")
        self.assertEqual(ref, {"name": "abc.png", "subfolder": "luna_director/proj", "type": "output"})
        path = resolve_ref(ref, self.dirs)
        with Image.open(path) as im:
            self.assertEqual((im.format, im.size, im.getpixel((0, 0))), ("PNG", (4, 3), (1, 2, 3)))
        again = self.store.save_png("proj", Image.new("RGB", (4, 3)), "abc")   # never overwrites
        self.assertNotEqual(again["name"], "abc.png")
        self.assertTrue(resolve_ref(again, self.dirs).is_file())
        odd = self.store.save_png("proj", Image.new("RGB", (2, 2)), "../../evil name")
        self.assertNotIn("/", odd["name"])
        self.assertNotIn("\\", odd["name"])
        self.assertTrue(resolve_ref(odd, self.dirs).is_file())

    def test_import_asset_dedups_by_content(self):
        data = png_bytes()
        r1 = self.store.import_asset("proj", data)
        r2 = self.store.import_asset("proj", data)
        self.assertEqual(r1, r2)
        self.assertEqual(r1["subfolder"], "luna_director/proj")
        self.assertEqual(r1["type"], "input")
        self.assertRegex(r1["name"], r"^[0-9a-f]{16}\.png$")
        folder = self.dirs["input"] / "luna_director" / "proj"
        self.assertEqual(len(list(folder.iterdir())), 1)
        self.assertEqual(resolve_ref(r1, self.dirs).read_bytes(), data)
        r3 = self.store.import_asset("proj", png_bytes(color=(200, 0, 0)))
        self.assertNotEqual(r1["name"], r3["name"])
        self.assertEqual(len(list(folder.iterdir())), 2)
        jpg = self.store.import_asset("proj", png_bytes(fmt="JPEG"))
        self.assertTrue(jpg["name"].endswith(".jpg"))
        webp = self.store.import_asset("proj", png_bytes(fmt="WEBP"))
        self.assertTrue(webp["name"].endswith(".webp"))

    def test_import_asset_rejects_non_image(self):
        for data in (b"", b"hello world", b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, png_bytes()[:40], None):
            with self.subTest(data=data[:12] if data else data):
                with self.assertRaises(RefError) as cm:
                    self.store.import_asset("proj", data)
                self.assertEqual(str(cm.exception), "not an image")
        folder = self.dirs["input"] / "luna_director" / "proj"
        self.assertEqual(list(folder.iterdir()) if folder.exists() else [], [])


class Resize(StoreBase):
    def _asset(self, size=(400, 200), mode="RGBA", color=(10, 200, 30, 128)):
        return self.store.import_asset("proj", png_bytes(size, color, mode))

    def item(self, **kw):
        it = dict(DEFAULT_ITEM)
        it.update(kw)
        return it

    def test_resize_asset_dry_run_writes_nothing(self):
        ref = self._asset()
        folder = self.dirs["input"] / "luna_director" / "proj"
        before = sorted(folder.iterdir())
        plan = self.store.resize_asset("proj", ref, self.item(ratio="1:1"), self.dirs, dry_run=True)
        self.assertEqual(plan, {"in": [400, 200], "out": [200, 200], "crop_box": [100, 0, 300, 200],
                                "pad_size": None, "changed": True})
        self.assertEqual(sorted(folder.iterdir()), before)

    def test_resize_asset_saves_rgb_copy_keeps_original(self):
        ref = self._asset()
        res = self.store.resize_asset("proj", ref, self.item(mode="longest_side", longest_side=100), self.dirs)
        self.assertEqual((res["in"], res["out"], res["changed"]), ([400, 200], [100, 50], True))
        self.assertNotEqual(res["ref"], ref)
        self.assertEqual(res["ref"]["subfolder"], "luna_director/proj")
        with Image.open(resolve_ref(res["ref"], self.dirs)) as im:
            self.assertEqual((im.mode, im.size), ("RGB", (100, 50)))
        with Image.open(resolve_ref(ref, self.dirs)) as im:
            self.assertEqual((im.mode, im.size), ("RGBA", (400, 200)))
        again = self.store.resize_asset("proj", ref, self.item(mode="longest_side", longest_side=100), self.dirs)
        self.assertEqual(again["ref"], res["ref"])  # the same result is the same asset

    def test_resize_asset_unchanged_returns_own_ref(self):
        ref = self._asset()
        res = self.store.resize_asset("proj", ref, self.item(), self.dirs)
        self.assertEqual((res["changed"], res["ref"], res["out"]), (False, ref, [400, 200]))

    def test_resize_asset_honours_exif_orientation(self):
        buf = io.BytesIO()
        exif = Image.Exif()
        exif[0x0112] = 6   # rotate 90: the stored 40x20 shows as 20x40
        Image.new("RGB", (40, 20), (5, 5, 5)).save(buf, "JPEG", exif=exif.tobytes())
        ref = self.store.import_asset("proj", buf.getvalue())
        plan = self.store.resize_asset("proj", ref, self.item(snap=8), self.dirs, dry_run=True)
        self.assertEqual(plan["in"], [20, 40])
        res = self.store.resize_asset("proj", ref, self.item(ratio="1:1"), self.dirs)
        with Image.open(resolve_ref(res["ref"], self.dirs)) as im:
            self.assertEqual(im.size, (20, 20))

    def test_resize_asset_bad_ref(self):
        with self.assertRaises(RefError):
            self.store.resize_asset("proj", {"name": "../x.png", "subfolder": "", "type": "input"},
                                    self.item(), self.dirs)
        with self.assertRaises(RefError):
            self.store.resize_asset("proj", {"name": "missing.png", "subfolder": "", "type": "input"},
                                    self.item(), self.dirs)

    def test_validate_resize_item_refuses_bad_values(self):
        validate_resize_item(self.item())
        validate_resize_item(self.item(mode="max_mp", max_mp=0.5, pad_color="#A0b1C2", snap=64,
                                       crop_anchor={"x": 0.2, "y": 1}, ratio="19.5:9", ratio_action="pad",
                                       allow_upscale=True, longest_side=2048.0, scale_factor=0.25))
        bad = [
            ("mode", "huge"), ("mode", None),
            ("pad_color", "nope"), ("pad_color", "#fff"), ("pad_color", "red"), ("pad_color", 0),
            ("max_mp", "abc"), ("max_mp", 0), ("max_mp", -1), ("max_mp", float("nan")), ("max_mp", True),
            ("longest_side", "abc"), ("longest_side", 0), ("longest_side", float("inf")),
            ("scale_factor", "2"), ("scale_factor", -0.5),
            ("snap", -8), ("snap", 1.5), ("snap", "64"), ("snap", True),
            ("ratio", 5), ("ratio_action", "stretch"), ("allow_upscale", "yes"),
            ("crop_anchor", 3), ("crop_anchor", {"x": "a", "y": 0}),
        ]
        for key, value in bad:
            with self.subTest(key=key, value=value):
                with self.assertRaises(ResizeStateError) as cm:
                    validate_resize_item(self.item(**{key: value}))
                self.assertIn(key, str(cm.exception))
                self.assertIsInstance(cm.exception, ValueError)
        ref = self._asset()
        with self.assertRaises(ResizeStateError):
            self.store.resize_asset("proj", ref, self.item(pad_color="nope", ratio="1:1", ratio_action="pad"),
                                    self.dirs)


class Guard(unittest.TestCase):
    def test_no_comfy_import(self):
        code = (f"import sys; sys.path.insert(0, {ROOT!r}); import luna_director.store; "
                "bad = [m for m in ('torch', 'comfy', 'comfy_api', 'server', 'folder_paths', 'nodes', "
                "'numpy', 'aiohttp') if m in sys.modules]; assert not bad, bad")
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_module_exports(self):
        for name in ("Store", "RefError", "ResizeStateError", "project_slug", "resolve_ref", "ENTRY_KEYS",
                     "validate_resize_item", "open_rgb"):
            self.assertTrue(hasattr(store_mod, name), name)


if __name__ == "__main__":
    unittest.main()
