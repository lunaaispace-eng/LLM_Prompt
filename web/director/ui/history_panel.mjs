// History strip, fold-out details, and the actions on a version. Compare renders over the canvas.
import { el, ensureCss } from "./dom.mjs";
import { assetUrl } from "./tools_bar.mjs";
import { ORIGINAL, chainDepth } from "../core/history.mjs";
import {
  USE_IN_GRAPH_REASON, NO_ORIGINAL_REASON, historyEntries, stripRows, versionLabel,
  showRestart, restartHint, restartPatch, editHerePatch, applyRerun, historyPatch, statusLabel,
  isBilledEntry, entryThumb, detailFields, resolvePair, mountCompare, rerunButton,
} from "./compare.mjs";

export {
  RUN_QUEUE_REASON, USE_IN_GRAPH_REASON, NO_ORIGINAL_REASON, historyEntries, stripRows, versionLabel,
  showRestart, restartHint, restartPatch, editHerePatch, rerunPatch, rerunButton, applyRerun,
  historyPatch, money, statusLabel, entryThumb, detailFields,
} from "./compare.mjs";

function byId(entries, id) {
  return (entries || []).find((e) => e && e.id === id) || null;
}

function useInGraphReason(store) {
  return store.boundNode ? "" : USE_IN_GRAPH_REASON;
}

