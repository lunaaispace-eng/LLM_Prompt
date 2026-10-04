"""A5: cloud job runner (luna_director/jobs.py). Providers are faked: `luna_imaging.studio.run` is patched, so
no network and no paid call."""
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import Image  # noqa: E402

from luna_director import jobs  # noqa: E402
from luna_director.store import ENTRY_KEYS, Store, resolve_ref  # noqa: E402
from luna_imaging import studio  # noqa: E402
from luna_imaging.capabilities import ModelCaps, caps_for  # noqa: E402
from luna_imaging.cost import gemini_cost, openai_estimate  # noqa: E402
from luna_imaging.http import ProviderError  # noqa: E402
from luna_imaging.sizes import openai_size  # noqa: E402
from luna_imaging.types import EditResult  # noqa: E402

KEY = "sk-FAKE-KEY-0123456789"
GREEN = (0, 255, 0, 255)
GPT = "gpt-image-2.5"
GEMINI = "gemini-3.1-flash-image"
GROK = "grok-imagine-image"
PROJECT = "demo"
SID = "sid-1"
WAIT = 10


class FakeRun:
    """Stands in for studio.run: records (req, key), returns a green 8x8 image at $0.01. `gate` makes calls
    block until released; `error` raises instead; `info` is appended to the result's info lines."""

    def __init__(self, gate=False, error=None, info=None):
        self.calls = []
        self.lock = threading.Lock()
        self.started = threading.Event()
        self.release = threading.Event()
        if not gate:
            self.release.set()
        self.error = error
        self.info = list(info or [])

    def __call__(self, req, key):
        with self.lock:
            self.calls.append((req, key))
        self.started.set()
        self.release.wait(WAIT)
        if self.error is not None:
            raise self.error
        img = Image.new("RGBA", (8, 8), GREEN)
        return EditResult(images=[img] * max(1, int(req.n)), cost_usd=0.01, info=list(self.info)), None


class Harness(unittest.TestCase):
    max_workers = 3

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.dirs = {"output": base / "output", "input": base / "input"}
        self.store = Store(self.dirs["output"] / "luna_director", self.dirs["input"] / "luna_director")
        self.events = []
        self._ev_lock = threading.Lock()
        self.fake = FakeRun()
        patcher = mock.patch.object(studio, "run", side_effect=lambda req, key: self.fake(req, key))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runner = jobs.JobRunner(self._emit, self.store, lambda provider: KEY, self._load,
                                     max_workers=self.max_workers)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.runner.close)

    def _emit(self, event, data, sid):
        with self._ev_lock:
            self.events.append((event, json.loads(json.dumps(data)), sid))

    def _load(self, ref):
        with Image.open(resolve_ref(ref, self.dirs)) as im:
            im.load()
            return im.copy()

    def asset(self, color=(10, 20, 30), size=(8, 8)):
        import io
        buf = io.BytesIO()
        Image.new("RGB", size, color).save(buf, "PNG")
        return self.store.import_asset(PROJECT, buf.getvalue())

    def mask(self):
        import io
        m = Image.new("L", (8, 8), 0)
        m.paste(255, (2, 2, 6, 6))
        buf = io.BytesIO()
        m.save(buf, "PNG")
        return self.store.import_asset(PROJECT, buf.getvalue())

    def finish(self):
        self.runner.wait(WAIT)

    def states(self, job_id):
        return [d["state"] for e, d, s in self.events if d["job_id"] == job_id]

    def last(self, job_id):
        return [d for e, d, s in self.events if d["job_id"] == job_id][-1]

    def spec(self, **kw):
        s = {"model": GPT, "operation": "generate", "prompt": "a lighthouse at dusk", "negative": "",
             "request": "lighthouse", "writer": None, "image": None, "mask": None, "refs": [], "parent": None,
             "aspect_ratio": "1:1", "resolution": "1K", "quality": "low", "background": "auto",
             "mask_mode": "auto", "crop_padding": 0.25, "feather_px": 16, "outpaint": None, "n": 1,
             "seed": 42}
        s.update(kw)
        return s

    def batch(self, **kw):
        b = {"models": [GPT], "count": 1, "variants_mode": "same",
             "prompts": [{"prompt": "a lighthouse at dusk", "negative": "", "sections": None}],
             "tier": "draft", "refs": [], "exact_text": "", "request": "lighthouse", "writer": None,
             "parent": None, "aspect_ratio": "3:2", "resolution": "1K", "quality": "medium",
             "background": "auto", "seed": 7}
        b.update(kw)
        return b

    def sent(self):
        """The requests studio.run received, in call order."""
        return [req for req, key in self.fake.calls]

    def entry_of(self, job_id):
        return self.last(job_id).get("entry")


