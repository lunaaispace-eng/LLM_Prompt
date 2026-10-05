"""Director modules inside the pack: every `luna_imaging` import resolves to the pack's own copy.

ComfyUI loads the pack as a package, so a top-level `luna_imaging` import fails there (or, with the
checkout on sys.path as in tests/_comfy.py, silently loads a second copy). Loading through `_comfy`
and checking each imported name's module catches both.
"""
import types
import unittest

import _comfy

PACK_CORE = f"{_comfy.PKG}.luna_imaging"


class DirectorImportsInsidePack(unittest.TestCase):
    def _assert_pack_core(self, obj):
        name = obj.__name__ if isinstance(obj, types.ModuleType) else obj.__module__
        self.assertTrue(name.startswith(PACK_CORE), f"{name} not from {PACK_CORE}")

    def test_store_uses_pack_core(self):
        store = _comfy.load("luna_director.store")
        for obj in (store.plan_resize, store.apply_state):
            self._assert_pack_core(obj)

    def test_jobs_uses_pack_core(self):
        jobs = _comfy.load("luna_director.jobs")
        for obj in (jobs.studio, jobs._gemini, jobs._openai, jobs._xai,
                    jobs.caps_for, jobs.gemini_cost, jobs.openai_size, jobs.EditRequest, jobs.ProviderError):
            self._assert_pack_core(obj)

    def test_writer_uses_pack_core(self):
        writer = _comfy.load("luna_director.writer")
        for obj in (writer._studio, writer.expand_box, writer.fit_mask):
            self._assert_pack_core(obj)


if __name__ == "__main__":
    unittest.main()
