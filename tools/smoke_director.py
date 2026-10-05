"""Manual Director HTTP smoke (A19). Live mode spends API money; ComfyUI must be running.

Run from this checkout with the ComfyUI python:

    E:\\ComfyUI-Easy-Install\\python_embeded\\python.exe tools\\smoke_director.py
        [--server http://127.0.0.1:8188] [--project smoke]
        [--only inpaint,generate,final] [--dry]

Keys are held by the server: this script never reads credentials or .env files.
Its own UUID sid is used only for HTTP polling (no websocket). Poll every 2 seconds,
timeout 300 seconds per job. --only final also runs Generate to supply its draft.
--dry replaces HTTP with an in-process fake; no server, network or provider imports.
History stores prompt parts separately, so sent_prompt is reconstructed using the
format in luna_director.jobs.prompt_to_send, not a captured provider request.
Exit code = number of FAIL lines. Unknown costs are printed as unknown, never $0.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from PIL import Image, ImageChops, ImageDraw

MODELS = ("gpt-image-2.5-flare", "gemini-3.1-flash-image", "grok-imagine-image")
BOX = (352, 224, 672, 544)  # Pillow paste box: right/bottom exclusive
EXACT = "SEA WATCH 1887"
TERMINAL = {"done", "error", "cancelled"}


def png(image):
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def test_images():
    image = Image.new("RGB", (1024, 768))
    image.putdata([(x * 255 // 1023, y * 255 // 767, 140)
                   for y in range(768) for x in range(1024)])
    ImageDraw.Draw(image).ellipse((392, 264, 632, 504), fill=(240, 210, 30))
    mask = Image.new("L", image.size, 0)
    mask.paste(255, BOX)
    return image, mask


def sent_prompt(entry):
    """Same joining rules as jobs.prompt_to_send; entry.prompt is the unjoined text."""
    result = entry.get("prompt") or ""
    exact = entry.get("exact_text")
    if exact:
        quoted = f'“{exact}”' if '"' in exact else f'"{exact}"'
        result += "\nText in the image, exactly: " + quoted
    writer = entry.get("writer") or {}
    negative = entry.get("negative") or ""
    if writer.get("negative_on") and negative.strip():
        result += "\nAvoid: " + negative
    return result


def money(value):
    return "unknown" if value is None else f"${value:.4f}"


class HTTP:
    def __init__(self, server):
        self.server = server.rstrip("/")

    def request(self, method, path, body=None, upload=None, timeout=30):
        headers, data = {}, None
        if upload is not None:
            project, name, raw = upload
            boundary = "director-smoke-" + uuid.uuid4().hex
            data = (f'--{boundary}\r\nContent-Disposition: form-data; name="project"\r\n\r\n'
                    f'{project}\r\n--{boundary}\r\nContent-Disposition: form-data; '
                    f'name="image"; filename="{name}"\r\nContent-Type: image/png\r\n\r\n').encode()
            data += raw + f"\r\n--{boundary}--\r\n".encode()
            headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            with urlopen(Request(self.server + path, data=data, headers=headers, method=method),
                         timeout=timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            # Do not print arbitrary server response bodies, headers or credentials.
            raise RuntimeError(f"HTTP {exc.code}") from None
        return raw if path.startswith("/view?") else json.loads(raw)


class FakeHTTP:
    """Small route fixture with real ref/job/entry shapes, including a running poll."""
    def __init__(self):
        self.images, self.jobs, self.entries = {}, [], {}

    def image_ref(self, project, image, kind):
        ref = {"name": uuid.uuid4().hex + ".png", "subfolder": "luna_director/" + project,
               "type": kind}
        self.images[(ref["name"], ref["subfolder"], kind)] = png(image)
        return ref

    def submit(self, body, spec):
        project, model = body["project"], spec["model"]
        if spec["operation"] == "inpaint":
            ref = spec["image"]
            image = Image.open(io.BytesIO(self.images[(ref["name"], ref["subfolder"], ref["type"])]))
            image = image.convert("RGB")
            image.paste((220, 30, 30), BOX)
        else:
            image = Image.new("RGB", (128, 128), (100, 140, 160))
        eid, jid = uuid.uuid4().hex, uuid.uuid4().hex
        est = None if model in (MODELS[0], MODELS[2]) else 0.04
        entry = {"id": eid, "project": project, "engine": "cloud", "model": model,
                 "status": "done", "prompt": spec["prompt"], "negative": "",
                 "writer": spec.get("writer"), "exact_text": spec.get("exact_text"),
                 "tier": spec.get("tier"), "parent": spec.get("parent"), "seconds": 0.1,
                 "est_cost_usd": est, "cost_usd": 0.02,
                 "outputs": [self.image_ref(project, image, "output")]}
        self.entries[eid] = entry
        self.jobs.append({"job_id": jid, "state": "queued", "sid": body["sid"], "entry": entry})
        return {"job_id": jid, "est_cost_usd": est}

    def request(self, method, path, body=None, upload=None, timeout=30):
        route, query = urlsplit(path).path, parse_qs(urlsplit(path).query)
        if method == "POST" and route == "/luna/director/asset":
            project, _, raw = upload
            return {"ref": self.image_ref(project, Image.open(io.BytesIO(raw)), "input")}
        if method == "POST" and route == "/luna/studio/estimate":
            return {"total": 0.05, "per_model": [
                {"model": model, "est_cost_usd": cost}
                for model, cost in zip(MODELS, (0.01, 0.04, None))], "unknown": [MODELS[2]]}
        if method == "POST" and route == "/luna/studio/run":
            return self.submit(body, body["spec"])
        if method == "POST" and route == "/luna/studio/batch":
            batch = body["batch"]
            jobs = []
            for model in batch["models"]:
                spec = {**batch["prompts"][0], "operation": "generate", "model": model,
                        "tier": batch["tier"], "exact_text": batch["exact_text"]}
                jobs.append({**self.submit(body, spec), "model": model, "variant": 1})
            return {"batch_id": uuid.uuid4().hex, "jobs": jobs, "est_cost_usd": 0.05,
                    "unknown": [MODELS[2]]}
        if method == "POST" and route == "/luna/studio/final":
            draft = self.entries[body["entry_id"]]
            return self.submit(body, {**draft, "operation": "edit", "tier": "final",
                                      "parent": draft["id"]})
        if method == "GET" and route == "/luna/studio/jobs":
            rows = []
            for job in self.jobs:
                if job["sid"] != query["sid"][0]:
                    continue
                job["state"] = "running" if job["state"] == "queued" else "done"
                row = {"job_id": job["job_id"], "state": job["state"]}
                if row["state"] == "done":
                    row["entry"] = job["entry"]
                rows.append(row)
            return {"jobs": rows}
        if method == "GET" and route == "/luna/director/history":
            return {"entries": list(self.entries.values()), "day_cost": 0.02 * len(self.entries)}
        if method == "GET" and route == "/view":
            return self.images[(query["filename"][0], query["subfolder"][0], query["type"][0])]
        raise ValueError("unsupported fake route")


class Smoke:
    def __init__(self, http, project, dry):
        self.http, self.project, self.dry = http, project, dry
        self.sid, self.costs, self.unknown_costs = uuid.uuid4().hex, {}, set()
        self.fails = 0

    def post(self, path, **payload):
        return self.http.request("POST", path, {"sid": self.sid, "project": self.project, **payload})

    def wait(self, jobs):
        pending = {j["job_id"] for j in jobs}
        ended, deadline = {}, time.monotonic() + 300
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                ended.update({jid: {"state": "timeout"} for jid in pending})
                break
            rows = self.http.request("GET", "/luna/studio/jobs?" + urlencode({"sid": self.sid}),
                                     timeout=min(30, remaining))["jobs"]
            for row in rows:
                entry = row.get("entry") or {}
                if entry.get("id"):
                    if entry.get("cost_usd") is None:
                        self.unknown_costs.add(entry["id"])
                    else:
                        self.costs[entry["id"]] = entry["cost_usd"]
                jid = row["job_id"]
                if jid in pending and row["state"] in TERMINAL:
                    ended[jid] = row
                    pending.remove(jid)
            if pending and not self.dry:
                time.sleep(min(2, max(0, deadline - time.monotonic())))
        return ended

    def verdict(self, step, passed, details=""):
        self.fails += not passed
        print(f"{'PASS' if passed else 'FAIL'} {step} {details}")

    def inpaint(self):
        image, mask = test_images()
        refs = [self.http.request("POST", "/luna/director/asset",
                                 upload=(self.project, name, png(im)))["ref"]
                for name, im in (("image.png", image), ("mask.png", mask))]
        for model in MODELS:
            try:
                spec = {"model": model, "operation": "inpaint", "image": refs[0], "mask": refs[1],
                        "prompt": "replace the circle with a ripe red apple", "n": 1,
                        **({"quality": "low"} if model == MODELS[0] else {"resolution": "1K"})}
                job = self.post("/luna/studio/run", spec=spec)
                row = self.wait([job])[job["job_id"]]
                entry, outside_ok = row.get("entry") or {}, False
                if row["state"] == "done" and entry.get("outputs"):
                    ref = entry["outputs"][0]
                    raw = self.http.request("GET", "/view?" + urlencode(
                        {"filename": ref["name"], "subfolder": ref["subfolder"], "type": "output"}))
                    with Image.open(io.BytesIO(raw)) as result:
                        # Compare at the input size; mask box is excluded from the difference.
                        diff = ImageChops.difference(image, result.convert("RGB").resize(image.size))
                    diff.paste((0, 0, 0), BOX)
                    outside_ok = diff.getbbox() is None
                self.verdict("inpaint", row["state"] == "done" and outside_ok,
                             f"{model} state={row['state']} seconds={entry.get('seconds', 'unknown')} "
                             f"est={money(job['est_cost_usd'])} actual={money(entry.get('cost_usd'))}")
            except Exception as exc:
                self.verdict("inpaint", False, f"{model} {type(exc).__name__}")

    def generate(self):
        batch = {"models": list(MODELS), "count": 1, "variants_mode": "same", "tier": "draft",
                 "prompts": [{"prompt": "a lighthouse keeper at dawn, a sign on the door"}],
                 "exact_text": EXACT}
        estimate = self.post("/luna/studio/estimate", batch=batch)
        print("estimate " + json.dumps(estimate, sort_keys=True))
        jobs = self.post("/luna/studio/batch", batch=batch)["jobs"]
        rows, draft, passed = self.wait(jobs), None, len(jobs) == 3
        for job in jobs:
            row = rows[job["job_id"]]
            entry = row.get("entry") or {}
            prompt = sent_prompt(entry)
            print(f"generate {job['model']} state={row['state']} seconds={entry.get('seconds', 'unknown')} "
                  f"actual={money(entry.get('cost_usd'))} sent_prompt (reconstructed): {prompt}")
            passed &= (row["state"] == "done" and f'"{EXACT}"' in prompt)
            if (job["model"] == MODELS[1] and row["state"] == "done"
                    and entry.get("tier") == "draft" and entry.get("outputs")):
                draft = entry
        passed &= {j["model"] for j in jobs} == set(MODELS)
        self.verdict("generate", passed)
        return draft

    def final(self, draft):
        if draft is None:
            self.verdict("final", False, "no completed Gemini draft")
            return
        job = self.post("/luna/studio/final", entry_id=draft["id"], choice={"resolution": "2K"})
        row = self.wait([job])[job["job_id"]]
        entry = row.get("entry") or {}
        passed = (row["state"] == "done" and entry.get("tier") == "final"
                  and entry.get("parent") == draft["id"] and bool(entry.get("outputs")))
        self.verdict("final", passed, f"state={row['state']} actual={money(entry.get('cost_usd'))}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Director live HTTP smoke (spends money without --dry).")
    parser.add_argument("--server", default="http://127.0.0.1:8188")
    parser.add_argument("--project", default="smoke")
    parser.add_argument("--only", default="inpaint,generate,final")
    parser.add_argument("--dry", action="store_true")
    args = parser.parse_args(argv)
    steps = {part.strip() for part in args.only.split(",")}
    if not steps or steps - {"inpaint", "generate", "final"}:
        parser.error("--only takes inpaint,generate,final (comma separated)")
    if "final" in steps:
        steps.add("generate")
    smoke = Smoke(FakeHTTP() if args.dry else HTTP(args.server), args.project, args.dry)
    draft = None
    for step in ("inpaint", "generate", "final"):
        if step not in steps:
            continue
        try:
            if step == "generate":
                draft = smoke.generate()
            elif step == "final":
                smoke.final(draft)
            else:
                smoke.inpaint()
        except Exception as exc:
            smoke.verdict(step, False, type(exc).__name__)
    try:
        history = smoke.http.request("GET", "/luna/director/history?" + urlencode({"project": args.project}))
        day = money(history["day_cost"])
    except Exception as exc:
        smoke.verdict("history", False, type(exc).__name__)
        day = "unknown"
    print(f"TOTAL actual_cost={money(sum(smoke.costs.values()))} "
          f"unknown_cost_entries={len(smoke.unknown_costs)} day_cost={day} FAILs={smoke.fails}")
    return smoke.fails


if __name__ == "__main__":
    sys.exit(main())
