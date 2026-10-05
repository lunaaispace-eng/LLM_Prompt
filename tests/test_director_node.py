"""Luna Director launcher: schema and execute (history result or the input image).

Loaded through tests/_comfy.py. Skips when comfy_api is not importable.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _comfy

from PIL import Image


def _color_tensor(torch, color, size=(6, 8)):
    h, w = size
    r, g, b = color
    image = torch.zeros(1, h, w, 3)
    image[..., 0] = r / 255
    image[..., 1] = g / 255
    image[..., 2] = b / 255
    return image


class DirectorNodeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _comfy.load("luna_director_node")
        cls.Node = cls.mod.LunaDirectorLauncher
        import torch
        cls.torch = torch

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.dirs = {"output": base / "output", "input": base / "input", "temp": base / "temp"}
        for d in self.dirs.values():
            d.mkdir()
        store_mod = _comfy.load("luna_director.store")
        self.store_mod = store_mod
        self.store = store_mod.Store(self.dirs["output"] / "luna_director",
                                     self.dirs["input"] / "luna_director")
        import folder_paths
        self._fp = folder_paths
        self._saved = (folder_paths.get_output_directory, folder_paths.get_input_directory,
                       folder_paths.get_temp_directory)
        folder_paths.get_output_directory = lambda: str(self.dirs["output"])
        folder_paths.get_input_directory = lambda: str(self.dirs["input"])
        folder_paths.get_temp_directory = lambda: str(self.dirs["temp"])

    def tearDown(self):
        self._fp.get_output_directory, self._fp.get_input_directory, self._fp.get_temp_directory = self._saved
        self._tmp.cleanup()

    def _ui_images(self, out):
        ui = out.ui.as_dict() if hasattr(out.ui, "as_dict") else out.ui
        self.assertIsInstance(ui, dict)
        return ui["images"]

    def _pixel(self, item):
        path = os.path.join(self.dirs["temp"], item.get("subfolder") or "", item["filename"])
        with Image.open(path) as im:
            return im.convert("RGB").getpixel((0, 0))

    def test_schema_validates(self):
        schema = self.Node.define_schema()
        schema.validate()
        self.assertEqual(schema.node_id, "LunaDirectorLauncher")
        self.assertEqual(schema.display_name, "Luna Director (Studio)")
        self.assertEqual(schema.category, "Luna/LLM")
        self.assertTrue(schema.is_output_node)
        self.assertIn("Open Studio", schema.description)
        self.assertEqual([i.id for i in schema.inputs], ["image", "director_state"])
        image, state = schema.inputs
        self.assertTrue(image.optional)
        self.assertFalse(state.optional)
        self.assertEqual([o.id for o in schema.outputs], ["image", "mask", "prompt", "info"])
        self.assertTrue(all(o.tooltip for o in schema.outputs))
        self.assertTrue(all(i.tooltip for i in schema.inputs))

    def test_execute_passthrough_without_selection(self):
        image = _color_tensor(self.torch, (10, 20, 30))
        state = json.dumps({"project": "proj", "selected": None, "prompt": "keep me"})
        out = self.Node.execute(image=image, director_state=state)
        got, mask, prompt, info = out.result
        self.assertTrue(self.torch.equal(got, image))
        self.assertEqual(tuple(mask.shape), (1, 6, 8))
        self.assertEqual(float(mask.max()), 0.0)
        self.assertEqual(prompt, "keep me")
        self.assertEqual(info, "")
        images = self._ui_images(out)
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["type"], "temp")
        self.assertEqual(self._pixel(images[0]), (10, 20, 30))

    def _entry(self, color, prompt, project="proj"):
        img = Image.new("RGB", (8, 6), color)
        ref = self.store.save_png(project, img, "result")
        return self.store.add_history(project, {
            "status": "done", "prompt": prompt, "model": "gpt-image-2", "operation": "edit",
            "outputs": [ref], "cost_usd": 0.05,
        })

    def test_execute_loads_selected(self):
        entry = self._entry((1, 2, 200), "from history")
        image = _color_tensor(self.torch, (10, 20, 30))
        state = json.dumps({"project": "proj", "selected": entry["id"], "prompt": "widget prompt"})
        out = self.Node.execute(image=image, director_state=state)
        got, mask, prompt, info = out.result
        self.assertEqual(tuple(got.shape), (1, 6, 8, 3))
        self.assertAlmostEqual(float(got[0, 0, 0, 0]), 1 / 255, places=5)
        self.assertAlmostEqual(float(got[0, 0, 0, 2]), 200 / 255, places=5)
        self.assertEqual(tuple(mask.shape), (1, 6, 8))
        self.assertEqual(float(mask.max()), 0.0)
        self.assertEqual(prompt, "from history")
        self.assertIn("status : done", info)
        images = self._ui_images(out)
        self.assertEqual(images[0]["type"], "temp")
        self.assertEqual(self._pixel(images[0]), (10, 20, 30))

    def test_execute_loads_selected_older_than_list_limit(self):
        old = self._entry((9, 8, 7), "old prompt")
        for i in range(200):
            self.store.add_history("proj", {"status": "done", "prompt": f"new-{i}"})
        listed = [e["id"] for e in self.store.list_history("proj", limit=200)]
        self.assertNotIn(old["id"], listed)
        self.assertIsNotNone(self.store.get_entry("proj", old["id"]))
        state = json.dumps({"project": "proj", "selected": old["id"], "prompt": ""})
        image = _color_tensor(self.torch, (10, 20, 30))
        with mock.patch.object(self.store_mod.Store, "list_history",
                                side_effect=AssertionError("execute must use get_entry")):
            out = self.Node.execute(image=image, director_state=state)
        got, _mask, prompt, info = out.result
        self.assertAlmostEqual(float(got[0, 0, 0, 0]), 9 / 255, places=5)
        self.assertAlmostEqual(float(got[0, 0, 0, 2]), 7 / 255, places=5)
        self.assertEqual(prompt, "old prompt")
        self.assertIn("status : done", info)
        self.assertEqual(self._pixel(self._ui_images(out)[0]), (10, 20, 30))


if __name__ == "__main__":
    unittest.main()
