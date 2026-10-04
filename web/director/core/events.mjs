// Director socket events. Stage A: `luna.job` events and the cloud reconcile. Pure, no DOM.
const ENDED = new Set(["done", "error", "cancelled"]);
const PASS = ["state", "entry", "error", "batch", "variant", "model"];

const has = (ids, id) => (ids instanceof Set ? ids.has(id) : Array.isArray(ids) && ids.includes(id));

// msg: {type: "luna.job", data: {job_id, state, entry?, error?, batch?, variant?, model?}}
// liveJobIds: server job ids that are live in this tab.
export function onJobEvent(msg, liveJobIds) {
  if (!msg || msg.type !== "luna.job" || !msg.data) return null;
  const d = msg.data;
  if (!d.job_id || !has(liveJobIds, d.job_id)) return null;
  const patch = {};
  for (const k of PASS) if (d[k] !== undefined && d[k] !== null) patch[k] = d[k];
  return { jobId: d.job_id, patch };
}

// Reconcile local cloud jobs with the server's table (GET /luna/studio/jobs). Only jobs that carry a
// server id are touched (pre-flight R-10); a job still in the local queue has none.
export function reconcileCloud(localJobs, serverJobs) {
  const byId = new Map((serverJobs || []).map((s) => [s.job_id, s]));
  const out = [];
  for (const j of localJobs || []) {
    if (!j.serverId || ENDED.has(j.state)) continue;
    const s = byId.get(j.serverId);
    if (!s) {
      out.push({ jobId: j.serverId, patch: { state: "error", error: { code: "lost", message: "The server no longer knows this job (ComfyUI restarted?)." } } });
    } else if (ENDED.has(s.state)) {
      const patch = { state: s.state };
      if (s.entry) patch.entry = s.entry;
      if (s.error) patch.error = s.error;
      out.push({ jobId: j.serverId, patch });
    }
  }
  return out;
}
