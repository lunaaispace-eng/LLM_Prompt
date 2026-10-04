import test from "node:test";
import assert from "node:assert/strict";
import { createClient } from "../../web/director/core/api_client.mjs";

function fake(reply = {}) {
  const calls = [];
  const fetchApi = async (url, init = {}) => {
    calls.push({ url, method: init.method || "GET", body: init.body });
    return { ok: !reply.error, status: reply.error ? 400 : 200, json: async () => reply };
  };
  return { calls, fetchApi };
}
const body = (c) => JSON.parse(c.body);

test("config is a GET", async () => {
  const f = fake({ engines: ["cloud"] });
  const out = await createClient(f.fetchApi).config();
  assert.deepEqual(out, { engines: ["cloud"] });
  assert.equal(f.calls[0].url, "/luna/director/config");
  assert.equal(f.calls[0].method, "GET");
});

test("apiBase prefixes the path", async () => {
  const f = fake({});
  await createClient(f.fetchApi, "/api").config();
  assert.equal(f.calls[0].url, "/api/luna/director/config");
});

test("error response throws with code", async () => {
  const f = fake({ error: { code: "busy", message: "writer busy" } });
  await assert.rejects(createClient(f.fetchApi).write({}), (e) => e.code === "busy" && e.message === "writer busy");
});

test("write, runCloud", async () => {
  const f = fake({ job_id: "j" });
  const c = createClient(f.fetchApi);
  await c.write({ request: "r" });
  await c.runCloud({ sid: "s", project: "p", spec: {} });
  assert.equal(f.calls[0].url, "/luna/director/write");
  assert.equal(f.calls[0].method, "POST");
  assert.deepEqual(body(f.calls[0]), { request: "r" });
  assert.equal(f.calls[1].url, "/luna/studio/run");
  assert.deepEqual(body(f.calls[1]), { sid: "s", project: "p", spec: {} });
});

test("cancelCloud takes job_id or batch_id", async () => {
  const f = fake({ ok: true });
  const c = createClient(f.fetchApi);
  await c.cancelCloud({ job_id: "j" });
  await c.cancelCloud({ batch_id: "b" });
  assert.equal(f.calls[0].url, "/luna/studio/cancel");
  assert.deepEqual(body(f.calls[0]), { job_id: "j" });
  assert.deepEqual(body(f.calls[1]), { batch_id: "b" });
});

test("cloudJobs encodes sid", async () => {
  const f = fake([]);
  await createClient(f.fetchApi).cloudJobs("a b&c");
  assert.equal(f.calls[0].url, "/luna/studio/jobs?sid=a%20b%26c");
});

test("history, entry, patchHistory", async () => {
  const f = fake({});
  const c = createClient(f.fetchApi);
  await c.history("my proj", 50);
  await c.entry("p", "e/1");
  await c.patchHistory("p", "e1", { star: true });
  assert.equal(f.calls[0].url, "/luna/director/history?project=my%20proj&limit=50");
  assert.equal(f.calls[1].url, "/luna/director/history/e%2F1?project=p");
  assert.equal(f.calls[2].url, "/luna/director/history/e1");
  assert.equal(f.calls[2].method, "POST");
  assert.deepEqual(body(f.calls[2]), { project: "p", patch: { star: true } });
});

test("importAsset posts multipart; importFromRef posts from_ref", async () => {
  const f = fake({ ref: { name: "a.png" } });
  const c = createClient(f.fetchApi);
  const blob = new Blob(["x"], { type: "image/png" });
  await c.importAsset(blob, "p");
  const ref = { name: "t.png", subfolder: "", type: "temp" };
  await c.importFromRef(ref, "p");
  assert.equal(f.calls[0].url, "/luna/director/asset");
  assert.ok(f.calls[0].body instanceof FormData);
  assert.equal(f.calls[0].body.get("project"), "p");
  assert.ok(f.calls[0].body.get("image"));
  assert.equal(f.calls[1].url, "/luna/director/asset");
  assert.deepEqual(body(f.calls[1]), { project: "p", from_ref: ref });
});

test("resize posts project, refs, state, dry_run", async () => {
  const f = fake([]);
  await createClient(f.fetchApi).resize("p", [{ name: "a" }], { all: {} }, true);
  assert.equal(f.calls[0].url, "/luna/director/resize");
  assert.deepEqual(body(f.calls[0]), { project: "p", refs: [{ name: "a" }], state: { all: {} }, dry_run: true });
});

test("viewUrl maps name to filename", () => {
  const c = createClient(async () => ({}));
  assert.equal(c.viewUrl({ name: "a b.png", subfolder: "luna_director/p", type: "input" }),
    "/view?filename=a%20b.png&subfolder=luna_director%2Fp&type=input");
  assert.equal(createClient(async () => ({}), "/api").viewUrl({ name: "a.png" }),
    "/api/view?filename=a.png&subfolder=&type=input");
});

test("runBatch, estimate, final", async () => {
  const f = fake({});
  const c = createClient(f.fetchApi);
  await c.runBatch("s", "p", { models: ["m"] });
  await c.estimate({ batch: { models: ["m"] } });
  await c.estimate({ spec: { a: 1 } });
  await c.final("s", "p", "e1", { quality: "high" });
  assert.equal(f.calls[0].url, "/luna/studio/batch");
  assert.deepEqual(body(f.calls[0]), { sid: "s", project: "p", batch: { models: ["m"] } });
  assert.equal(f.calls[1].url, "/luna/studio/estimate");
  assert.deepEqual(body(f.calls[1]), { batch: { models: ["m"] } });
  assert.deepEqual(body(f.calls[2]), { spec: { a: 1 } });
  assert.equal(f.calls[3].url, "/luna/studio/final");
  assert.deepEqual(body(f.calls[3]), { sid: "s", project: "p", entry_id: "e1", choice: { quality: "high" } });
});