class CloudJobTests(Harness):
    def test_events_order(self):
        out = self.runner.submit_cloud(SID, PROJECT, self.spec())
        self.finish()
        self.assertEqual(self.states(out["job_id"]), ["queued", "running", "done"])
        for event, data, sid in self.events:
            self.assertEqual(event, "luna.job")
            self.assertEqual(sid, SID)
        self.assertIn("est_cost_usd", out)

    def test_entry_written_with_cost_and_mode(self):
        out = self.runner.submit_cloud(SID, PROJECT, self.spec())
        self.finish()
        entry = self.entry_of(out["job_id"])
        self.assertEqual(entry["status"], "done")
        self.assertEqual(entry["cost_usd"], 0.01)
        self.assertEqual(entry["mode"], "whole")
        self.assertEqual(entry["engine"], "cloud")
        self.assertEqual(entry["model"], GPT)
        self.assertEqual(self.store.get_entry(PROJECT, entry["id"])["id"], entry["id"])
        saved = resolve_ref(entry["outputs"][0], self.dirs)
        self.assertTrue(saved.is_file())

        self.fake.info = ["size : 8x8", "mode : crop"]
        out = self.runner.submit_cloud(SID, PROJECT, self.spec(
            operation="inpaint", image=self.asset(), mask=self.mask(), prompt="red door"))
        self.finish()
        self.assertEqual(self.entry_of(out["job_id"])["mode"], "crop")
        self.assertEqual(self.entry_of(out["job_id"])["inputs"][0]["type"], "input")

    def test_mode_from_info(self):
        self.assertEqual(jobs.mode_from_info(["x", "mode : native"]), "native")
        self.assertEqual(jobs.mode_from_info(["mode : crop"]), "crop")
        self.assertEqual(jobs.mode_from_info(["size : 1024x1024"]), "whole")
        self.assertEqual(jobs.mode_from_info([]), "whole")

    def test_entry_has_params_writer_and_parent(self):
        writer = {"provider": "Gemini", "model": "gemini-3-flash", "preset": "Edit Rewrite - GPT Image",
                  "thinking": False, "negative_on": False, "positive": "p", "negative": "n"}
        out = self.runner.submit_cloud(SID, PROJECT, self.spec(writer=writer, parent="abc123", quality="high",
                                                               aspect_ratio="16:9", seed=9))
        self.finish()
        entry = self.entry_of(out["job_id"])
        self.assertEqual(entry["parent"], "abc123")
        self.assertEqual(entry["writer"]["preset"], "Edit Rewrite - GPT Image")
        self.assertEqual(entry["writer"]["provider"], "Gemini")
        self.assertEqual(entry["params"]["quality"], "high")
        self.assertEqual(entry["params"]["aspect_ratio"], "16:9")
        self.assertEqual(entry["params"]["n"], 1)
        for k in ("resolution", "background", "mask_mode", "crop_padding", "feather_px", "outpaint"):
            self.assertIn(k, entry["params"])
        self.assertEqual(entry["seed"], 9)
        self.assertEqual(entry["request"], "lighthouse")
        self.assertIsNone(entry["batch"])
        self.assertIsNone(entry["tier"])

    def test_build_entry_keys_match_store(self):
        entry = jobs.build_entry(self.spec(), status="done", outputs=[], cost_usd=0.01, est_cost_usd=None,
                                 seconds=1.0, mode="whole", error=None)
        self.assertEqual(set(entry) | {"id", "ts", "project"}, set(ENTRY_KEYS))
        self.assertTrue({"id", "ts", "project"}.isdisjoint(entry))

    def test_negative_appended_as_avoid(self):
        on = {"negative_on": True}
        self.runner.submit_cloud(SID, PROJECT, self.spec(negative="blur, text", writer=on))
        self.finish()
        self.assertEqual(self.sent()[-1].prompt, "a lighthouse at dusk\nAvoid: blur, text")
        out = self.runner.submit_cloud(SID, PROJECT, self.spec(negative="blur, text", writer={"negative_on": False}))
        self.finish()
        self.assertEqual(self.sent()[-1].prompt, "a lighthouse at dusk")
        entry = self.entry_of(out["job_id"])
        self.assertEqual(entry["prompt"], "a lighthouse at dusk")
        self.assertEqual(entry["negative"], "blur, text")
        self.runner.submit_cloud(SID, PROJECT, self.spec(negative="  ", writer=on))
        self.finish()
        self.assertEqual(self.sent()[-1].prompt, "a lighthouse at dusk")

    def test_error_named(self):
        self.fake.error = ValueError("mask is empty")
        out = self.runner.submit_cloud(SID, PROJECT, self.spec())
        self.finish()
        self.assertEqual(self.states(out["job_id"]), ["queued", "running", "error"])
        self.assertEqual(self.last(out["job_id"])["error"], {"code": "refused", "message": "mask is empty"})
        self.fake.error = ProviderError("HTTP 400: bad prompt", status=400)
        out = self.runner.submit_cloud(SID, PROJECT, self.spec())
        self.finish()
        self.assertEqual(self.last(out["job_id"])["error"]["code"], "provider")

    def test_bad_spec_refused_at_submit(self):
        with self.assertRaises(ValueError):
            self.runner.submit_cloud(SID, PROJECT, self.spec(model="dall-e-9"))
        with self.assertRaises(ValueError):
            self.runner.submit_cloud(SID, PROJECT, {**self.spec(), "api_key": "x"})
        self.assertEqual(self.events, [])

    def test_no_key_is_named(self):
        def no_key(provider):
            raise RuntimeError("No OpenAI API key found. Set OPENAI_API_KEY")
        runner = jobs.JobRunner(self._emit, self.store, no_key, self._load)
        self.addCleanup(runner.close)
        out = runner.submit_cloud(SID, PROJECT, self.spec())
        runner.wait(WAIT)
        self.assertEqual(self.last(out["job_id"])["error"]["code"], "no_key")
        self.assertEqual(self.fake.calls, [])

    def test_key_never_in_events(self):
        self.runner.submit_cloud(SID, PROJECT, self.spec())
        self.finish()
        self.fake.error = ProviderError(f"HTTP 401: Incorrect API key provided: {KEY}", status=401)
        out = self.runner.submit_cloud(SID, PROJECT, self.spec())
        self.finish()
        self.assertTrue(all(key == KEY for req, key in self.fake.calls))
        self.assertNotIn(KEY, json.dumps(self.events))
        self.assertNotIn(KEY, json.dumps(self.runner.jobs_for(SID)))
        self.assertEqual(self.last(out["job_id"])["state"], "error")
        ledger = (self.dirs["output"] / "luna_director" / PROJECT / "history.jsonl").read_text("utf-8")
        self.assertNotIn(KEY, ledger)

    def test_ledger_refusal_writes_fallback_entry(self):
        real = self.store.add_history
        calls = []

        def refuse_first(project, entry):
            calls.append(entry)
            if len(calls) == 1:
                raise ValueError("history entry is not plain JSON: boom")
            return real(project, entry)

        self.store.add_history = refuse_first
        with self.assertLogs("luna_director.jobs", "WARNING"):
            out = self.runner.submit_cloud(SID, PROJECT, self.spec())
            self.finish()
        entry = self.entry_of(out["job_id"])
        self.assertEqual(entry["status"], "done")
        self.assertEqual(entry["cost_usd"], 0.01)
        self.assertTrue(entry["error"].startswith("ledger refused: "))
        self.assertTrue(resolve_ref(entry["outputs"][0], self.dirs).is_file())
        self.assertEqual(set(calls[1]), {"status", "outputs", "cost_usd", "error"})
        self.assertAlmostEqual(self.store.day_cost(date.today().isoformat()), 0.01)


