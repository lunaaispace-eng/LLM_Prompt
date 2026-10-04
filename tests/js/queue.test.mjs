import test from "node:test";
import assert from "node:assert/strict";
import { createQueue } from "../../web/director/core/queue.mjs";

const mk = (n, extra = {}) => Array.from({ length: n }, (_, i) => ({ id: `j${i}`, kind: "cloud", ...extra }));
const ended = ["done", "error", "cancelled"];
const past = (q) => q.jobs().filter((j) => j.state !== "queued" && !ended.includes(j.state));

test("the 4th cloud job waits until one of the first three ends", () => {
  const q = createQueue();
  mk(4).forEach((j) => q.add(j));
  const got = [q.next(), q.next(), q.next()];
  assert.deepEqual(got.map((j) => j.id), ["j0", "j1", "j2"]);
  assert.equal(q.next(), null);
  q.update("j1", { state: "running" });
  assert.equal(q.next(), null);
  q.update("j1", { state: "done" });
  assert.equal(q.next().id, "j3");
});

test("error and cancelled also free a slot", () => {
  for (const state of ["error", "cancelled"]) {
    const q = createQueue();
    mk(4).forEach((j) => q.add(j));
    q.next(); q.next(); q.next();
    q.update("j0", { state });
    assert.equal(q.next().id, "j3");
  }
});

test("a cancelled queued job never leaves queued", () => {
  const q = createQueue({ cloudParallel: 1 });
  mk(2).forEach((j) => q.add(j));
  q.update("j1", { state: "cancelled" });
  q.next();
  q.update("j0", { state: "done" });
  assert.equal(q.next(), null);
  assert.equal(q.get("j1").state, "cancelled");
});

test("next sets startedAt and the job leaves queued", () => {
  const q = createQueue({ now: () => 42 });
  q.add({ id: "a", kind: "cloud" });
  const j = q.next();
  assert.equal(j.state, "uploading");
  assert.equal(j.startedAt, 42);
});

test("limits live in a per-kind table", () => {
  const q = createQueue({ cloudParallel: 2 });
  assert.deepEqual(q.limits, { cloud: 2 });
  q.limits.package = 1;
  q.add({ id: "p1", kind: "package" });
  q.add({ id: "p2", kind: "package" });
  assert.equal(q.next().id, "p1");
  assert.equal(q.next(), null);
});

test("a 12-job batch has at most 3 past queued", () => {
  const q = createQueue();
  q.addBatch("b1", mk(12).map((j, i) => ({ ...j, serverId: `s${i}`, variant: (i % 4) + 1, model: "m" })));
  while (q.next()) { /* drain */ }
  assert.equal(past(q).length, 3);
  assert.equal(q.get("j0").batch, "b1");
  assert.equal(q.get("j5").serverId, "s5");
});

test("cancelBatch removes queued and flags running", () => {
  const q = createQueue();
  q.addBatch("b1", mk(5));
  q.next(); q.next(); q.next();
  q.update("j0", { state: "running" });
  q.update("j1", { state: "done" });
  const res = q.cancelBatch("b1");
  const by = Object.fromEntries(res.map((r) => [r.id, r.action]));
  assert.deepEqual(by, { j0: "flagged", j2: "flagged", j3: "removed", j4: "removed" });
  assert.equal(q.get("j3"), undefined);
  assert.equal(q.get("j0").cancelledAfterSend, true);
  assert.equal(q.get("j1").cancelledAfterSend, undefined);
  assert.equal(q.get("j1").state, "done");
});

test("batches groups by batch with a state", () => {
  const q = createQueue();
  q.addBatch("b1", mk(2));
  q.addBatch("b2", [{ id: "x", kind: "cloud" }]);
  q.add({ id: "solo", kind: "cloud" });
  q.next();
  const b = q.batches();
  assert.deepEqual(b.map((x) => x.batchId), ["b1", "b2"]);
  assert.equal(b[0].jobs.length, 2);
  assert.equal(b[0].state, "running");
  assert.equal(b[1].state, "queued");
  q.update("x", { state: "done" });
  assert.equal(q.batches()[1].state, "done");
});
