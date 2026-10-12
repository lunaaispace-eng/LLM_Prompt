"""LLM Prompt (API): Gemini model check and list filtering. No network: urlopen is faked."""
import contextlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import _comfy


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(table):
    """table: model -> served model name, or an int HTTP status."""
    def urlopen(req, timeout=0):
        model = req.full_url.split("/models/")[1].split(":")[0]
        res = table[model]
        if isinstance(res, int):
            raise urllib.error.HTTPError(req.full_url, res, "x", {},
                                         io.BytesIO(json.dumps({"error": {"message": "gone"}}).encode()))
        return _Resp(json.dumps({"modelVersion": res}).encode())
    return urlopen


class ApiModelsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _comfy.load("llm_prompt_api_node")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(self.mod, "_MODEL_CHECK_FILE", Path(self.tmp.name) / "check.json")
        p.start()
        self.addCleanup(p.stop)
        self.mod._MODEL_LIST_CACHE_TS.clear()

    def _check(self, table, force=True):
        with mock.patch.object(self.mod.urllib.request, "urlopen", _fake_urlopen(table)), \
             mock.patch.object(self.mod, "_resolve_api_key", return_value="k"), \
             contextlib.redirect_stdout(io.StringIO()):
            return self.mod._check_gemini_models(list(table), force=force)

    def test_probe_classifies(self):
        self._check({"gemini-2.5-flash": 404, "gemini-3.7-flash": "gemini-3.8-flash",
                     "gemini-3.8-flash": "gemini-3.8-flash", "gemini-flash-latest": "gemini-3.8-flash",
                     "gemini-3.6-flash": 500})
        data = self.mod._check_load()
        self.assertEqual(data["gemini-2.5-flash"]["status"], "dead")
        self.assertEqual(data["gemini-3.7-flash"]["status"], "routed")
        self.assertEqual(data["gemini-3.7-flash"]["served"], "gemini-3.8-flash")
        self.assertEqual(data["gemini-3.8-flash"]["status"], "ok")
        self.assertEqual(data["gemini-flash-latest"]["status"], "ok")  # aliases are meant to move
        self.assertNotIn("gemini-3.6-flash", data)  # a 500 says nothing about the model

    def test_fresh_checks_are_not_repeated(self):
        self._check({"gemini-3.8-flash": "gemini-3.8-flash"})
        self.assertEqual(self._check({"gemini-3.8-flash": 404}, force=False), 0)
        self.assertEqual(self.mod._check_load()["gemini-3.8-flash"]["status"], "ok")

    def test_list_hides_dead_routed_and_image_models(self):
        self._check({"gemini-2.5-flash": 404, "gemini-3.7-flash": "gemini-3.8-flash",
                     "gemini-3.8-flash": "gemini-3.8-flash"})
        live = ["gemini-2.5-flash", "gemini-3.7-flash", "gemini-3.8-flash",
                "gemini-nano-banana-2.1", "gemma-4-31b-it"]
        with mock.patch.object(self.mod, "_fetch_models_from_server", return_value=live), \
             mock.patch.object(self.mod, "_resolve_api_key", return_value="k"):
            models = self.mod._get_models_for_provider("Gemini")
        self.assertEqual(models, ["gemini-3.8-flash", "gemma-4-31b-it"])

    def test_served_model_marks_rerouted(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.mod._note_served_model("gemini-3.5-flash", "gemini-3.6-flash")
            self.mod._note_served_model("gemini-flash-latest", "gemini-3.8-flash")
            self.mod._note_served_model("gemini-3.8-flash", "gemini-3.8-flash")
            self.mod._note_served_model("gemma-4-31b-it", "gemma-4-31b-it-0925")
        data = self.mod._check_load()
        self.assertEqual(data["gemini-3.5-flash"]["status"], "routed")
        self.assertEqual(set(data), {"gemini-3.5-flash"})


if __name__ == "__main__":
    unittest.main()
