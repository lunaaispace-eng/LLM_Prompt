// Run orchestration and socket bridge; core queue/events remain the state-machine authorities.
import { createQueue, ACTIVE, ENDED } from "../core/queue.mjs";
import { onJobEvent, reconcileCloud } from "../core/events.mjs";
import { cloudCanRun, cloudSpec } from "./engine_panel.mjs";
const controllers = new WeakMap();
let serial = 0;
const jobsOf = (store) => Object.values(store.get().jobs || {});
const failure = (e) => ({ code: e.code || "error", message: e.message || String(e) });
const queueOf = (store) => {
  const queue = createQueue();
  for (const job of jobsOf(store)) {
    if (job.state === "queued" && !job.serverId && !job.body) continue;
    queue.add({ ...job, state: job.serverId && job.state === "queued" ? "uploading" : job.state });
  }
  return queue;
};
function put(store, job) { store.set({ jobs: { ...store.get().jobs, [job.id]: job } }); }
function remove(store, id) {
  const jobs = { ...store.get().jobs }; delete jobs[id]; store.set({ jobs });
}

export async function refreshDayCost(store, client) {
  const project = store.get().project;
  try {
    const result = await client.history(project);
    store.set({ dayCost: result.day_cost, dayCostError: null });
  } catch (e) { store.set({ dayCostError: failure(e) }); }
}

export function installQueueActions(store, client) {
  if (controllers.has(store)) return controllers.get(store);
  const early = new Map();
  let costRefresh = Promise.resolve(), reconciling = 0;
  function rereadCost() {
    costRefresh = costRefresh.then(() => refreshDayCost(store, client));
    return costRefresh;
  }
  async function patch(result) {
    if (!result) return;
    const job = jobsOf(store).find((j) => j.serverId === result.jobId);
    if (!job) return;
    const patch = result.patch;
    if (ENDED.has(job.state) && patch.state && !ENDED.has(patch.state)) return;
    const next = { ...job, ...patch };
    if (patch.state === "running" && job.startedAt == null) next.startedAt = Date.now();
    if (ENDED.has(patch.state)) next.finishedAt = Date.now();
    // Queued cancellations have no paid entry and disappear from the queue.
    if (patch.state === "cancelled" && !patch.entry && !job.startedAt) remove(store, job.id);
    else put(store, next);
    if (!reconciling && ENDED.has(patch.state)) pump();
    if (patch.state === "done" || patch.state === "cancelled" || patch.entry) await rereadCost();
  }
  async function submit(job) {
    try {
      const result = await client.runCloud(job.body);
      const current = store.get().jobs[job.id];
      put(store, { ...current, serverId: result.job_id, est_cost_usd: result.est_cost_usd, state: "uploading", startedAt: null });
      const messages = early.get(result.job_id) || [];
      early.delete(result.job_id);
      for (const msg of messages) await message(msg);
      // The server can start/finish before the POST reply arrives, or while the socket is down.
      await reconnect(false);
      return job.id;
    } catch (e) {
      put(store, { ...store.get().jobs[job.id], state: "error", error: failure(e), finishedAt: Date.now() });
      pump();
      throw e;
    }
  }
  function pump(wanted) {
    const queue = queueOf(store);
    let job, result;
    while ((job = queue.next())) {
      put(store, { ...job, startedAt: null });
      const pending = submit(job);
      if (job.id === wanted) result = pending;
      else pending.catch(() => {}); // failure is visible on its row
    }
    return result;
  }
  async function message(msg) {
    const result = onJobEvent(msg, new Set(jobsOf(store).map((j) => j.serverId).filter(Boolean)));
    if (result) return patch(result);
    if (msg?.type === "luna.job" && jobsOf(store).some((j) => j.state === "uploading")) {
      if (early.size >= 100) early.delete(early.keys().next().value);
      const list = early.get(msg.data?.job_id) || [];
      early.set(msg.data?.job_id, [...list.slice(-7), msg]);
    }
  }
  async function reconnect(discover = true, drain = true) {
    reconciling++;
    try {
      const result = await client.cloudJobs(store.get().socketSid);
      const server = result.jobs || [];
      if (discover) for (const s of server) {
        if (s.state === "cancelled" && !s.entry) continue;
        if (!jobsOf(store).some((j) => j.serverId === s.job_id)) {
          put(store, { id: s.job_id, serverId: s.job_id, kind: "cloud", state: s.state,
            project: s.entry?.project || store.get().project, entry: s.entry, error: s.error, model: s.entry?.model,
            operation: s.entry?.operation, est_cost_usd: s.entry?.est_cost_usd, startedAt: s.state === "running" ? Date.now() : null });
        }
      }
      for (const update of reconcileCloud(jobsOf(store).filter((j) => j.kind === "cloud"), server)) await patch(update);
      for (const s of server) {
        const local = jobsOf(store).find((j) => j.serverId === s.job_id);
        if (local && !ENDED.has(local.state) && !ENDED.has(s.state)) await message({ type: "luna.job", data: s });
      }
      store.set({ queueError: null });
    } catch (e) { store.set({ queueError: failure(e) }); }
    finally { reconciling--; if (drain && !reconciling) pump(); }
  }
  async function replay(ids) {
    for (const id of ids) {
      const messages = early.get(id) || []; early.delete(id);
      for (const msg of messages) await message(msg);
    }
  }
  const control = { pump, message, reconnect, patch, rereadCost, replay };
  controllers.set(store, control);
  store.register("runCurrent", () => runCurrent(store, client));
  store.register("cancelJob", (_s, id, confirmed) => cancelJob(store, client, id, confirmed));
  store.register("cancelBatch", (_s, id, confirmed) => cancelBatch(store, client, id, confirmed));
  return control;
}

