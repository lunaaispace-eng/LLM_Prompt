// D2 studio shell. Panels mount from SLOT_TABLE; a slot with no row stays empty.
// A13–A18a each add one row (and, if needed, one id in a tab's `slots` list).
import { clear, el, ensureCss, projectSlug } from "../ui/dom.mjs";
import { mountAssets } from "../ui/assets_panel.mjs";
import { mountCanvas } from "../ui/canvas_view.mjs";
import { mountReferences } from "../ui/references_panel.mjs";
import { mountSettings } from "../ui/settings.mjs";
import { mountWriter } from "../ui/writer_panel.mjs";
import { mountQueue } from "../ui/queue_panel.mjs";
import { mountEngine } from "../ui/engine_panel.mjs";
import { mountHistory } from "../ui/history_panel.mjs";
import { mountGenerate } from "../ui/generate_tab.mjs";

export const USE_IN_GRAPH_REASON =
  "open the studio from a launcher node to send a result to the graph";

// Shell rows only. Later tasks append {slot, module, mount}.
export const SLOT_TABLE = [
  { slot: "settings", module: "ui/settings.mjs", mount: mountSettings },
  { slot: "canvas", module: "ui/canvas_view.mjs", mount: mountCanvas },
  { slot: "writer", module: "ui/writer_panel.mjs", mount: mountWriter },
  { slot: "assets", module: "ui/assets_panel.mjs", mount: mountAssets },
  { slot: "refs", module: "ui/references_panel.mjs", mount: mountReferences },
  { slot: "engine", module: "ui/engine_panel.mjs", mount: mountEngine },
  { slot: "history", module: "ui/history_panel.mjs", mount: mountHistory },
  { slot: "queue", module: "ui/queue_panel.mjs", mount: mountQueue },
  { slot: "generate", module: "ui/generate_tab.mjs", mount: mountGenerate },
];

// Each tab carries its own layout. Stage A has no Packages entry and no placeholder.
export const TABS = [
  { id: "edit", label: "Edit", slots: ["assets", "refs", "canvas", "writer", "engine", "queue", "history"] },
  { id: "generate", label: "Generate", slots: ["refs", "generate", "queue", "history"] },
];

const LABELS = {
  assets: "Assets", refs: "References", canvas: "Canvas", writer: "Writer",
  engine: "Engine", queue: "Run queue", generate: "Generate", history: "History",
};

const ENDED = new Set(["done", "error", "cancelled"]);

export function readDirectorState(node) {
  const widget = node?.widgets?.find((w) => w.name === "director_state");
  if (!widget || !widget.value) return {};
  try {
    const data = JSON.parse(widget.value);
    return data && typeof data === "object" ? data : {};
  } catch {
    return {};
  }
}

export function writeDirectorState(node, state) {
  const widget = node?.widgets?.find((w) => w.name === "director_state");
  if (!widget) return;
  const next = JSON.stringify({
    project: state.project || null,
    selected: state.selected || null,
    prompt: state.prompt || "",
  });
  if (widget.value !== next) widget.value = next;
}

/** The frame action A18 and A18a call. Unbound, it returns the reason and writes nothing. */
export function installFrameActions(store, getNode) {
  store.register("useInGraph", (s, entryId) => {
    const node = getNode?.();
    if (!node) return USE_IN_GRAPH_REASON;
    s.set({ selected: entryId });
    writeDirectorState(node, s.get());
  });
}

function queueCount(state) {
  return Object.values(state.jobs || {}).filter((job) => job && !ENDED.has(job.state)).length;
}

function tabFor(state) {
  const id = state.mode === "generate" ? "generate" : "edit";
  return TABS.find((tab) => tab.id === id) || TABS[0];
}

function focusable(root) {
  return [...root.querySelectorAll("button, [href], input, select, textarea")].filter((node) => {
    if (node.disabled || node.hidden) return false;
    if (node.closest("[hidden]")) return false;
    return true;
  });
}

/**
 * Mount the full-screen studio on document.body.
 * Esc closes. Focus stays inside. The graph keeps running underneath.
 * Returns `{close}`.
 */
