import test from "node:test";
import assert from "node:assert/strict";
import { createStore, defaultState } from "../../web/director/core/store.mjs";
import { createMaskHandoff } from "../../web/director/ui/mask_handoff.mjs";
import { runCurrent, cancelJob, cancelBatch, installQueueActions, queueRows, elapsedLabel, mountQueue, queueFailureText } from "../../web/director/ui/queue_panel.mjs";
import { writePrompt } from "../../web/director/ui/writer_panel.mjs";
import { SLOT_TABLE } from "../../web/director/frames/overlay.mjs";
const config = { cloud: [{ models: [{ id: "model", ref_limit: { inpaint: 1, edit: 1 } }] }] };
function setup(operation = "inpaint") {
  const d = defaultState(), calls = [], server = [];
  const store = createStore({ project: "test", socketSid: "socket-owned-sid", cloudConfig: config,
    asset: { name: "canvas.png" }, prompt: "Prompt", request: "Request", maskEmpty: false,
    engine: { ...d.engine, model: "model", params: { ...d.engine.params, operation } },
    writer: { ...d.writer, model: "writer" } });
  const client = {
    async importAsset(blob, project) { calls.push(["mask", blob, project]); return { ref: { name: "mask.png" } }; },
    async runCloud(body) { calls.push(["run", body]); const job_id = `server-${server.length}`;
      server.push({ job_id, state: "running" }); return { job_id, est_cost_usd: 0.05 }; },
    async cloudJobs(sid) { assert.equal(sid, "socket-owned-sid"); return { jobs: server.map((j) => ({ ...j })) }; },
    async history() { calls.push(["history"]); return { day_cost: 9.87, entries: [] }; },
    async cancelCloud(body) { calls.push(["cancel", body]); },
    async write(body) { calls.push(["write", body]); return { positive: "Written" }; },
  };
  const handoff = createMaskHandoff(store, client, () => "png");
  const change = (data = [255, 0]) => handoff.changed({ w: 2, h: 1, data: new Uint8ClampedArray(data) }, "canvas");
  change();
  return { store, client, calls, server, handoff, change, control: installQueueActions(store, client) };
}
test("a billed queue row says billed — no image, with its cost or cost unknown", () => {
  assert.equal(queueFailureText({ error: { code: "billed", message: "raw provider text" }, entry: { cost_usd: 0.2 } }),
    "billed — no image · $0.20");
  assert.equal(queueFailureText({ error: { code: "billed", message: "raw" }, entry: { cost_usd: null } }),
    "billed — no image · cost unknown");
  assert.equal(queueFailureText({ error: { code: "provider", message: "nope" } }), "provider: nope");
  assert.equal(queueFailureText({}), "");
});
test("run uploads mask once per version, sends socket sid/spec, and stores serverId; action is shared", async () => {
  const { store, client, calls, change } = setup();
  const first = await runCurrent(store, client);
  const second = await store.act("runCurrent");
  assert.equal(calls.filter((c) => c[0] === "mask").length, 1);
  assert.equal(store.get().jobs[first].serverId, "server-0");
  assert.equal(store.get().jobs[second].state, "running");
  assert.ok(store.get().jobs[first].startedAt);
  const body = calls.find((c) => c[0] === "run")[1];
  assert.equal(body.sid, "socket-owned-sid"); assert.equal(body.spec.mask.name, "mask.png");
  assert.deepEqual(calls.slice(0, 2).map((c) => c[0]), ["mask", "run"]);
  change([0, 255]); await store.act("runCurrent");
  assert.equal(calls.filter((c) => c[0] === "mask").length, 2);
});
test("mask changes publish empty/revision, remount cache and concurrent uploads, asset changes invalidate", async () => {
  const { store, client, handoff, calls, change } = setup();
  const v = store.get().maskVersion;
  await Promise.all([store.act("uploadMask"), store.act("uploadMask")]);
  assert.equal(calls.filter((c) => c[0] === "mask").length, 1);
  handoff.destroy(); createMaskHandoff(store, client, () => "remounted png");
  await store.act("uploadMask"); assert.equal(calls.filter((c) => c[0] === "mask").length, 1);
  change([0, 0]); assert.equal(store.get().maskEmpty, true); assert.equal(store.get().maskVersion, v + 1);
  assert.equal(store.get().mask, null); await assert.rejects(store.act("uploadMask"), /empty/);
});
test("writer gets the canvas mask before building the inpaint body; failed upload never writes", async () => {
  const { store, client, calls, change } = setup();
  await writePrompt(store, client, config);
  assert.deepEqual(calls.map((c) => c[0]), ["mask", "write"]);
  assert.equal(calls[1][1].mask.name, "mask.png");
  await writePrompt(store, client, config); assert.equal(calls.filter((c) => c[0] === "mask").length, 1);
  change([0, 0]); await writePrompt(store, client, config);
  assert.match(store.get().writer.error.message, /empty/); assert.equal(calls.filter((c) => c[0] === "write").length, 2);
});
test("three concurrent jobs; fourth remains local; queued cancel removes without sending", async () => {
  const { store, client, calls } = setup("edit");
  for (let i = 0; i < 3; i++) await runCurrent(store, client);
  const fourth = await runCurrent(store, client);
  assert.equal(store.get().jobs[fourth].state, "queued"); assert.equal(store.get().jobs[fourth].serverId, undefined);
  await cancelJob(store, client, fourth);
  assert.equal(store.get().jobs[fourth], undefined); assert.equal(calls.filter((c) => c[0] === "run").length, 3);
  assert.equal(calls.filter((c) => c[0] === "cancel").length, 0);
});
test("running cancel asks first, remains active until paid result; done/cancelled reread server day cost", async () => {
  const { store, client, calls, server, control } = setup("edit");
  const id = await runCurrent(store, client);
  assert.deepEqual(await cancelJob(store, client, id), { needsConfirm: true });
  assert.equal(calls.filter((c) => c[0] === "cancel").length, 0);
  await cancelJob(store, client, id, true); assert.equal(store.get().jobs[id].cancelledAfterSend, true);
  server[0] = { job_id: "server-0", state: "cancelled", entry: { id: "paid", cost_usd: 0.1, outputs: [{ name: "result.png" }] } };
  await control.message({ type: "luna.job", data: server[0] });
  assert.equal(store.get().jobs[id].entry.outputs[0].name, "result.png"); assert.equal(store.get().dayCost, 9.87);
  const second = await runCurrent(store, client);
  await control.message({ type: "luna.job", data: { job_id: store.get().jobs[second].serverId, state: "done", entry: { cost_usd: 0.2 } } });
  assert.equal(calls.filter((c) => c[0] === "history").length, 2);
});
test("reconnect patches done/lost, leaves local-only queued job alone; ignores foreign events", async () => {
  const { store, server, control } = setup("edit");
  store.set({ jobs: { local: { id: "local", kind: "cloud", state: "queued" },
    a: { id: "a", kind: "cloud", serverId: "a", state: "running" },
    b: { id: "b", kind: "cloud", serverId: "b", state: "running" },
    c: { id: "c", kind: "cloud", serverId: "c", state: "running" },
    lost: { id: "lost", kind: "cloud", serverId: "lost", state: "running" } } });
  server.push({ job_id: "a", state: "done", entry: { cost_usd: 1 } }, { job_id: "b", state: "running" }, { job_id: "c", state: "running" });
  // Keep three slots occupied during terminal patches so the local job remains waiting.
  server.push({ job_id: "other", state: "running" });
  await control.reconnect();
  assert.equal(store.get().jobs.local.state, "queued"); assert.equal(store.get().jobs.lost.error.code, "lost");
  assert.equal(store.get().jobs.a.state, "done");
  await control.message({ type: "luna.job", data: { job_id: "foreign", state: "done" } });
  assert.equal(store.get().jobs.foreign, undefined);
});
test("event before POST response is replayed and terminal patch starts next local job", async () => {
  const { store, client, control, server } = setup("edit");
  client.runCloud = async () => {
    await control.message({ type: "luna.job", data: { job_id: "early", state: "done", entry: { cost_usd: 0.1 } } });
    server.push({ job_id: "early", state: "done" }); return { job_id: "early" };
  };
  const id = await runCurrent(store, client); assert.equal(store.get().jobs[id].state, "done");
  assert.equal(store.get().jobs[id].entry.cost_usd, 0.1);
});
test("batch row summary/cost, expandable children, separate Final, batch cancel uses same confirm", async () => {
  const { store, client, server, calls } = setup("edit");
  const jobs = { a: { id: "a", serverId: "a", kind: "cloud", batch: "batch", model: "one", variant: 0, state: "running", est_cost_usd: 0.1 },
    b: { id: "b", kind: "cloud", batch: "batch", model: "two", variant: 1, state: "queued", est_cost_usd: null },
    done: { id: "done", batch: "batch", model: "one", state: "done", est_cost_usd: 0.1, entry: { cost_usd: 0.25 } },
    final: { id: "final", batch: "batch", tier: "final", state: "done", entry: { cost_usd: 0.5 } } };
  store.set({ jobs, generate: { ...store.get().generate, batch: { batch_id: "batch", idea: "Idea", models: ["one", "two"], count: 2 } } });
  const rows = queueRows(store.get()); assert.equal(rows.length, 2); assert.equal(rows[0].label, "Idea / 2 models · 2 images each");
  assert.equal(rows[0].state, "running"); assert.equal(rows[0].actual, 0.25); assert.equal(rows[0].est_cost_usd, null);
  server.push({ job_id: "a", state: "running" });
  assert.deepEqual(await cancelBatch(store, client, "batch"), { needsConfirm: true });
  await cancelBatch(store, client, "batch", true);
  assert.equal(store.get().jobs.b, undefined); assert.deepEqual(calls.find((c) => c[0] === "cancel")[1], { batch_id: "batch" });
});
test("server-queued cancel reconciles before confirm and waits for authoritative terminal event", async () => {
  const { store, client, server, control } = setup("edit");
  store.set({ jobs: { q: { id: "q", serverId: "q", kind: "cloud", state: "queued" } } });
  server.push({ job_id: "q", state: "running" });
  assert.deepEqual(await cancelJob(store, client, "q"), { needsConfirm: true });
  assert.equal(store.get().jobs.q.state, "running");
  await control.message({ type: "luna.job", data: { job_id: "q", state: "error", error: { code: "refused", message: "No result" } } });
  assert.equal(store.get().jobs.q.error.code, "refused");
});
test("disabled/unsupported runs never send; failed run keeps inputs and surfaces an error row", async () => {
  const { store, client, calls } = setup("edit");
  store.set({ asset: null }); assert.equal((await runCurrent(store, client)).ok, false); assert.equal(calls.length, 0);
  store.set({ asset: { name: "image.png" } }); const inputs = store.get();
  client.runCloud = async () => { throw new Error("refused"); };
  await assert.rejects(runCurrent(store, client), /refused/);
  assert.equal(store.get().asset, inputs.asset); assert.equal(store.get().prompt, inputs.prompt);
  assert.equal(Object.values(store.get().jobs)[0].state, "error");
  store.set({ engine: { ...store.get().engine, kind: "workflow" } });
  await assert.rejects(runCurrent(store, client), /unavailable/);
});
test("queue slot and elapsed labels have no fake percentage", () => {
  assert.equal(SLOT_TABLE.find((r) => r.slot === "queue").mount, mountQueue);
  assert.equal(elapsedLabel({ startedAt: 1000 }, 62000), "1:01");
  assert.equal(elapsedLabel({ startedAt: 1000, finishedAt: 5000 }, 99999), "0:04");
  assert.equal(elapsedLabel({}), "");
});