class CancelTests(Harness):
    max_workers = 1

    def test_cancel_before_start_never_calls_run(self):
        self.fake = FakeRun(gate=True)
        first = self.runner.submit_cloud(SID, PROJECT, self.spec(prompt="first"))
        self.assertTrue(self.fake.started.wait(WAIT))
        second = self.runner.submit_cloud(SID, PROJECT, self.spec(prompt="second"))
        self.assertTrue(self.runner.cancel(second["job_id"]))
        self.fake.release.set()
        self.finish()
        self.assertEqual([r.prompt for r in self.sent()], ["first"])
        self.assertEqual(self.states(second["job_id"]), ["queued", "cancelled"])
        self.assertNotIn("entry", self.last(second["job_id"]))
        self.assertEqual(len(self.store.list_history(PROJECT)), 1)
        self.assertFalse(self.runner.cancel(second["job_id"]))
        self.assertFalse(self.runner.cancel("no-such-job"))

    def test_cancel_while_running_keeps_image_and_cost(self):
        self.fake = FakeRun(gate=True)
        out = self.runner.submit_cloud(SID, PROJECT, self.spec())
        self.assertTrue(self.fake.started.wait(WAIT))
        self.assertTrue(self.runner.cancel(out["job_id"]))
        self.fake.release.set()
        self.finish()
        self.assertEqual(self.states(out["job_id"]), ["queued", "running", "cancelled"])
        entry = self.entry_of(out["job_id"])
        self.assertEqual(entry["status"], "cancelled")
        self.assertEqual(entry["cost_usd"], 0.01)
        path = resolve_ref(entry["outputs"][0], self.dirs)
        self.assertTrue(path.is_file())
        with Image.open(path) as im:
            self.assertEqual(im.convert("RGBA").getpixel((0, 0)), GREEN)
        self.assertEqual(self.store.get_entry(PROJECT, entry["id"])["status"], "cancelled")
        self.assertAlmostEqual(self.store.day_cost(date.today().isoformat()), 0.01)

    def test_cancel_while_running_then_error_is_error_entry(self):
        self.fake = FakeRun(gate=True, error=ProviderError("HTTP 500: upstream", status=500))
        out = self.runner.submit_cloud(SID, PROJECT, self.spec())
        self.assertTrue(self.fake.started.wait(WAIT))
        self.assertTrue(self.runner.cancel(out["job_id"]))
        self.fake.release.set()
        self.finish()
        self.assertEqual(self.states(out["job_id"]), ["queued", "running", "error"])
        stored = self.store.list_history(PROJECT)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["status"], "error")
        self.assertEqual(stored[0]["outputs"], [])
        self.assertIsNone(stored[0]["cost_usd"])

    def test_cancel_batch(self):
        self.fake = FakeRun(gate=True)
        out = self.runner.submit_batch(SID, PROJECT, self.batch(count=3))
        self.assertTrue(self.fake.started.wait(WAIT))
        self.assertEqual(self.runner.cancel_batch(out["batch_id"]), 3)
        self.fake.release.set()
        self.finish()
        self.assertEqual(len(self.fake.calls), 1)
        ids = [j["job_id"] for j in out["jobs"]]
        self.assertEqual(self.states(ids[0]), ["queued", "running", "cancelled"])
        kept = self.entry_of(ids[0])
        self.assertEqual(kept["cost_usd"], 0.01)
        self.assertTrue(resolve_ref(kept["outputs"][0], self.dirs).is_file())
        for jid in ids[1:]:
            self.assertEqual(self.states(jid), ["queued", "cancelled"])
        self.assertEqual(self.runner.cancel_batch(out["batch_id"]), 0)
        self.assertEqual(self.runner.cancel_batch("nope"), 0)

    def test_jobs_for_sid_reports_states(self):
        self.fake = FakeRun(gate=True)
        done = self.runner.submit_cloud(SID, PROJECT, self.spec(prompt="one"))
        self.assertTrue(self.fake.started.wait(WAIT))
        self.fake.started.clear()
        self.fake.release.set()
        self.runner.wait(WAIT)
        self.fake.release.clear()
        running = self.runner.submit_cloud(SID, PROJECT, self.spec(prompt="two"))
        self.assertTrue(self.fake.started.wait(WAIT))
        self.fake.started.clear()
        other = self.runner.submit_cloud("sid-2", PROJECT, self.spec(prompt="three"))
        listed = self.runner.jobs_for(SID)
        self.assertEqual([j["job_id"] for j in listed], [done["job_id"], running["job_id"]])
        self.assertEqual([j["state"] for j in listed], ["done", "running"])
        self.assertEqual(listed[0]["entry"]["status"], "done")
        self.assertNotIn("entry", listed[1])
        self.assertNotIn(other["job_id"], json.dumps(listed))
        self.assertNotIn(KEY, json.dumps(listed))
        self.fake.release.set()
        self.finish()