export function mountOverlay({ app, api, store, client, socket, node }) {
  void app;
  void api;
  store.boundNode = node || null;
  ensureCss(new URL("../director.css", import.meta.url));

  if (node) {
    const saved = readDirectorState(node);
    store.set({
      project: projectSlug(saved.project || store.get().project),
      selected: saved.selected ?? null,
      prompt: typeof saved.prompt === "string" ? saved.prompt : (store.get().prompt || ""),
    });
  } else if (!store.get().project) {
    store.set({ project: "default" });
  }

  const title = el("h1", { class: "ld-title", text: "Luna Image Studio" });
  const project = el("input", { class: "ld-project-input", type: "text", spellcheck: "false", "aria-label": "project" });
  const projectLabel = el("label", { class: "ld-project" }, ["project: ", project]);
  const tabs = el("div", { class: "ld-tabs", role: "tablist" });
  const badge = el("span", { class: "ld-badge" });
  const settingsBody = el("div", { class: "ld-settings-body" });
  const settingsPanel = el("div", { class: "ld-settings", hidden: true }, [settingsBody]);
  const settingsBtn = el("button", { class: "ld-btn", type: "button", text: "Settings" });
  const closeBtn = el("button", { class: "ld-btn", type: "button", text: "Close" });
  const status = el("div", { class: "ld-status", hidden: true });
  const grid = el("div", { class: "ld-grid" });
  const shell = el("div", { class: "ld-shell" }, [
    el("header", { class: "ld-bar" }, [title, projectLabel, tabs, el("span", { class: "ld-spacer" }), badge, settingsBtn, closeBtn]),
    status,
    grid,
    settingsPanel,
    el("p", { class: "ld-hint", text: "Esc to close" }),
  ]);
  const root = el("div", { class: "luna-director", role: "dialog", "aria-modal": "true", "aria-label": "Luna Image Studio" }, [shell]);

  const settingsRow = SLOT_TABLE.find((row) => row.slot === "settings");
  const settingsHandle = settingsRow ? settingsRow.mount(settingsBody, store, client) : null;
  const gridCleanups = [];
  let closed = false;
  let shownMode = "";

  for (const item of TABS) {
    tabs.append(el("button", {
      class: "ld-tab", type: "button", role: "tab", text: item.label,
      "aria-selected": "false",
      onclick: () => store.act("setMode", item.id),
    }));
  }

  function mountGrid(tab) {
    for (const fn of gridCleanups) fn();
    gridCleanups.length = 0;
    clear(grid);
    grid.dataset.mode = tab.id;
    shownMode = tab.id;
    for (const slot of tab.slots) {
      const body = el("div", { class: "ld-slot-body" });
      const section = el("section", { class: "ld-slot" + (slot === "queue" ? " ld-queue" : ""), "data-slot": slot }, [
        el("header", { text: LABELS[slot] || slot }),
        body,
      ]);
      const row = SLOT_TABLE.find((item) => item.slot === slot);
      if (row) {
        const handle = row.mount(body, store, client);
        if (handle?.destroy) gridCleanups.push(() => handle.destroy());
      } else if (slot === "canvas") {
        body.append(el("p", { class: "ld-empty", text: "drop an image or take it from graph" }));
      }
      grid.append(section);
    }
  }

  function render() {
    const state = store.get();
    if (document.activeElement !== project) project.value = state.project || "";
    badge.textContent = "Queue " + queueCount(state);
    const tab = tabFor(state);
    for (const button of tabs.querySelectorAll(".ld-tab")) {
      button.setAttribute("aria-selected", button.textContent === tab.label ? "true" : "false");
    }
    if (shownMode !== tab.id) mountGrid(tab);
    if (node) writeDirectorState(node, state);
  }

  function setStatus(next) {
    if (next === "open") {
      status.hidden = true;
      status.replaceChildren();
      return;
    }
    status.hidden = false;
    status.className = "ld-status " + (next === "reconnecting" ? "error" : "busy");
    const text = next === "reconnecting"
      ? "socket lost, reconnecting"
      : "connecting to the Director socket…";
    status.replaceChildren(el("span", { class: "ld-spin" }), el("span", { text }));
  }

  function close() {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey, true);
    offStore();
    offSocket?.();
    for (const fn of gridCleanups) fn();
    settingsHandle?.destroy?.();
    root.remove();
  }

  function onKey(ev) {
    if (closed || !root.isConnected) return;
    if (ev.key === "Escape") {
      ev.preventDefault();
      ev.stopPropagation();
      close();
      return;
    }
    if (ev.key !== "Tab" || !root.contains(ev.target)) return;
    const items = focusable(root);
    if (!items.length) {
      ev.preventDefault();
      ev.stopPropagation();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    if (ev.shiftKey && (ev.target === first || !root.contains(document.activeElement))) {
      ev.preventDefault();
      ev.stopPropagation();
      last.focus();
    } else if (!ev.shiftKey && ev.target === last) {
      ev.preventDefault();
      ev.stopPropagation();
      first.focus();
    }
  }

  project.addEventListener("change", () => {
    store.set({ project: projectSlug(project.value) });
  });
  settingsBtn.addEventListener("click", () => {
    settingsPanel.hidden = !settingsPanel.hidden;
    if (!settingsPanel.hidden) settingsPanel.querySelector("input")?.focus();
  });
  closeBtn.addEventListener("click", close);

  const offStore = store.subscribe(() => render());
  const offSocket = socket?.subscribe?.(setStatus);
  if (!socket) setStatus("open");
  render();

  document.body.append(root);
  document.addEventListener("keydown", onKey, true);
  closeBtn.focus();

  return { close };
}
