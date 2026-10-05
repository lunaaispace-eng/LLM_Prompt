"""A6: the Director routes on a plain aiohttp app with a fake writer and a fake job runner; the store is real
(temp folders). Loaded through tests/_comfy.py, so the routes import the pack's own modules."""
import io
import json
import os
import shutil
import tempfile
import types
from pathlib import Path
from unittest import mock

import _comfy
from aiohttp import FormData, web
from aiohttp.test_utils import AioHTTPTestCase
from PIL import Image

routes_mod = _comfy.load("luna_director.routes")
writer_mod = _comfy.load("luna_director.writer")
store_mod = _comfy.load("luna_director.store")
jobs_mod = _comfy.load("luna_director.jobs")

PROJECT = "demo"
GEN_KEYS = {"size": [1024, 1024], "variants": 3, "sections": True, "exact_text": "OPEN", "feedback": "",
            "prior_prompt": "", "result": None}


def _png(color=(200, 10, 10), size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class FakeWriter:
    WriterRequest = writer_mod.WriterRequest
    WriterError = writer_mod.WriterError

    def __init__(self):
        self.requests = []
        self.raise_with = None

    def _presets(self):
        return {"Image Edit": "", "Edit Rewrite - GPT Image": "", "Generate - GPT Image": ""}

    def write(self, req):
        self.requests.append(req)
        if self.raise_with:
            raise self.raise_with
        return writer_mod.WriterResult("a red apple", "", "", "Edit Rewrite - GPT Image", ["Image 1: the picture"],
                                       0.1, [{"positive": "a red apple", "negative": "", "sections": None}])


class FakeJobs:
    def __init__(self, store):
        self.store = store
        self.calls = []

    def submit_cloud(self, sid, project, spec):
        self.calls.append(("run", sid, project, spec))
        jobs_mod.normalize_spec(spec)
        return {"job_id": "j1", "est_cost_usd": 0.04}

    def submit_batch(self, sid, project, batch):
        self.calls.append(("batch", sid, project, batch))
        est = jobs_mod.estimate_batch(batch)
        jobs = [{"job_id": f"j{i}", "model": m, "variant": v, "est_cost_usd": None}
                for i, (v, m) in enumerate(((v, m) for v in range(1, batch["count"] + 1) for m in batch["models"]))]
        return {"batch_id": "b1", "jobs": jobs, "est_cost_usd": est["total"], "unknown": est["unknown"]}

    def submit_final(self, sid, project, entry_id, choice):
        self.calls.append(("final", sid, project, entry_id, choice))
        if self.store.get_entry(project, entry_id) is None:
            raise ValueError(f"no history entry {entry_id!r}")
        return {"job_id": "jf", "est_cost_usd": 0.2}

    def cancel(self, job_id):
        self.calls.append(("cancel", job_id))
        return True

    def cancel_batch(self, batch_id):
        self.calls.append(("cancel_batch", batch_id))
        return 2

    def jobs_for(self, sid):
        return [{"job_id": "j1", "state": "running"}] if sid == "s1" else []


def _batch(**over):
    b = {"models": ["gpt-image-2", "gemini-3.1-flash-image"], "count": 2, "variants_mode": "same",
         "prompts": [{"prompt": "a fox"}], "tier": "draft"}
    b.update(over)
    return b


class DirectorRoutes(AioHTTPTestCase):
    async def get_application(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dirs = {k: str(self.tmp / k) for k in ("input", "output", "temp")}
        for d in self.dirs.values():
            Path(d).mkdir()
        self.store = store_mod.Store(self.tmp / "output" / "luna_director", self.tmp / "input" / "luna_director")
        self.writer = FakeWriter()
        self.jobs = FakeJobs(self.store)
        deps = routes_mod.Deps(store=self.store, jobs=self.jobs, writer=self.writer, dirs=self.dirs)
        table = web.RouteTableDef()
        routes_mod.register_routes(table, deps)
        app = web.Application()
        app.add_routes(table)
        return app

    async def asyncTearDown(self):
        await super().asyncTearDown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _post(self, path, body):
        resp = await self.client.post(path, json=body)
        return resp.status, await resp.json()

    async def _asset(self, data=None):
        form = FormData()
        form.add_field("project", PROJECT)
        form.add_field("image", data or _png(), filename="a.png", content_type="image/png")
        resp = await self.client.post("/luna/director/asset", data=form)
        return resp.status, await resp.json()

    def _write_body(self, canvas, **over):
        body = {"canvas": canvas, "mask": None, "refs": [], "provider": "Gemini", "model": "gemini-3.5-flash-lite",
                "preset": None, "target_model": "gpt-image-2", "operation": "edit", "request": "add an apple",
                "send": {"canvas": True, "mask": True, "refs": True}, "thinking": False, "negative": False,
                "mask_mode": "auto", "crop_padding": 0.25, "vision_mp": 1.0, "server_url": "http://x:1",
                "gguf": {}, "size": None, "variants": 1, "sections": False, "exact_text": "", "feedback": "",
                "prior_prompt": "", "result": None}
        body.update(over)
        return body

    # ---- config

    async def test_config_lists_writers_and_presets(self):
        resp = await self.client.get("/luna/director/config")
        cfg = await resp.json()
        self.assertEqual(resp.status, 200)
        self.assertIn("Local GGUF", cfg["writers"])
        self.assertIn("Gemini", cfg["writers"]["providers"])
        flags = {p["title"]: p for p in cfg["presets"]}
        self.assertTrue(flags["Edit Rewrite - GPT Image"]["edit_rewrite"])
        self.assertFalse(flags["Image Edit"]["edit_rewrite"])

    async def test_config_engines_cloud_only(self):
        cfg = await (await self.client.get("/luna/director/config")).json()
        self.assertEqual(cfg["engines"], ["cloud"])
        self.assertEqual(cfg["settings"], {})

    async def test_config_cloud_models_grouped_with_limits_controls_and_default_preset(self):
        cfg = await (await self.client.get("/luna/director/config")).json()
        listed = [m["id"] for g in cfg["cloud"] for m in g["models"]]
        studio = _comfy.load("luna_image_studio_node")
        self.assertEqual(sorted(listed), sorted(set(studio.MODELS)))
        self.assertEqual(len(listed), len(set(listed)))
        grok = next(m for g in cfg["cloud"] if g["provider"] == "xai" for m in g["models"]
                    if m["id"] == "grok-imagine-image")
        self.assertEqual(grok["ref_limit"]["edit"], 4)
        self.assertEqual(grok["ref_limit"]["compose"], 5)
        self.assertEqual(grok["ref_limit"]["generate"], 0)
        self.assertEqual(grok["default_preset"], "Edit Rewrite - Grok Imagine")
        self.assertIn("seed", grok["controls"])
        self.assertEqual(grok["options"]["seed"], {"sent": False})
        gpt25 = next(m for g in cfg["cloud"] for m in g["models"] if m["id"] == "gpt-image-2.5-flare")
        self.assertIn("background", gpt25["controls"])

    async def test_config_generate_preset_draft_and_final_options(self):
        cfg = await (await self.client.get("/luna/director/config")).json()
        for g in cfg["cloud"]:
            for m in g["models"]:
                self.assertTrue({"generate_preset", "draft", "final_options"} <= set(m), m["id"])
                if m["id"].startswith("gpt-image"):
                    self.assertEqual(m["generate_preset"], "Generate - GPT Image")

    # ---- write

    async def test_write_returns_result(self):
        _, a = await self._asset()
        status, res = await self._post("/luna/director/write", self._write_body(a["ref"]))
        self.assertEqual(status, 200)
        self.assertEqual(res["positive"], "a red apple")
        self.assertEqual(res["preset_used"], "Edit Rewrite - GPT Image")

    async def test_write_body_keys(self):
        _, a = await self._asset()
        status, _ = await self._post("/luna/director/write", self._write_body(a["ref"]))
        self.assertEqual(status, 200)
        req = self.writer.requests[-1]
        self.assertEqual(req.server_url, "http://x:1")
        self.assertEqual(req.canvas.size, (64, 48))
        status, err = await self._post("/luna/director/write", self._write_body(a["ref"], bogus=1))
        self.assertEqual((status, err["error"]["code"]), (400, "refused"))
        # A Generate body: no canvas, three variants.
        body = self._write_body(None, operation="generate", refs=[], **GEN_KEYS)
        status, _ = await self._post("/luna/director/write", body)
        self.assertEqual(status, 200)
        req = self.writer.requests[-1]
        self.assertIsNone(req.canvas)
        self.assertEqual((req.operation, req.variants, req.size, req.exact_text), ("generate", 3, (1024, 1024), "OPEN"))

    async def test_write_outpaint_grows_canvas(self):
        _, a = await self._asset()
        status, _ = await self._post("/luna/director/write",
                                     self._write_body(a["ref"], operation="outpaint", outpaint=[10, 0, 10, 0]))
        self.assertEqual(status, 200)
        req = self.writer.requests[-1]
        self.assertEqual(req.canvas.size, (84, 48))
        self.assertEqual(req.mask.getpixel((0, 0)), 255)
        self.assertEqual(req.mask.getpixel((40, 20)), 0)

    async def test_write_busy_is_409(self):
        _, a = await self._asset()
        self.writer.raise_with = writer_mod.WriterError("busy", "GGUF busy")
        status, err = await self._post("/luna/director/write", self._write_body(a["ref"]))
        self.assertEqual((status, err["error"]["code"]), (409, "busy"))
        self.writer.raise_with = writer_mod.WriterError("no_key", "no key")
        status, err = await self._post("/luna/director/write", self._write_body(a["ref"]))
        self.assertEqual((status, err["error"]["code"]), (400, "no_key"))

    async def test_route_errors_scrub_environment_file_and_key_patterns(self):
        _, a = await self._asset()
        api = _comfy.load("llm_prompt_api_node")
        keys = ["FAKE-ENV-CREDENTIAL", "FAKE-FILE-CREDENTIAL", "FAKE-ALIAS-CREDENTIAL",
                "sk-FAKE0123456789", "xai-FAKE0123456789", "AIzaFAKE012345678901234567"]
        self.writer.raise_with = writer_mod.WriterError("provider", "failure " + " ".join(keys))
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": keys[0], "XAI_API_KEY": keys[2]}), \
                mock.patch.object(api, "_load_env_file_keys", return_value={"OPENAI_API_KEY": keys[1]}):
            status, err = await self._post("/luna/director/write", self._write_body(a["ref"]))
        self.assertEqual((status, err["error"]["code"]), (400, "provider"))
        for key in keys:
            self.assertNotIn(key, json.dumps(err))
        self.assertIn("[key removed]", err["error"]["message"])

    async def test_ref_traversal_400(self):
        bad = {"name": "../../secret.png", "subfolder": "", "type": "input"}
        status, err = await self._post("/luna/director/write", self._write_body(bad))
        self.assertEqual((status, err["error"]["code"]), (400, "bad_ref"))
        status, err = await self._post("/luna/director/asset", {"project": PROJECT, "from_ref": bad})
        self.assertEqual((status, err["error"]["code"]), (400, "bad_ref"))

    async def test_asset_missing_or_directory_ref_is_bad_ref(self):
        Path(self.dirs["input"], "directory").mkdir()
        for name in ("missing.png", "directory"):
            with self.subTest(name=name):
                status, err = await self._post("/luna/director/asset", {"project": PROJECT,
                    "from_ref": {"name": name, "subfolder": "", "type": "input"}})
                self.assertEqual((status, err["error"]["code"]), (400, "bad_ref"))

    async def test_outpaint_margin_cap_refused(self):
        _, a = await self._asset()
        status, err = await self._post("/luna/director/write",
            self._write_body(a["ref"], operation="outpaint", outpaint=[2049, 0, 0, 0]))
        self.assertEqual((status, err["error"]["code"]), (400, "refused"))
        self.assertEqual(self.writer.requests, [])
        self.assertEqual(routes_mod._margins([2048, 0, 0, 0]), [2048, 0, 0, 0])

    async def test_body_flags_require_json_booleans(self):
        _, a = await self._asset()
        for flag in ("thinking", "negative", "sections"):
            for value in ("false", 0, None, []):
                with self.subTest(flag=flag, value=value):
                    status, err = await self._post("/luna/director/write",
                        self._write_body(a["ref"], **{flag: value}))
                    self.assertEqual((status, err["error"]["code"]), (400, "refused"))
        for flag in ("canvas", "mask", "refs", "extra"):
            with self.subTest(send=flag):
                status, err = await self._post("/luna/director/write",
                    self._write_body(a["ref"], send={flag: "false"}))
                self.assertEqual((status, err["error"]["code"]), (400, "refused"))
        status, err = await self._post("/luna/director/resize",
            {"project": PROJECT, "refs": [a["ref"]], "dry_run": "false"})
        self.assertEqual((status, err["error"]["code"]), (400, "refused"))
        self.assertEqual(self.writer.requests, [])

    async def test_body_numeric_conversions_refused(self):
        _, a = await self._asset()
        for over in ({"variants": {}}, {"size": [{}, 1024]}, {"variants": "2"},
                     {"crop_padding": {}}, {"vision_mp": {}}, {"gguf": []},
                     {"gguf": {"image_max_tokens": {}}}, {"gguf": {"frequency_penalty": []}}):
            with self.subTest(over=over):
                status, err = await self._post("/luna/director/write", self._write_body(a["ref"], **over))
                self.assertEqual((status, err["error"]["code"]), (400, "refused"))
        status, err = await self._post("/luna/director/resize", {"project": PROJECT,
            "refs": [a["ref"]], "state": {"all": {"mode": "longest_side", "longest_side": {}}}})
        self.assertEqual((status, err["error"]["code"]), (400, "refused"))

    async def test_internal_typeerror_returns_500(self):
        _, a = await self._asset()
        self.writer.raise_with = TypeError("fake internal failure")
        status, err = await self._post("/luna/director/write", self._write_body(a["ref"]))
        self.assertEqual((status, err["error"]["code"]), (500, "internal"))

    async def test_run_and_batch_count_caps_refused(self):
        status, err = await self._post("/luna/studio/run", {"project": PROJECT,
            "spec": {"model": "gpt-image-2", "operation": "generate", "prompt": "a fox", "n": 9}})
        self.assertEqual((status, err["error"]["code"]), (400, "refused"))
        status, err = await self._post("/luna/studio/batch", {"project": PROJECT, "batch": _batch(count=9)})
        self.assertEqual((status, err["error"]["code"]), (400, "refused"))

    # ---- assets and resize

    async def test_asset_import_dedups(self):
        s1, a = await self._asset()
        s2, b = await self._asset()
        self.assertEqual((s1, s2), (200, 200))
        self.assertEqual(a["ref"], b["ref"])
        Path(self.dirs["output"], "g.png").write_bytes(_png())
        status, c = await self._post("/luna/director/asset",
                                     {"project": PROJECT, "from_ref": {"name": "g.png", "subfolder": "", "type": "output"}})
        self.assertEqual((status, c["ref"]), (200, a["ref"]))

    async def test_resize_dry_run_and_save(self):
        _, a = await self._asset(_png(size=(400, 200)))
        state = {"all": {"mode": "longest_side", "longest_side": 100}}
        before = sorted(p.name for p in Path(self.dirs["input"], "luna_director", PROJECT).iterdir())
        status, dry = await self._post("/luna/director/resize",
                                       {"project": PROJECT, "refs": [a["ref"]], "state": state, "dry_run": True})
        self.assertEqual(status, 200)
        self.assertEqual(dry["items"][0]["out"], [100, 50])
        self.assertEqual(sorted(p.name for p in Path(self.dirs["input"], "luna_director", PROJECT).iterdir()), before)
        status, done = await self._post("/luna/director/resize",
                                        {"project": PROJECT, "refs": [a["ref"]], "state": state})
        self.assertEqual(status, 200)
        new = done["items"][0]["ref"]
        self.assertNotEqual(new, a["ref"])
        self.assertTrue(store_mod.resolve_ref(a["ref"], self.dirs).is_file())

    async def test_resize_bad_pad_color_400(self):
        _, a = await self._asset()
        state = {"all": {"ratio": "1:1", "ratio_action": "pad", "pad_color": "red"}}
        status, err = await self._post("/luna/director/resize", {"project": PROJECT, "refs": [a["ref"]], "state": state})
        self.assertEqual((status, err["error"]["code"]), (400, "bad_state"))

    # ---- cloud runs and Generate

    async def test_run_returns_job_id(self):
        spec = {"model": "gpt-image-2", "operation": "generate", "prompt": "a fox"}
        status, res = await self._post("/luna/studio/run", {"sid": "s1", "project": PROJECT, "spec": spec})
        self.assertEqual((status, res["job_id"]), (200, "j1"))
        status, err = await self._post("/luna/studio/run",
                                       {"sid": "s1", "project": PROJECT, "spec": {**spec, "model": "nope"}})
        self.assertEqual((status, err["error"]["code"]), (400, "refused"))

    async def test_jobs_route_lists_sid_jobs_without_keys(self):
        resp = await self.client.get("/luna/studio/jobs?sid=s1")
        body = await resp.json()
        self.assertEqual(body["jobs"], [{"job_id": "j1", "state": "running"}])
        self.assertNotIn("key", json.dumps(body).lower())

    async def test_batch_route_returns_jobs(self):
        status, res = await self._post("/luna/studio/batch", {"sid": "s1", "project": PROJECT, "batch": _batch()})
        self.assertEqual(status, 200)
        self.assertEqual(res["batch_id"], "b1")
        self.assertEqual(len(res["jobs"]), 4)
        self.assertIn("unknown", res)

    async def test_batch_prompt_count_mismatch_400(self):
        bad = _batch(variants_mode="varied", count=3, prompts=[{"prompt": "a"}])
        status, err = await self._post("/luna/studio/batch", {"sid": "s1", "project": PROJECT, "batch": bad})
        self.assertEqual((status, err["error"]["code"]), (400, "refused"))

    async def test_estimate_route_sum(self):
        status, res = await self._post("/luna/studio/estimate", {"batch": _batch()})
        self.assertEqual(status, 200)
        known = [p["est_cost_usd"] for p in res["per_model"] if p["est_cost_usd"] is not None]
        self.assertAlmostEqual(res["total"], sum(known))
        status, one = await self._post("/luna/studio/estimate",
                                       {"spec": {"model": "gemini-3.1-flash-image", "operation": "generate",
                                                 "prompt": "a fox"}})
        self.assertEqual(status, 200)
        self.assertEqual(len(one["per_model"]), 1)

    async def test_final_route_child_of_entry(self):
        entry = self.store.add_history(PROJECT, jobs_mod.build_entry(
            jobs_mod.normalize_spec({"model": "gpt-image-2", "operation": "generate", "prompt": "a fox"}),
            status="done", outputs=[], cost_usd=0.01, est_cost_usd=0.01, seconds=1.0, mode="native", error=None))
        status, res = await self._post("/luna/studio/final", {"sid": "s1", "project": PROJECT,
                                                              "entry_id": entry["id"], "choice": {"quality": "high"}})
        self.assertEqual((status, res["job_id"]), (200, "jf"))
        self.assertEqual(self.jobs.calls[-1][3:], (entry["id"], {"quality": "high"}))
        status, err = await self._post("/luna/studio/final",
                                       {"sid": "s1", "project": PROJECT, "entry_id": "missing", "choice": {}})
        self.assertEqual((status, err["error"]["code"]), (400, "refused"))

    async def test_cancel_route_accepts_batch_id(self):
        status, res = await self._post("/luna/studio/cancel", {"batch_id": "b1"})
        self.assertEqual((status, res), (200, {"ok": True, "cancelled": 2}))
        status, res = await self._post("/luna/studio/cancel", {"job_id": "j1"})
        self.assertEqual((status, res), (200, {"ok": True}))

    # ---- history

    async def _entry(self):
        return self.store.add_history(PROJECT, jobs_mod.build_entry(
            jobs_mod.normalize_spec({"model": "gpt-image-2", "operation": "generate", "prompt": "a fox"}),
            status="done", outputs=[], cost_usd=0.25, est_cost_usd=0.2, seconds=1.0, mode="native", error=None))

    async def test_history_returns_entries_and_day_cost(self):
        e = await self._entry()
        resp = await self.client.get(f"/luna/director/history?project={PROJECT}&limit=10")
        body = await resp.json()
        self.assertEqual([x["id"] for x in body["entries"]], [e["id"]])
        self.assertAlmostEqual(body["day_cost"], 0.25)
        one = await (await self.client.get(f"/luna/director/history/{e['id']}?project={PROJECT}")).json()
        self.assertEqual(one["id"], e["id"])

    async def test_history_patch_needs_project_and_allowed_keys(self):
        e = await self._entry()
        status, err = await self._post(f"/luna/director/history/{e['id']}", {"patch": {"star": True}})
        self.assertEqual((status, err["error"]["code"]), (400, "refused"))
        status, err = await self._post(f"/luna/director/history/{e['id']}",
                                       {"project": PROJECT, "patch": {"prompt": "x"}})
        self.assertEqual((status, err["error"]["code"]), (400, "refused"))
        status, res = await self._post(f"/luna/director/history/{e['id']}",
                                       {"project": PROJECT, "patch": {"star": True}})
        self.assertEqual((status, res["star"]), (200, True))

    async def test_no_package_routes_without_packages_root(self):
        resp = await self.client.get("/luna/packages")
        self.assertEqual(resp.status, 404)
