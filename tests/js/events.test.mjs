import test from "node:test";
import assert from "node:assert/strict";
import { onJobEvent, reconcileCloud } from "../../web/director/core/events.mjs";

const ev = (data) => ({ type: "luna.job", data });
const entry = { id: "e1" };

test("a job id not live in this tab is dropped", () => {
  assert.equal(onJobEvent(ev({ job_id: "x", state: "running" }), new Set(["y"])), null);
  assert.equal(onJobEvent(ev({ job_id: "x", state: "running" }), []), null);
});

test("other message types are ignored", () => {
  assert.equal(onJobEvent({ type: "status", data: { job_id: "x" } }, ["x"]), null);
  assert.equal(onJobEvent(null, ["x"]), null);
});

test("done carries the entry", () => {
  assert.deepEqual(onJobEvent(ev({ job_id: "x", state: "done", entry }), ["x"]),
    { jobId: "x", patch: { state: "done", entry } });
});

test("error stays {code, message}", () => {
  const error = { code: "refused", message: "no" };
  assert.deepEqual(onJobEvent(ev({ job_id: "x", state: "error", error }), ["x"]),
    { jobId: "x", patch: { state: "error", error } });
});

test("batch, variant and model reach the patch", () => {
  const r = onJobEvent(ev({ job_id: "x", state: "running", batch: "b", variant: 2, model: "m" }), ["x"]);
  assert.deepEqual(r.patch, { state: "running", batch: "b", variant: 2, model: "m" });
});

test("cancelled with an entry keeps the image", () => {
  assert.deepEqual(onJobEvent(ev({ job_id: "x", state: "cancelled", entry }), ["x"]),
    { jobId: "x", patch: { state: "cancelled", entry } });
});

const L = (serverId, state = "running", extra = {}) => ({ id: "l" + serverId, serverId, state, ...extra });

test("reconcile: server done gives one patch with the entry", () => {
  const out = reconcileCloud([L("a")], [{ job_id: "a", state: "done", entry }]);
  assert.deepEqual(out, [{ jobId: "a", patch: { state: "done", entry } }]);
});

test("reconcile: server running changes nothing", () => {
  assert.deepEqual(reconcileCloud([L("a")], [{ job_id: "a", state: "running" }]), []);
});

test("reconcile: server error and cancelled", () => {
  const error = { code: "provider", message: "x" };
  const out = reconcileCloud([L("a"), L("b")], [
    { job_id: "a", state: "error", error }, { job_id: "b", state: "cancelled", entry }]);
  assert.deepEqual(out, [
    { jobId: "a", patch: { state: "error", error } },
    { jobId: "b", patch: { state: "cancelled", entry } }]);
});

test("reconcile: unknown to the server is lost", () => {
  const out = reconcileCloud([L("a")], []);
  assert.equal(out.length, 1);
  assert.equal(out[0].jobId, "a");
  assert.equal(out[0].patch.state, "error");
  assert.equal(out[0].patch.error.code, "lost");
});

test("reconcile: a job without serverId is untouched", () => {
  assert.deepEqual(reconcileCloud([{ id: "q", state: "queued" }, { id: "r", state: "running" }], []), []);
});

test("reconcile: a locally finished job is not patched again", () => {
  assert.deepEqual(reconcileCloud([L("a", "done")], [{ job_id: "a", state: "done", entry }]), []);
  assert.deepEqual(reconcileCloud([L("a", "done")], []), []);
});
