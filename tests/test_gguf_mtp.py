"""LLM Prompt (GGUF): the GGUF header reader and the MTP gate in `_load_model`.

Headers are synthesized on disk (metadata only, no tensors); `Llama` is replaced by a
recorder, so no test loads a model or touches the GPU.
"""
import os
import shutil
import struct
import tempfile
import unittest
from unittest import mock

import _comfy


def _gguf(path, kv):
    """Write a metadata-only GGUF v3 header. kv: list of (key, value) with str/int/list[str]."""
    def s(x):
        b = x.encode()
        return struct.pack("<Q", len(b)) + b

    out = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + struct.pack("<Q", len(kv))
    for k, v in kv:
        out += s(k)
        if isinstance(v, str):
            out += struct.pack("<I", 8) + s(v)
        elif isinstance(v, list):
            out += struct.pack("<I", 9) + struct.pack("<I", 8) + struct.pack("<Q", len(v))
            out += b"".join(s(x) for x in v)
        else:
            out += struct.pack("<I", 4) + struct.pack("<I", v)
    with open(path, "wb") as f:
        f.write(out)


class GgufHeaderTests(unittest.TestCase):
    def setUp(self):
        self.node = _comfy.load("llm_prompt_node")
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.node._GGUF_MTP_CACHE.clear()
        self.node._GGUF_ARCH_CACHE.clear()

    def _file(self, name, kv):
        p = os.path.join(self.dir, name)
        _gguf(p, kv)
        return p

    def test_mtp_layers_read_after_arrays(self):
        p = self._file("mtp.gguf", [("general.architecture", "qwen35"),
                                    ("tokenizer.ggml.tokens", ["a", "bb", "ccc"]),
                                    ("qwen35.nextn_predict_layers", 1)])
        self.assertEqual(self.node._read_gguf_arch(p), "qwen35")
        self.assertEqual(self.node._gguf_mtp_layers(p, "qwen35"), 1)

    def test_no_mtp_key_is_zero(self):
        p = self._file("plain.gguf", [("general.architecture", "qwen35"),
                                      ("qwen35.block_count", 64)])
        self.assertEqual(self.node._gguf_mtp_layers(p, "qwen35"), 0)

    def test_unreadable_header_is_zero(self):
        p = os.path.join(self.dir, "bad.gguf")
        with open(p, "wb") as f:
            f.write(b"NOPE")
        self.assertEqual(self.node._gguf_mtp_layers(p, "qwen35"), 0)
        self.assertEqual(self.node._read_gguf_arch(p), "")


class DrafterLookupTests(unittest.TestCase):
    """Gemma 4 drafter GGUFs are hidden from the model list."""

    def setUp(self):
        from pathlib import Path
        self.node = _comfy.load("llm_prompt_node")
        self.node._GGUF_ARCH_CACHE.clear()
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "models").mkdir()
        (self.dir / "_mtp_drafters").mkdir()
        self.target = self.dir / "models" / "gemma-12b-uncensored.gguf"
        _gguf(self.target, [("general.architecture", "gemma4"), ("gemma4.embedding_length", 3840)])
        _gguf(self.dir / "_mtp_drafters" / "mtp-gemma-4-26B-A4B-it.gguf",
              [("general.architecture", "gemma4-assistant"), ("gemma4-assistant.embedding_length_out", 2816)])
        self.d12 = self.dir / "_mtp_drafters" / "mtp-gemma-4-12b-it.gguf"
        _gguf(self.d12, [("general.architecture", "gemma4-assistant"),
                         ("gemma4-assistant.embedding_length_out", 3840)])

    def test_drafter_is_not_a_model(self):
        self.assertTrue(self.node._is_mtp_drafter(self.d12))
        self.assertFalse(self.node._is_mtp_drafter(self.target))


class MtpGateTests(unittest.TestCase):
    """`_load_model` passes `speculative` only when every MTP condition holds."""

    def setUp(self):
        self.node = _comfy.load("llm_prompt_node")
        self.node._GGUF_MTP_CACHE.clear()
        self.node._GGUF_ARCH_CACHE.clear()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.mtp = os.path.join(self.dir, "m-mtp.gguf")
        _gguf(self.mtp, [("general.architecture", "qwen35"), ("qwen35.nextn_predict_layers", 1)])
        self.plain = os.path.join(self.dir, "m-plain.gguf")
        _gguf(self.plain, [("general.architecture", "qwen35")])
        self.calls = []

        def fake_llama(**kw):
            self.calls.append(kw)
            return mock.MagicMock()

        fake_llama.__init__ = lambda self, **kw: None  # _filter_kwargs_for_callable reads it
        self.patches = [
            mock.patch.object(self.node, "Llama", mock.MagicMock(side_effect=fake_llama)),
            mock.patch.object(self.node, "_pick_device", return_value="cuda"),
            mock.patch.object(self.node, "_filter_kwargs_for_callable", side_effect=lambda f, kw: kw),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _load(self, path, mmproj=None, mtp=2, **kw):
        from pathlib import Path
        runner = self.node._LLMRunner()
        with mock.patch.object(self.node, "_resolve_model_paths",
                               return_value=(Path(path), Path(mmproj) if mmproj else None)), \
             mock.patch.object(self.node.torch.cuda, "is_available", return_value=False):
            runner._load_model("m", "auto", n_ctx=4096, want_vision=bool(mmproj),
                               mtp_draft_tokens=mtp, **kw)
        return self.calls[-1], runner

    def test_on_with_heads_text_only(self):
        kw, runner = self._load(self.mtp)
        spec = kw["speculative"]
        self.assertEqual(spec.spec_type, self.node.SpeculativeType.DRAFT_MTP)
        self.assertEqual(spec.draft_n_max, 2)
        self.assertEqual(runner.current_signature["mtp_draft"], 2)

    def test_off_when_zero(self):
        kw, runner = self._load(self.mtp, mtp=0)
        self.assertNotIn("speculative", kw)
        self.assertEqual(runner.current_signature["mtp_draft"], 0)

    def test_skipped_without_heads(self):
        kw, _ = self._load(self.plain)
        self.assertNotIn("speculative", kw)

    def test_skipped_with_projector(self):
        mm = os.path.join(self.dir, "mmproj.gguf")
        _gguf(mm, [("general.architecture", "clip")])
        with mock.patch.object(self.node, "_build_vision_handler", return_value=mock.MagicMock()):
            kw, _ = self._load(self.mtp, mmproj=mm)
        self.assertNotIn("speculative", kw)

    def test_skipped_on_old_wheel(self):
        with mock.patch.object(self.node, "SpecConfig", None):
            kw, _ = self._load(self.mtp)
        self.assertNotIn("speculative", kw)


if __name__ == "__main__":
    unittest.main()