test("terminal event drains the local queue and stale running events cannot revive a done job", async () => {
  const { store, client, control } = setup("edit");
  const first = await runCurrent(store, client);
  await runCurrent(store, client); await runCurrent(store, client);
  const waiting = await runCurrent(store, client);
  await control.message({ type: "luna.job", data: { job_id: "server-0", state: "done", entry: { cost_usd: 1 } } });
  await new Promise((r) => setImmediate(r));
  assert.equal(store.get().jobs[waiting].serverId, "server-3");
  await control.message({ type: "luna.job", data: { job_id: "server-0", state: "running" } });
  assert.equal(store.get().jobs[first].state, "done");
});
test("server-queued cancellation removes its row and re-reads day cost without marking it paid", async () => {
  const { store, client, control, server, calls } = setup("edit");
  store.set({ jobs: { q: { id: "q", serverId: "q", kind: "cloud", state: "queued" } } });
  server.push({ job_id: "q", state: "queued" });
  client.cancelCloud = async () => { server[0].state = "cancelled"; };
  await cancelJob(store, client, "q");
  assert.equal(store.get().jobs.q, undefined);
  assert.equal(calls.filter((c) => c[0] === "history").length, 1);
  await control.reconnect(); assert.equal(store.get().jobs.q, undefined);
});
test("reopening a fresh store discovers this sid's running and paid completed jobs", async () => {
  const { store, server, control } = setup("edit");
  server.push({ job_id: "active", state: "running" }, { job_id: "paid", state: "cancelled", entry: { model: "model", cost_usd: 0.2 } });
  await control.reconnect();
  assert.equal(store.get().jobs.active.serverId, "active");
  assert.equal(store.get().jobs.paid.entry.cost_usd, 0.2);
});
test("mask edited during import uploads the new revision and never publishes the stale reference", async () => {
  const { store, client, change } = setup();
  let release, count = 0;
  client.importAsset = async () => {
    count++;
    if (count === 1) await new Promise((r) => { release = r; });
    return { ref: { name: count === 1 ? "old.png" : "new.png" } };
  };
  const pending = store.act("uploadMask");
  await new Promise((r) => setImmediate(r));
  change([0, 255]); release();
  const ref = await pending;
  assert.equal(ref.name, "new.png"); assert.equal(store.get().mask.name, "new.png"); assert.equal(count, 2);
});
