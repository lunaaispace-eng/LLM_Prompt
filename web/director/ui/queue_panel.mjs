import { el, ensureCss } from "./dom.mjs";
import { createQueue, ACTIVE } from "../core/queue.mjs";
import { batchSummary } from "../core/generate.mjs";
import { installQueueActions } from "./run_queue.mjs";
export { runCurrent, cancelJob, cancelBatch, refreshDayCost, installQueueActions } from "./run_queue.mjs";
const money = (n, fallback = "—") => Number.isFinite(n) ? `$${n.toFixed(2)}` : fallback;
export function elapsedLabel(job, now = Date.now()) {
  if (!Number.isFinite(job.startedAt)) return "";
  const seconds = Math.max(0, Math.floor(((job.finishedAt ?? now) - job.startedAt) / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
export function queueRows(state) {
  const queue = createQueue(), jobs = Object.values(state.jobs || {});
  for (const job of jobs) queue.add(job);
  const batches = new Map(queue.batches().map((b) => [b.batchId, b]));
  const seen = new Set(), rows = [];
  for (const job of jobs) {
    if (!job.batch || job.tier === "final" || job.entry?.tier === "final") { rows.push(job); continue; }
    if (seen.has(job.batch)) continue;
    seen.add(job.batch);
    const group = batches.get(job.batch), children = group.jobs.filter((j) => j.tier !== "final" && j.entry?.tier !== "final");
    const q = createQueue(); q.addBatch(job.batch, children);
    const batch = job.batchInfo || (state.generate?.batch?.batch_id === job.batch || state.generate?.batch?.id === job.batch
      ? state.generate.batch : {}) || {};
    const info = { idea: batch.idea || job.idea || "Batch", models: batch.models || [...new Set(children.map((j) => j.model))],
      count: batch.count || Math.max(1, ...children.map((j) => (j.variant ?? 0) + 1)) };
    const estimates = children.map((j) => j.est_cost_usd);
    const paid = children.map((j) => j.entry?.cost_usd).filter(Number.isFinite);
    rows.push({ id: job.batch, batchId: job.batch, label: batchSummary(info), jobs: children,
      state: q.batches()[0].state, startedAt: Math.min(...children.map((j) => j.startedAt).filter(Number.isFinite)),
      finishedAt: children.every((j) => j.finishedAt != null) ? Math.max(...children.map((j) => j.finishedAt)) : null,
      est_cost_usd: estimates.every(Number.isFinite) ? estimates.reduce((a, b) => a + b, 0) : null,
      actual: paid.length ? paid.reduce((a, b) => a + b, 0) : null });
  }
  return rows;
}

export function mountQueue(root, store, client) {
  ensureCss(new URL("./queue_panel.css", import.meta.url));
  const control = installQueueActions(store, client), expanded = new Set();
  let alive = true, confirmation = null, error = "";
  const panel = el("div", { class: "ld-run-queue" }), body = el("div"), footer = el("div", { class: "ld-queue-cost" });
  const popup = el("div", { class: "ld-queue-confirm", role: "alertdialog", "aria-label": "Cancel running job?", hidden: true });
  const failure = el("p", { role: "alert", class: "ld-queue-error" });
  panel.append(el("p", { class: "ld-muted", text: "cloud: up to 3 at once" }),
    el("div", { class: "ld-queue-row ld-muted" }, ["job", "state / elapsed", "estimate", "actual", ""]), body, popup, failure, footer);
  root.append(panel);
  async function cancel(row, confirmed = false) {
    try {
      const result = await store.act(row.batchId ? "cancelBatch" : "cancelJob", row.id, confirmed);
      confirmation = result?.needsConfirm ? row : null;
      error = result?.retry ? "Submission in flight; try Cancel again once it reaches the server." : "";
    } catch (_) { error = "Cancel failed; try again."; }
    if (alive) render();
  }
  function rowNode(job, child = false) {
    const label = job.label || `${job.model || "cloud job"} · ${job.operation || job.entry?.operation || "run"}`;
    const name = job.batchId ? el("button", { type: "button", class: "ld-btn", text: `${expanded.has(job.id) ? "▾" : "▸"} ${label}`,
      "aria-expanded": expanded.has(job.id) ? "true" : "false", onclick: () => {
        if (expanded.has(job.id)) expanded.delete(job.id); else expanded.add(job.id); render();
      } }) : el("span", { text: label });
    const running = ACTIVE.has(job.state);
    const state = el("div", {}, [el("span", { text: `${job.state} ${elapsedLabel(job)}`.trim() })]);
    if (running) state.append(el("div", { class: "ld-queue-progress", role: "progressbar", "aria-label": "Cloud job running" }, [el("span")]));
    if (job.state === "cancelled" && (job.entry || job.cancelledAfterSend)) {
      state.append(el("small", { text: "cancelled — saved to history, cost counted" }));
    } else if (job.cancelledAfterSend) {
      state.append(el("small", { text: "Cancel requested; the provider still bills a call already sent." }));
    }
    if (job.error) state.append(el("small", { class: "ld-queue-error", text: `${job.error.code}: ${job.error.message}` }));
    const action = running || job.state === "queued" ? el("button", { type: "button", class: "ld-btn", text: "Cancel", onclick: () => { void cancel(job); } }) : "";
    return el("div", { class: `ld-queue-row${child ? " ld-queue-child" : ""}` }, [name, state,
      money(job.est_cost_usd, "after run"), money(job.actual ?? job.entry?.cost_usd), action]);
  }
  function render() {
    if (!alive) return;
    const s = store.get(), rows = queueRows(s);
    body.replaceChildren(...(rows.length ? rows.flatMap((job) => [rowNode(job),
      ...(job.batchId && expanded.has(job.id) ? job.jobs.map((j) => rowNode(j, true)) : [])]) : [el("p", { text: "No runs yet" })]));
    popup.hidden = !confirmation;
    popup.replaceChildren();
    if (confirmation) popup.append(el("strong", { text: "Cancel running job?" }),
      el("p", { text: "A call already sent is saved to history marked cancelled, with its image and cost. The provider still bills it." }),
      el("button", { type: "button", class: "ld-btn", text: "Keep running", onclick: () => { confirmation = null; render(); } }),
      el("button", { type: "button", class: "ld-btn", text: "Cancel job", onclick: () => { void cancel(confirmation, true); } }));
    footer.replaceChildren(el("strong", { text: `today: ${money(s.dayCost)}` }), el("small", { text: "sum of actual cost, from history" }),
      el("button", { type: "button", class: "ld-btn", text: "Refresh", onclick: () => { void control.reconnect().then(() => control.rereadCost()); } }));
    failure.textContent = error || (s.queueError ? "Queue reconciliation failed; refresh to retry." : "")
      || (s.dayCostError ? "Day total unavailable; refresh to retry." : "");
    failure.hidden = !failure.textContent;
  }
  const off = store.subscribe(render), timer = setInterval(render, 1000);
  render(); void control.rereadCost();
  return { destroy() { alive = false; off(); clearInterval(timer); panel.remove(); } };
}
