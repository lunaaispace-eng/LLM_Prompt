// Client for the Director's /luna/* routes, over an injected fetchApi. Pure, no DOM globals.
// An {error:{code,message}} reply becomes a thrown Error carrying `code`.
const enc = encodeURIComponent;

export function createClient(fetchApi, apiBase = "") {
  const url = (p) => apiBase + p;

  async function parse(res) {
    let data = null;
    try { data = await res.json(); } catch (_) { /* non-JSON reply */ }
    if (data && data.error) {
      const e = new Error(data.error.message || String(data.error.code || "error"));
      e.code = data.error.code;
      e.status = res.status;
      throw e;
    }
    if (res && res.ok === false) {
      const e = new Error("request failed (" + res.status + ")");
      e.code = "http_" + res.status;
      e.status = res.status;
      throw e;
    }
    return data;
  }

  const get = async (path) => parse(await fetchApi(url(path), { method: "GET" }));
  const post = async (path, body) => parse(await fetchApi(url(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }));

  return {
    config: () => get("/luna/director/config"),
    write: (body) => post("/luna/director/write", body),
    runCloud: (body) => post("/luna/studio/run", body),
    cancelCloud: (ids) => post("/luna/studio/cancel", ids), // {job_id} or {batch_id}
    cloudJobs: (sid) => get("/luna/studio/jobs?sid=" + enc(sid)),

    history: (project, limit) =>
      get("/luna/director/history?project=" + enc(project) + (limit != null ? "&limit=" + enc(limit) : "")),
    entry: (project, id) => get("/luna/director/history/" + enc(id) + "?project=" + enc(project)),
    patchHistory: (project, id, patch) => post("/luna/director/history/" + enc(id), { project, patch }),

    importAsset(blob, project) {
      const form = new FormData();
      form.append("image", blob, blob.name || "image.png");
      form.append("project", project);
      return fetchApi(url("/luna/director/asset"), { method: "POST", body: form }).then(parse);
    },
    importFromRef: (ref, project) => post("/luna/director/asset", { project, from_ref: ref }),
    resize: (project, refs, state, dryRun) =>
      post("/luna/director/resize", { project, refs, state, dry_run: !!dryRun }),
    viewUrl: (ref) => url("/view?filename=" + enc(ref.name) + "&subfolder=" + enc(ref.subfolder || "")
      + "&type=" + enc(ref.type || "input")),

    // S10 generate
    runBatch: (sid, project, batch) => post("/luna/studio/batch", { sid, project, batch }),
    // Takes {batch} or {spec}; a bare batch object is wrapped.
    estimate: (batchOrSpec) => post("/luna/studio/estimate",
      batchOrSpec && (batchOrSpec.batch || batchOrSpec.spec) ? batchOrSpec : { batch: batchOrSpec }),
    final: (sid, project, entryId, choice) =>
      post("/luna/studio/final", { sid, project, entry_id: entryId, choice }),
  };
}