class GenerateTests(Harness):
    def test_auto_operation(self):
        self.assertEqual(jobs.auto_operation(GPT, 0), "generate")
        self.assertEqual(jobs.auto_operation(GPT, 2), "compose")
        self.assertEqual(jobs.auto_operation(GROK, 1), "compose")
        none = ModelCaps("gemini", GEMINI, 0, False, False, False)
        with mock.patch.object(jobs, "caps_for", side_effect=lambda m: none if m == GEMINI else caps_for(m)):
            self.assertEqual(jobs.auto_operation(GEMINI, 3), "generate")
            out = self.runner.submit_batch(SID, PROJECT, self.batch(models=[GEMINI, GPT], refs=[self.asset()]))
            self.finish()
        by_model = {req.model: req for req in self.sent()}
        self.assertEqual(by_model[GEMINI].operation, "generate")
        self.assertEqual(by_model[GEMINI].images, [])
        self.assertEqual(by_model[GPT].operation, "compose")
        self.assertEqual(len(by_model[GPT].images), 1)
        gem_job = next(j for j in out["jobs"] if j["model"] == GEMINI)
        self.assertEqual(self.entry_of(gem_job["job_id"])["inputs"], [])

    def test_ref_limit(self):
        self.assertEqual(jobs.ref_limit(GROK, "generate"), 0)
        self.assertEqual(jobs.ref_limit(GROK, "compose"), 5)
        self.assertEqual(jobs.ref_limit(GROK, "edit"), 4)
        self.assertEqual(jobs.ref_limit(GROK, "inpaint"), 4)

    def test_batch_expands_models_times_count(self):
        models = [GPT, GEMINI, GROK]
        out = self.runner.submit_batch(SID, PROJECT, self.batch(models=models, count=4))
        self.finish()
        self.assertEqual(len(out["jobs"]), 12)
        self.assertEqual(len(self.sent()), 12)
        self.assertTrue(all(req.n == 1 for req in self.sent()))
        for m in models:
            self.assertEqual(sorted(j["variant"] for j in out["jobs"] if j["model"] == m), [1, 2, 3, 4])
        batch_ids = set()
        for j in out["jobs"]:
            entry = self.entry_of(j["job_id"])
            batch_ids.add(entry["batch"])
            self.assertEqual(entry["variant"], j["variant"])
            self.assertEqual(entry["model"], j["model"])
            self.assertEqual(entry["tier"], "draft")
        self.assertEqual(batch_ids, {out["batch_id"]})

    def test_batch_varied_one_prompt_per_variant(self):
        prompts = [{"prompt": f"prompt {i}", "negative": "", "sections": {"subject": f"s{i}"}} for i in (1, 2, 3)]
        out = self.runner.submit_batch(SID, PROJECT, self.batch(models=[GPT, GEMINI], count=3,
                                                                variants_mode="varied", prompts=prompts))
        self.finish()
        for j in out["jobs"]:
            entry = self.entry_of(j["job_id"])
            self.assertEqual(entry["prompt"], f"prompt {j['variant']}")
            self.assertEqual(entry["sections"], {"subject": f"s{j['variant']}"})
        sent = sorted((r.model, r.prompt) for r in self.sent())
        self.assertEqual(sent, sorted((m, f"prompt {i}") for m in (GPT, GEMINI) for i in (1, 2, 3)))

    def test_batch_same_one_prompt(self):
        self.runner.submit_batch(SID, PROJECT, self.batch(models=[GPT, GROK], count=3))
        self.finish()
        self.assertEqual({r.prompt for r in self.sent()}, {"a lighthouse at dusk"})
        self.assertEqual(len(self.sent()), 6)

    def test_batch_prompt_count_mismatch_refused(self):
        prompts = [{"prompt": "a", "negative": "", "sections": None}, {"prompt": "b", "negative": "", "sections": None}]
        with self.assertRaises(ValueError):
            self.runner.submit_batch(SID, PROJECT, self.batch(count=3, variants_mode="varied", prompts=prompts))
        with self.assertRaises(ValueError):
            self.runner.submit_batch(SID, PROJECT, self.batch(count=2, variants_mode="same", prompts=prompts))
        with self.assertRaises(ValueError):
            self.runner.submit_batch(SID, PROJECT, self.batch(models=[]))
        with self.assertRaises(ValueError):
            self.runner.submit_batch(SID, PROJECT, self.batch(models=[GPT, GEMINI, GROK, GPT + "-flare", GEMINI]))
        with self.assertRaises(ValueError):
            jobs.estimate_batch(self.batch(count=3, variants_mode="varied", prompts=prompts))
        self.assertEqual(self.events, [])

    def test_batch_refs_cut_per_model_limit(self):
        refs = [self.asset((i, i, i)) for i in range(6)]
        models = [GROK, "grok-imagine-image-pro", GEMINI]
        self.runner.submit_batch(SID, PROJECT, self.batch(models=models, refs=refs))
        self.finish()
        by_model = {req.model: req for req in self.sent()}
        self.assertEqual(len(by_model[GROK].images), 5)
        self.assertEqual(len(by_model["grok-imagine-image-pro"].images), 3)
        self.assertEqual(len(by_model[GEMINI].images), 6)
        self.assertEqual(by_model[GROK].images[0].getpixel((0, 0)), (0, 0, 0))

    def test_estimate_batch_sum_equals_per_model(self):
        b = self.batch(models=[GPT, GEMINI, GROK], count=3, quality="medium")
        est = jobs.estimate_batch(b)
        per = {p["model"]: p["est_cost_usd"] for p in est["per_model"]}
        self.assertEqual(est["unknown"], [GROK])
        self.assertIsNone(per[GROK])
        self.assertAlmostEqual(est["total"], sum(v for v in per.values() if v is not None))
        for model in (GPT, GEMINI):
            spec = jobs.batch_job_spec(b, model, 1)
            self.assertAlmostEqual(per[model], 3 * jobs.estimate_cloud_cost(spec))
        self.assertGreater(per[GPT], 0)
        self.assertAlmostEqual(per[GEMINI], 3 * gemini_cost(GEMINI, "0.5K", 1))   # draft: cheapest size
        out = self.runner.submit_batch(SID, PROJECT, b)
        self.assertAlmostEqual(out["est_cost_usd"], est["total"])
        self.assertEqual(out["unknown"], [GROK])
        self.finish()

    def test_estimate_openai_and_gemini_known_xai_none(self):
        spec = self.spec(model=GPT, quality="low", aspect_ratio="1:1", resolution="1K")
        self.assertAlmostEqual(jobs.estimate_cloud_cost(spec), openai_estimate(GPT, "low", "1024x1024"))
        spec = self.spec(model=GPT, quality="high", aspect_ratio="16:9", resolution="2K", n=2)
        size = openai_size(GPT, "16:9", "2K", 0, 0, [])
        self.assertAlmostEqual(jobs.estimate_cloud_cost(spec), 2 * openai_estimate(GPT, "high", size))
        self.assertIsNone(jobs.estimate_cloud_cost(self.spec(model=GPT, quality="auto")))
        self.assertIsNone(jobs.estimate_cloud_cost(self.spec(model="gpt-image-1.5", quality="low")))
        spec = self.spec(model=GEMINI, resolution="2K", n=2)
        self.assertAlmostEqual(jobs.estimate_cloud_cost(spec), gemini_cost(GEMINI, "2K", 2))
        self.assertAlmostEqual(jobs.estimate_cloud_cost(self.spec(model=GEMINI, resolution="auto")),
                               gemini_cost(GEMINI, "1K", 1))
        self.assertIsNone(jobs.estimate_cloud_cost(self.spec(model=GROK)))
        out = self.runner.submit_cloud(SID, PROJECT, self.spec(model=GROK))
        self.assertIsNone(out["est_cost_usd"])
        self.finish()

    def test_draft_params_cheapest(self):
        gpt = jobs.draft_params(GPT, {"quality": "high", "resolution": "4K", "aspect_ratio": "16:9"})
        opts = jobs.options_for(GPT)
        self.assertEqual(gpt["quality"], "low")
        self.assertEqual(gpt["quality"], opts["quality"][0])
        self.assertEqual(gpt["resolution"], "1K")
        self.assertEqual(gpt["aspect_ratio"], "16:9")
        gem = jobs.draft_params(GEMINI, {"resolution": "4K"})
        self.assertEqual(gem["resolution"], "0.5K")
        self.assertIn("0.5K", jobs.options_for(GEMINI)["resolution"])
        self.assertEqual(jobs.draft_params("gemini-3-pro-image", {"resolution": "4K"})["resolution"], "1K")
        grok = jobs.draft_params(GROK, {"quality": "medium", "resolution": "2K"})
        self.assertEqual((grok["quality"], grok["resolution"]), ("low", "1K"))
        self.assertEqual(jobs.draft_params("grok-imagine-image-pro", {"quality": "medium"})["quality"], "auto")

    def test_final_options(self):
        self.assertEqual(jobs.final_options(GPT), ["high", "max"])
        self.assertEqual(jobs.final_options("gpt-image-2"), ["high"])
        self.assertEqual(jobs.final_options(GEMINI), ["2K", "4K"])
        self.assertEqual(jobs.final_options(GROK), ["2K"])

    def _draft(self, model, refs=(), **kw):
        on = {"negative_on": True, "provider": "Gemini"}
        b = self.batch(models=[model], refs=list(refs), exact_text="SEA WATCH 1887", writer=on,
                       prompts=[{"prompt": "a lighthouse at dusk", "negative": "blur", "sections": {"subject": "x"}}],
                       **kw)
        out = self.runner.submit_batch(SID, PROJECT, b)
        self.finish()
        return self.entry_of(out["jobs"][0]["job_id"])

    def _final(self, draft, choice):
        n_before = len(self.sent())
        out = self.runner.submit_final(SID, PROJECT, draft["id"], choice)
        self.finish()
        self.assertEqual(len(self.sent()), n_before + 1)
        return self.sent()[-1], self.entry_of(out["job_id"]), out

    def test_final_gpt_same_prompt_high(self):
        ref = self.asset((200, 10, 10))
        draft = self._draft(GPT, refs=[ref], aspect_ratio="3:2")
        self.assertEqual(draft["params"]["quality"], "low")
        req, final, out = self._final(draft, {})
        self.assertIn("est_cost_usd", out)
        # Peter 2026-10-04 "Draft as reference": every model's Final is an edit with the draft as Image 1.
        self.assertEqual(req.operation, "edit")
        self.assertEqual(req.images[0].convert("RGBA").getpixel((0, 0)), GREEN)
        self.assertEqual(req.images[1].getpixel((0, 0)), (200, 10, 10))
        self.assertEqual(req.quality, "high")
        self.assertEqual(req.aspect_ratio, "3:2")
        self.assertTrue(req.prompt.startswith(jobs.FINAL_LEAD + "\n"))
        self.assertIn("a lighthouse at dusk", req.prompt)
        self.assertTrue(req.prompt.endswith('Text in the image, exactly: "SEA WATCH 1887"\nAvoid: blur'))
        self.assertEqual(final["parent"], draft["id"])
        self.assertEqual(final["tier"], "final")
        for k in ("batch", "variant", "seed", "negative", "exact_text", "sections", "request"):
            self.assertEqual(final[k], draft[k], k)
        self.assertEqual(final["inputs"], [draft["outputs"][0], ref])
        req, final, _ = self._final(draft, {"quality": "max"})
        self.assertEqual(req.quality, "max")
        old = self._draft("gpt-image-2")
        req, _, _ = self._final(old, {"quality": "max"})   # max is not offered on gpt-image-2
        self.assertEqual(req.quality, "high")

    def test_final_gemini_grok_uses_draft_as_image(self):
        refs = [self.asset((i, 50, 50)) for i in range(6)]
        for model, choice, want_res, n_refs in ((GEMINI, {"resolution": "4K"}, "4K", 6),
                                                (GEMINI, {}, "2K", 6),
                                                (GROK, {"resolution": "4K"}, "2K", 4)):
            with self.subTest(model=model, choice=choice):
                draft = self._draft(model, refs=refs)
                req, final, _ = self._final(draft, choice)
                self.assertEqual(req.operation, "edit")
                self.assertEqual(req.images[0].convert("RGBA").getpixel((0, 0)), GREEN)
                self.assertEqual(len(req.images), 1 + n_refs)
                self.assertEqual(req.images[1].getpixel((0, 0)), (0, 50, 50))
                self.assertTrue(req.prompt.startswith(jobs.FINAL_LEAD))
                self.assertIn("a lighthouse at dusk", req.prompt)
                self.assertEqual(req.resolution, want_res)
                self.assertEqual(final["tier"], "final")

    def test_final_keeps_aspect(self):
        for model in (GPT, GEMINI, GROK):
            with self.subTest(model=model):
                draft = self._draft(model, aspect_ratio="16:9")
                req, final, _ = self._final(draft, {})
                self.assertEqual(final["params"]["aspect_ratio"], "16:9")
                self.assertEqual(req.aspect_ratio, "16:9")

    def test_final_unknown_entry_refused(self):
        with self.assertRaises(ValueError):
            self.runner.submit_final(SID, PROJECT, "deadbeef", {})
        self.assertEqual(self.events, [])

    def test_exact_text_sent_quoted(self):
        out = self.runner.submit_batch(SID, PROJECT, self.batch(exact_text="SEA WATCH 1887"))
        self.finish()
        self.assertTrue(self.sent()[-1].prompt.endswith('\nText in the image, exactly: "SEA WATCH 1887"'))
        entry = self.entry_of(out["jobs"][0]["job_id"])
        self.assertEqual(entry["prompt"], "a lighthouse at dusk")
        self.assertEqual(entry["exact_text"], "SEA WATCH 1887")
        self.runner.submit_batch(SID, PROJECT, self.batch(exact_text='THE "OLD" INN'))
        self.finish()
        self.assertTrue(self.sent()[-1].prompt.endswith('Text in the image, exactly: “THE "OLD" INN”'))
        self.assertEqual(jobs.prompt_to_send("p", "neg", "SEA", True),
                         'p\nText in the image, exactly: "SEA"\nAvoid: neg')

    def test_events_carry_batch_variant_model(self):
        out = self.runner.submit_batch(SID, PROJECT, self.batch(models=[GPT, GEMINI], count=2))
        self.finish()
        for j in out["jobs"]:
            for e, d, s in self.events:
                if d["job_id"] == j["job_id"]:
                    self.assertEqual((d["batch"], d["variant"], d["model"]), (out["batch_id"], j["variant"], j["model"]))
        plain = self.runner.submit_cloud(SID, PROJECT, self.spec())
        self.finish()
        for d in [d for e, d, s in self.events if d["job_id"] == plain["job_id"]]:
            self.assertEqual((d["batch"], d["variant"], d["model"]), (None, None, None))


if __name__ == "__main__":
    unittest.main()