export function mountHistory(root, store, client) {
  ensureCss(new URL("./history_panel.css", import.meta.url));
  const hint = el("p", { class: "ld-restart-hint", hidden: true });
  const empty = el("p", { class: "ld-empty", text: "No history yet. Run an edit." });
  const strip = el("div", { class: "ld-history-strip", role: "list" });
  const showHidden = el("input", { type: "checkbox" });
  const error = el("p", { class: "ld-history-error", role: "alert" });
  const details = el("div", { class: "ld-history-details" });
  const panel = el("div", { class: "ld-history" }, [
    el("div", { class: "ld-history-main" }, [
      hint, strip, empty,
      el("label", { class: "ld-history-hidden" }, [showHidden, " Show hidden"]),
      error,
    ]),
    details,
  ]);
  root.append(panel);

  let alive = true;
  let seq = 0;
  let projectSeen = null;
  let draftFor = null;
  let draftPrompt = "";
  let compareHost = null;
  const promptBox = el("textarea", { class: "ld-history-prompt", rows: "3", "aria-label": "prompt" });
  const noteBox = el("input", { type: "text", "aria-label": "note" });

  function setCompare(part) {
    const c = store.get().compare || { on: false, a: null, b: null };
    store.set({ compare: { on: !!c.on, a: c.a ?? null, b: c.b ?? null, ...part } });
  }

  function replaceEntry(id, patch) {
    store.set({ history: store.get().history.map((e) => (e.id === id ? { ...e, ...patch } : e)) });
  }

  async function sendPatch(entry, patch) {
    const body = historyPatch(patch);
    error.textContent = "";
    if (!client?.patchHistory || !store.get().project) {
      error.textContent = "history is not loaded";
      return;
    }
    try {
      await client.patchHistory(store.get().project, entry.id, body);
      if (alive) replaceEntry(entry.id, body);
    } catch (err) {
      if (alive) error.textContent = err.message || "could not update the entry";
    }
  }

  function renderDetails(entry) {
    details.replaceChildren();
    if (!entry) {
      details.append(el("p", { class: "ld-muted", text: "Select a version." }));
      return;
    }
    if (draftFor !== entry.id) {
      draftFor = entry.id;
      draftPrompt = entry.prompt || "";
    }
    const state = store.get();
    const run = rerunButton(typeof store.has === "function" && store.has("runCurrent"));
    const restart = restartPatch(state.history, entry.id, state);
    const graphReason = useInGraphReason(store);
    if (document.activeElement !== promptBox) promptBox.value = draftPrompt;
    if (document.activeElement !== noteBox) noteBox.value = entry.note || "";
    const rerun = el("button", {
      type: "button", class: "ld-btn ld-history-primary", text: "Re-run",
      disabled: run.disabled, title: run.reason,
    });
    const restartBtn = el("button", {
      type: "button", class: "ld-btn", text: "Restart from original",
      hidden: !showRestart(state.history, entry.id),
      disabled: !restart, title: restart ? "" : NO_ORIGINAL_REASON,
    });
    const graphBtn = el("button", {
      type: "button", class: "ld-btn", text: "Use in graph",
      disabled: !!graphReason, title: graphReason,
    });
    const editBtn = el("button", { type: "button", class: "ld-btn", text: "Edit from here" });
    const starBtn = el("button", {
      type: "button", class: "ld-btn", text: entry.star ? "Starred" : "Star",
      "aria-pressed": entry.star ? "true" : "false",
    });
    const hideBtn = el("button", {
      type: "button", class: "ld-btn", text: entry.hidden ? "Unhide" : "Hide",
    });
    rerun.addEventListener("click", () => {
      const result = applyRerun(store, entry, promptBox.value);
      if (result.disabled) error.textContent = result.reason;
    });
    restartBtn.addEventListener("click", () => {
      const patch = restartPatch(store.get().history, entry.id, store.get());
      if (patch) store.set(patch);
    });
    editBtn.addEventListener("click", () => store.set(editHerePatch(entry, store.get())));
    graphBtn.addEventListener("click", () => {
      try {
        const reason = store.act("useInGraph", entry.id);
        if (reason) error.textContent = String(reason);
      } catch (err) {
        error.textContent = /unknown store action/.test(err.message) ? USE_IN_GRAPH_REASON : err.message;
      }
    });
    starBtn.addEventListener("click", () => sendPatch(entry, { star: !entry.star }));
    hideBtn.addEventListener("click", () => sendPatch(entry, { hidden: !entry.hidden }));

    const list = el("dl", { class: "ld-history-fields" });
    for (const item of detailFields(entry)) {
      if (item.id === "prompt") continue;
      list.append(el("dt", { text: item.label }), el("dd", { text: item.value }));
    }
    const head = [el("p", { class: "ld-history-kicker", text: versionLabel(state.history, entry) })];
    if (entry.status === "cancelled" || isBilledEntry(entry)) {
      head.push(el("p", { class: "ld-history-cancelled", text: statusLabel(entry) }));
    }
    details.append(
      ...head, list,
      el("label", { class: "ld-field" }, [el("span", { text: "prompt" }), promptBox]),
      el("p", { class: "ld-muted ld-history-rerun-note", text: "edited prompt → new child entry" }),
      el("div", { class: "ld-history-actions" }, [rerun, editBtn, restartBtn, graphBtn, starBtn, hideBtn]),
      el("label", { class: "ld-field" }, [el("span", { text: "note" }), noteBox]),
    );
  }

  function render() {
    const state = store.get();
    const rows = stripRows(state.history, { includeHidden: showHidden.checked });
    const selected = byId(state.history, state.selected);
    const pair = resolvePair(state.history, state);
    const depth = selected ? chainDepth(state.history, selected.id) : 0;
    hint.hidden = !showRestart(state.history, state.selected);
    hint.textContent = restartHint(depth);
    empty.hidden = rows.length > 0;
    strip.replaceChildren(...rows.map((row) => {
      const thumb = entryThumb(row.entry);
      const img = thumb
        ? el("img", { alt: "", src: assetUrl(thumb, client), draggable: "false" })
        : el("span", { class: "ld-history-blank", text: row.synthetic ? "original" : "—" });
      const current = !row.synthetic && row.id === state.selected;
      const side = pair && (pair.a?.id === row.id ? "a" : pair.b?.id === row.id ? "b" : "");
      const button = el("button", {
        type: "button", class: "ld-hthumb", "aria-current": current ? "true" : "false",
        "data-side": side, title: row.label,
      }, [img]);
      button.addEventListener("click", () => {
        if (row.synthetic) {
          setCompare({ a: ORIGINAL, b: store.get().selected || store.get().compare?.b || null });
          return;
        }
        const c = store.get().compare || {};
        store.set({ selected: row.id, compare: { on: !!c.on, a: null, b: null } });
      });
      const pick = (key) => el("button", {
        type: "button", class: "ld-hpick", text: key.toUpperCase(), "aria-label": "compare " + key,
        "aria-pressed": (store.get().compare || {})[key] === row.id ? "true" : "false",
        onclick: () => setCompare({ [key]: row.id }),
      });
      return el("div", {
        class: "ld-hitem" + (row.entry?.star ? " starred" : "") + (row.entry?.hidden ? " hidden-entry" : ""),
        role: "listitem", "data-id": row.id, "data-depth": String(row.depth),
        style: "margin-top:" + (row.depth * 14) + "px",
      }, [
        button,
        el("span", { class: "ld-hlabel", text: row.label }),
        row.entry?.status === "cancelled" || isBilledEntry(row.entry)
          ? el("span", { class: "ld-hstatus", text: statusLabel(row.entry) }) : null,
        el("span", { class: "ld-hpicks" }, [pick("a"), pick("b")]),
      ]);
    }));
    renderDetails(selected);
    attachCompare();
  }

  function attachCompare() {
    if (compareHost || !root.isConnected) return;
    const stage = root.closest(".ld-grid")?.querySelector("[data-slot=canvas] .ld-stage");
    if (stage) compareHost = mountCompare(stage, store, client);
  }

  async function load(project) {
    const n = ++seq;
    if (!project || !client?.history) return;
    try {
      const data = await client.history(project);
      if (!alive || n !== seq) return;
      const patch = { history: historyEntries(data) };
      if (data && typeof data.day_cost === "number") patch.dayCost = data.day_cost;
      error.textContent = "";
      store.set(patch);
    } catch (err) {
      if (alive && n === seq) error.textContent = err.message || "could not load history";
    }
  }

  // A job that ends (done / error / cancelled) has just written a ledger entry: reload, as on a project change.
  let endedSeen = "";
  function onStore() {
    const s = store.get();
    const project = s.project || "";
    const ended = Object.values(s.jobs || {}).filter((j) => j && /^(done|error|cancelled)$/.test(j.state))
      .map((j) => j.id).sort().join(",");
    if (project !== projectSeen || ended !== endedSeen) {
      projectSeen = project;
      endedSeen = ended;
      load(project);
    }
    render();
  }

  const prevRegister = store.register;
  function wrappedRegister(name, fn) {
    const out = prevRegister.call(store, name, fn);
    if (name === "runCurrent" && alive) render();
    return out;
  }
  store.register = wrappedRegister;
  promptBox.addEventListener("input", () => { draftPrompt = promptBox.value; });
  noteBox.addEventListener("change", () => {
    const entry = byId(store.get().history, draftFor);
    if (entry && (entry.note || "") !== noteBox.value) sendPatch(entry, { note: noteBox.value });
  });
  showHidden.addEventListener("change", render);
  const off = store.subscribe(onStore);
  onStore();
  queueMicrotask(attachCompare);

  return {
    destroy() {
      alive = false;
      off();
      compareHost?.destroy();
      if (store.register === wrappedRegister) store.register = prevRegister;
    },
  };
}
