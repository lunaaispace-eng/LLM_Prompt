"""Luna Image Studio (API Key) node: schema, model union, tensor <-> PIL wiring.

Runs with the ComfyUI python. The pack is loaded as a stub package so its heavy
__init__.py (llama-cpp etc.) never runs; skipped when comfy_api is not importable.
"""
import importlib
import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = r"E:\ComfyUI-Easy-Install\ComfyUI"
for p in (ROOT, COMFY):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    import comfy_api.latest  # noqa: F401
    import torch
    _HAVE_COMFY = True
except Exception:  # pragma: no cover - plain python without ComfyUI
    _HAVE_COMFY = False

PKG = "llm_prompt_pack"


def _load_node():
    if PKG not in sys.modules:
        pkg = types.ModuleType(PKG)
        pkg.__path__ = [ROOT]
        sys.modules[PKG] = pkg
    return importlib.import_module(f"{PKG}.luna_image_studio_node")


def _base_kwargs(**over):
    kw = dict(prompt="make it red", model="gpt-image-2.5-flare", operation="edit",
              aspect_ratio="auto", resolution="1K", batch_count=1, seed=0)
    kw.update(over)
    return kw


@unittest.skipUnless(_HAVE_COMFY, "comfy_api not importable (run with the ComfyUI python)")
class StudioNodeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_node()
        cls.Node = cls.mod.LunaImageStudio

    def _run(self, fake, **kw):
        with mock.patch(f"{PKG}.luna_imaging.studio.run", side_effect=fake), \
             mock.patch.object(self.mod, "_resolve_api_key", return_value="sk-test"):
            return self.Node.execute(**_base_kwargs(**kw))

    def test_schema_validates(self):
        schema = self.Node.define_schema()
        schema.validate()
        self.assertEqual(schema.node_id, "LunaImageStudio")
        self.assertEqual(schema.display_name, "Luna Image Studio (API Key)")
        self.assertEqual(schema.category, "Luna/LLM")
        names = [i.id for i in schema.inputs]
        self.assertEqual(names, [
            "prompt", "model", "operation", "aspect_ratio", "resolution", "batch_count", "seed",
            "reference_images", "mask", "invert_mask", "bboxes", "mask_mode", "crop_padding",
            "feather", "outpaint_left", "outpaint_top", "outpaint_right", "outpaint_bottom",
            "quality", "background", "width", "height", "timeout", "max_retries"])
        self.assertEqual([o.id for o in schema.outputs], ["image", "mask", "text", "info"])

    def test_model_union_contains_all(self):
        models = self.mod.MODELS
        for m in ("gpt-image-2.5-flare", "gemini-3.1-flash-image", "grok-imagine-image-2.0"):
            self.assertIn(m, models)
        self.assertEqual(len(models), len(set(models)))
        self.assertEqual(models[0], "gpt-image-2.5-flare")

    def test_execute_inpaint_wiring(self):
        from luna_imaging.masks import fit_mask  # top-level import is fine for the fake
        from PIL import Image
        seen = {}

        def fake(req, key):
            seen["req"], seen["key"] = req, key
            mod_types = sys.modules[f"{PKG}.luna_imaging.types"]
            used = fit_mask(req.mask, req.images[0].size)
            return mod_types.EditResult(images=[req.images[0].convert("RGBA")], text="t",
                                        cost_usd=0.0123, info=["mode : crop"]), used

        image = torch.rand(1, 32, 32, 3)
        mask = torch.zeros(1, 16, 16)
        mask[:, 8:, 8:] = 1.0  # bottom-right quarter
        out = self._run(fake, operation="inpaint", reference_images={"reference_image_1": image},
                        mask=mask, bboxes="[[0,0,0.5,0.5]]")
        img_t, mask_t, text, info = out.result
        self.assertEqual(tuple(img_t.shape), (1, 32, 32, 3))
        self.assertEqual(tuple(mask_t.shape[-2:]), (32, 32))
        self.assertEqual(text, "t")
        self.assertIn("cost : $0.0123", info)
        self.assertIn("mode : crop", info)
        self.assertIn("inpaint", info)

        req = seen["req"]
        self.assertEqual(seen["key"], "sk-test")
        self.assertEqual(req.provider, "openai")
        self.assertEqual(req.operation, "inpaint")
        self.assertNotIn("seed", req.extra)
        self.assertEqual(req.images[0].size, (32, 32))
        self.assertEqual(req.mask.mode, "L")
        # union: bbox top-left quarter + MASK bottom-right quarter
        self.assertEqual(req.mask.getpixel((4, 4)), 255)
        self.assertEqual(req.mask.getpixel((28, 28)), 255)
        self.assertEqual(req.mask.getpixel((28, 4)), 0)
        self.assertAlmostEqual(float(mask_t[0, 4, 4]), 1.0)
        self.assertAlmostEqual(float(mask_t[0, 4, 28]), 0.0)
        # echoed image survives the round trip
        self.assertLess(float((img_t - image).abs().max()), 1 / 255 + 1e-6)
        self.assertIsInstance(req.images[0], Image.Image)

    def test_widgets_map_to_request(self):
        seen = {}

        def fake(req, key):
            seen["req"] = req
            t = sys.modules[f"{PKG}.luna_imaging.types"]
            return t.EditResult(images=[req.images[0].convert("RGBA")]), None

        a, b = torch.rand(2, 8, 8, 3), torch.rand(1, 8, 8, 3)
        out = self._run(fake, operation="outpaint",
                        reference_images={"reference_image_2": b, "reference_image_1": a},
                        mask_mode="crop", crop_padding=0.5, feather=4, outpaint_left=1,
                        outpaint_top=2, outpaint_right=3, outpaint_bottom=4, quality="high",
                        background="opaque", batch_count=3, width=64, height=48,
                        timeout=100, max_retries=1)
        req = seen["req"]
        self.assertEqual(len(req.images), 3)  # slot 1 batch of 2 first, then slot 2
        self.assertEqual(req.outpaint, (1, 2, 3, 4))
        self.assertEqual((req.mask_mode, req.crop_padding, req.feather_px), ("crop", 0.5, 4))
        self.assertEqual((req.quality, req.background, req.n), ("high", "opaque", 3))
        self.assertEqual((req.width, req.height, req.timeout, req.max_retries), (64, 48, 100.0, 1))
        self.assertIsNone(req.mask)
        mask_t = out.result[1]
        self.assertEqual(tuple(mask_t.shape), (1, 8, 8))
        self.assertEqual(float(mask_t.max()), 0.0)  # no mask used -> zeros

    def test_transparent_outputs_alpha(self):
        from PIL import Image

        def fake(req, key):
            t = sys.modules[f"{PKG}.luna_imaging.types"]
            im = Image.new("RGBA", (16, 12), (255, 0, 0, 0))
            im.paste((255, 0, 0, 255), (0, 0, 8, 12))
            return t.EditResult(images=[im]), None

        out = self._run(fake, operation="generate", background="transparent")
        img_t, mask_t = out.result[0], out.result[1]
        self.assertEqual(tuple(img_t.shape), (1, 12, 16, 3))
        self.assertAlmostEqual(float(mask_t[0, 0, 0]), 1.0)
        self.assertAlmostEqual(float(mask_t[0, 0, 15]), 0.0)

    def test_bad_bboxes_raise(self):
        image = torch.rand(1, 8, 8, 3)
        for bad in ("not json", "[1,2,3,4]", "[[1,2,3]]", '[["a",0,1,1]]'):
            with self.assertRaisesRegex(ValueError, r"\[\[x, y, w, h\]"):
                self._run(lambda r, k: None, operation="inpaint",
                          reference_images={"reference_image_1": image}, bboxes=bad)

    def test_missing_key_names_env_var(self):
        with mock.patch.object(self.mod, "_resolve_api_key", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY"):
                self.Node.execute(**_base_kwargs(model="gemini-3.1-flash-image", operation="generate"))


if __name__ == "__main__":
    unittest.main()
