"""LLM Prompt (GGUF): characterization of `_LLMRunner.generate` media assembly, plus the
Director hooks `_pil_to_content`, `media_override` and `_RUNNER_LOCK`.

Director plan, task A2. `_load_model` and `_invoke` are replaced by recorders, so no test
loads a model, imports a chat handler or touches the GPU (`keep_model_loaded=True` keeps
`clear()` and its CUDA calls out of every run).
"""
import base64
import inspect
import io
import threading
import unittest
from unittest import mock

import _comfy

REPLY = "[POSITIVE]\nA\n[NEGATIVE]\nB"


def _tensor(h=8, w=8, n=1):
    import torch
    t = torch.arange(n * h * w * 3, dtype=torch.float32).reshape(n, h, w, 3)
    return t / float(t.max())


def _gen_kw(**over):
    kw = dict(model_name="test-model.gguf", system_prompt="None",
              custom_system_prompt="You write image prompts.", user_prompt="a red fox in snow",
              output_format="text", split_output=True, max_tokens=512, temperature=0.7,
              top_p=0.9, top_k=20, min_p=0.0, repetition_penalty=1.0, device="cuda",
              auto_settings=False, disable_thinking=True, keep_model_loaded=True, seed=0)
    kw.update(over)
    return kw


class GGUFWriterCoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _comfy.load("llm_prompt_node")

    def _runner(self, handler=True):
        """A fresh runner whose `_load_model` / `_invoke` only record their arguments."""
        runner = self.mod._LLMRunner()
        rec = {}

        def fake_load(model_name, device, **kw):
            rec["load"] = dict(model_name=model_name, device=device, **kw)
            runner.chat_handler = object() if handler else None

        def fake_invoke(**kw):
            rec["invoke"] = kw
            return REPLY

        runner._load_model = fake_load
        runner._invoke = fake_invoke
        return runner, rec

    def _generate(self, handler=True, **over):
        runner, rec = self._runner(handler)
        with mock.patch("builtins.print"):
            out = runner.generate(**_gen_kw(**over))
        return out, rec

    def _reference_jpeg_url(self, frame, max_mp=0.0):
        """The pre-change encoding of `_image_tensor_to_content`, written out by hand."""
        import numpy as np
        from PIL import Image
        arr = (frame.cpu().numpy() * 255).clip(0, 255).astype(np.uint8)
        pil = self.mod._downscale_pil_to_mp(Image.fromarray(arr, mode="RGB"), max_mp)
        buf = io.BytesIO()
        pil.save(buf, format="JPEG", quality=95)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")

    # ---- Characterization of today's media assembly ----------------------------------

    def test_no_override_unchanged(self):
        img = _tensor()
        out, rec = self._generate(image=img)
        media = rec["invoke"]["media_content"]
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["type"], "image_url")
        self.assertEqual(media[0]["image_url"]["url"], self._reference_jpeg_url(img[0]))
        self.assertTrue(rec["load"]["want_vision"])
        self.assertEqual(out, ("A", "B", ""))

    def test_image_and_reference_labelled_in_order(self):
        img, ref = _tensor(), _tensor(6, 10)
        _, rec = self._generate(image=img, reference_image=ref)
        media = rec["invoke"]["media_content"]
        self.assertEqual([m["type"] for m in media], ["text", "image_url", "text", "image_url"])
        self.assertEqual(media[0]["text"], "Input Image:")
        self.assertEqual(media[2]["text"], "Reference Image:")
        self.assertEqual(media[1]["image_url"]["url"], self._reference_jpeg_url(img[0]))
        self.assertEqual(media[3]["image_url"]["url"], self._reference_jpeg_url(ref[0]))

    def test_batch_image_every_frame_and_vision_mp(self):
        img = _tensor(40, 50, n=2)
        _, rec = self._generate(image=img, vision_mp=0.001)
        media = rec["invoke"]["media_content"]
        self.assertEqual(len(media), 2)
        for i in range(2):
            self.assertEqual(media[i]["image_url"]["url"], self._reference_jpeg_url(img[i], 0.001))

    def test_text_only_loads_without_vision(self):
        _, rec = self._generate()
        self.assertFalse(rec["load"]["want_vision"])
        self.assertEqual(rec["invoke"]["media_content"], [])
        self.assertEqual(rec["invoke"]["user_prompt"], "USER PROMPT:\na red fox in snow")

    def test_media_dropped_without_handler(self):
        _, rec = self._generate(handler=False, image=_tensor())
        self.assertTrue(rec["load"]["want_vision"])
        self.assertEqual(rec["invoke"]["media_content"], [])

    def test_canvas_from_image_dims(self):
        _, rec = self._generate(image=_tensor(6, 10))
        self.assertTrue(rec["invoke"]["user_prompt"].startswith("CANVAS FORMAT:"))
        self.assertIn("(10x6)", rec["invoke"]["user_prompt"])

    def test_image_tensor_to_content_matches_reference(self):
        img = _tensor(12, 20)
        for t in (img, img[0]):
            got = self.mod._image_tensor_to_content(t, max_mp=0.0)
            self.assertEqual(got, {"type": "image_url",
                                   "image_url": {"url": self._reference_jpeg_url(img[0])}})

    def test_execute_passes_through_generate(self):
        Node = self.mod.LLMPromptNode
        seen = {}

        def fake_generate(**kw):
            seen.update(kw)
            return ("p", "n", "l")
        with mock.patch.object(self.mod._RUNNER, "generate", fake_generate):
            out = Node.execute(**_gen_kw(unknown_extra=1))
        self.assertEqual(tuple(out.args), ("p", "n", "l"))
        self.assertNotIn("unknown_extra", seen)
        self.assertNotIn("media_override", seen)

    # ---- Director hooks -------------------------------------------------------------

    def test_media_override_used(self):
        from PIL import Image
        content = self.mod._pil_to_content(Image.new("RGB", (16, 12), (200, 30, 30)), 0.0)
        override = [{"type": "text", "text": "Image 1:"}, content]
        _, rec = self._generate(image=None, media_override=override)
        self.assertEqual(rec["invoke"]["media_content"], override)
        self.assertTrue(rec["load"]["want_vision"])

    def test_media_override_replaces_tensor_media(self):
        override = [{"type": "text", "text": "Image 1:"}]
        _, rec = self._generate(image=_tensor(), reference_image=_tensor(), media_override=override)
        self.assertEqual(rec["invoke"]["media_content"], override)

    def test_empty_override_is_text_only(self):
        _, rec = self._generate(image=_tensor(), media_override=[])
        self.assertEqual(rec["invoke"]["media_content"], [])
        self.assertFalse(rec["load"]["want_vision"])

    def test_media_override_is_last_parameter(self):
        params = list(inspect.signature(self.mod._LLMRunner.generate).parameters.values())
        self.assertEqual(params[-1].name, "media_override")
        self.assertIsNone(params[-1].default)

    def test_pil_to_content_jpeg(self):
        from PIL import Image
        got = self.mod._pil_to_content(Image.new("RGB", (32, 24), (10, 20, 30)), 0.0)
        self.assertEqual(got["type"], "image_url")
        self.assertTrue(got["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_pil_to_content_downscales(self):
        from PIL import Image
        got = self.mod._pil_to_content(Image.new("RGB", (200, 100)), 0.005)
        raw = base64.b64decode(got["image_url"]["url"].split(",", 1)[1])
        w, h = Image.open(io.BytesIO(raw)).size
        self.assertLessEqual(w * h, 5000)

    def test_pil_to_content_rgba(self):
        from PIL import Image
        got = self.mod._pil_to_content(Image.new("RGBA", (16, 16), (1, 2, 3, 128)), 0.0)
        self.assertTrue(got["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_execute_holds_lock(self):
        Node = self.mod.LLMPromptNode
        reached = threading.Event()

        def fake_generate(**kw):
            reached.set()
            return ("p", "n", "l")
        lock = self.mod._RUNNER_LOCK
        with mock.patch.object(self.mod._RUNNER, "generate", fake_generate):
            lock.acquire()
            try:
                t = threading.Thread(target=Node.execute, kwargs=_gen_kw(), daemon=True)
                t.start()
                self.assertFalse(reached.wait(0.2), "execute reached generate while the lock was held")
            finally:
                lock.release()
            self.assertTrue(reached.wait(5.0), "execute never reached generate after release")
            t.join(5.0)
        self.assertFalse(lock.locked())


if __name__ == "__main__":
    unittest.main()