export async function runCurrent(store, client) {
  const control = installQueueActions(store, client);
  if (store.get().engine.kind !== "cloud") throw new Error("Engine unavailable");
  const allowed = cloudCanRun(store.get());
  if (!allowed.ok) return allowed;
  const config = store.get().cloudConfig || await client.config();
  if (store.get().engine.params.operation === "inpaint") await store.act("uploadMask");
  const state = store.get();
  const id = `cloud-${++serial}`;
  put(store, { id, kind: "cloud", state: "queued", model: state.engine.model, operation: state.engine.params.operation,
    project: state.project, body: { sid: state.socketSid, project: state.project, spec: cloudSpec(state, config) } });
  return await control.pump(id) || id;
}

// Batch expansion/concurrency live on the server. Register its jobs in the same queue/socket bridge.
async function acceptSubmission(store, client, send, metadata, batchInfo = null) {
  const control = installQueueActions(store, client), state = store.get();
  const pendingId = `cloud-submit-${++serial}`;
  put(store, { id: pendingId, kind: "cloud", state: "uploading", project: state.project, ...metadata });
  try {
    const result = await send(state.socketSid, state.project);
    const queue = createQueue();
    const jobs = (result.jobs || [result]).map((job) => ({
      ...metadata, id: job.job_id, serverId: job.job_id, kind: "cloud", state: "uploading",
      project: state.project, model: job.model || metadata.model, variant: job.variant ?? metadata.variant,
      est_cost_usd: job.est_cost_usd, startedAt: null, batchInfo,
    }));
    if (batchInfo) {
      batchInfo = { ...batchInfo, batch_id: result.batch_id };
      for (const job of jobs) job.batchInfo = batchInfo;
      queue.addBatch(result.batch_id, jobs);
    } else for (const job of jobs) queue.add(job);
    const next = { ...store.get().jobs }; delete next[pendingId];
    for (const job of queue.jobs()) next[job.id] = job;
    store.set({ jobs: next, ...(batchInfo ? { generate: { ...store.get().generate, batch: batchInfo, selected: null } } : {}) });
    await control.replay(jobs.map((job) => job.serverId));
    await control.reconnect(false);
    return result;
  } catch (e) {
    remove(store, pendingId);
    throw e;
  }
}

export function submitBatch(store, client, batch) {
  return acceptSubmission(store, client, (sid, project) => client.runBatch(sid, project, batch), {},
    { idea: batch.request, models: batch.models, count: batch.count, tier: batch.tier });
}

export function submitFinal(store, client, entry, choice) {
  return acceptSubmission(store, client, (sid, project) => client.final(sid, project, entry.id, choice),
    { model: entry.model, batch: entry.batch, variant: entry.variant, tier: "final", operation: "edit" });
}

export async function cancelJob(store, client, id, confirmed = false) {
  const control = installQueueActions(store, client);
  let job = store.get().jobs[id];
  if (!job || ENDED.has(job.state)) return false;
  // Refresh a server-queued row before deciding whether it needs the running confirmation.
  if (job.serverId && job.state === "queued") { await control.reconnect(false, false);
    if (store.get().queueError) throw new Error("Refresh the queue before cancelling this job");
    job = store.get().jobs[id]; }
  if (!job || ENDED.has(job.state)) return false;
  if (job.state !== "queued" && !confirmed) return { needsConfirm: true };
  if (job.state === "uploading" && !job.serverId) return { retry: true }; // POST is in flight; wait for its server id
  if (job.serverId) await client.cancelCloud({ job_id: job.serverId });
  job = store.get().jobs[id];
  if (!job) return true;
  if (job.state === "queued" && !job.serverId) remove(store, id);
  else if (!ENDED.has(job.state)) put(store, { ...job, cancelledAfterSend: true });
  if (job.serverId) await control.reconnect(false);
  control.pump();
  return true;
}

export async function cancelBatch(store, client, batchId, confirmed = false) {
  const control = installQueueActions(store, client);
  await control.reconnect(false, false);
  if (store.get().queueError) throw new Error("Refresh the queue before cancelling this batch");
  const queue = createQueue();
  for (const job of jobsOf(store)) queue.add(job);
  const group = queue.batches().find((b) => b.batchId === batchId);
  if (!group) return false;
  if (group.jobs.some((j) => ACTIVE.has(j.state)) && !confirmed) return { needsConfirm: true };
  if (group.jobs.some((j) => j.state === "uploading" && !j.serverId)) return { retry: true };
  if (group.jobs.some((j) => j.serverId && !ENDED.has(j.state))) await client.cancelCloud({ batch_id: batchId });
  const outcomes = queue.cancelBatch(batchId);
  // A server-queued job may have started meanwhile: keep it until the server confirms cancellation.
  for (const outcome of outcomes) {
    const job = store.get().jobs[outcome.id];
    if (outcome.action === "removed" && !job.serverId) remove(store, job.id);
    else if (!ENDED.has(job.state)) put(store, { ...job, cancelledAfterSend: true });
  }
  await control.reconnect(false); control.pump();
  return true;
}
