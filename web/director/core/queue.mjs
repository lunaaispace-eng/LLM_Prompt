// Job queue: state machine and per-kind concurrency. Pure, no DOM.
// States: queued, uploading, submitted, running, finishing, done, error, cancelled.
// Cloud jobs use queued, uploading, running, done, error, cancelled; stage B package jobs also use
// submitted and finishing.
const ENDED = new Set(["done", "error", "cancelled"]);
const ACTIVE = new Set(["uploading", "submitted", "running", "finishing"]);

export function createQueue({ cloudParallel = 3, now = Date.now } = {}) {
  // Per-kind limits. Stage B adds `package: 1` here.
  const limits = { cloud: cloudParallel };
  let list = [];

  const kindOf = (j) => j.kind || "cloud";
  const get = (id) => list.find((j) => j.id === id);
  const activeCount = (kind) => list.filter((j) => kindOf(j) === kind && ACTIVE.has(j.state)).length;

  function add(job) {
    const j = { state: "queued", kind: "cloud", ...job };
    list.push(j);
    return j;
  }

  function addBatch(batchId, jobs) {
    return jobs.map((job) => add({ ...job, batch: batchId }));
  }

  function next() {
    for (const j of list) {
      if (j.state !== "queued") continue;
      const kind = kindOf(j);
      const limit = limits[kind] ?? 1;
      if (activeCount(kind) >= limit) continue;
      j.state = "uploading";
      if (j.startedAt == null) j.startedAt = now();
      return j;
    }
    return null;
  }

  function update(id, patch) {
    const j = get(id);
    if (!j) return null;
    Object.assign(j, patch);
    return j;
  }

  // Removes the queued jobs; flags the sent ones so their paid image is kept (S5).
  function cancelBatch(batchId) {
    const out = [];
    const keep = [];
    for (const j of list) {
      if (j.batch !== batchId) { keep.push(j); continue; }
      if (j.state === "queued") {
        out.push({ id: j.id, action: "removed" });
        continue;
      }
      keep.push(j);
      if (ACTIVE.has(j.state)) {
        j.cancelledAfterSend = true;
        out.push({ id: j.id, action: "flagged" });
      }
    }
    list = keep;
    return out;
  }

  function batchState(jobs) {
    if (jobs.some((j) => ACTIVE.has(j.state))) return "running";
    if (jobs.some((j) => j.state === "queued")) return "queued";
    if (jobs.some((j) => j.state === "done")) return "done";
    if (jobs.some((j) => j.state === "error")) return "error";
    return "cancelled";
  }

  function batches() {
    const order = [];
    const map = new Map();
    for (const j of list) {
      if (j.batch == null) continue;
      if (!map.has(j.batch)) { map.set(j.batch, []); order.push(j.batch); }
      map.get(j.batch).push(j);
    }
    return order.map((batchId) => ({ batchId, jobs: map.get(batchId), state: batchState(map.get(batchId)) }));
  }

  return { limits, add, addBatch, next, update, get, cancelBatch, batches, jobs: () => list.slice() };
}

export { ENDED, ACTIVE };
