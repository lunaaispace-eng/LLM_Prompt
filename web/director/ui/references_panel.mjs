// References: role labels, drag order is the image number, past-the-limit rows stay but are not sent.
import { el } from "./dom.mjs";
import { limitFor } from "../core/generate.mjs";
import { refLimit, sentRefs } from "../core/writer_req.mjs";
import { busyText, clipboardFiles, enqueueImport, importBatch, mergeImported } from "./assets_panel.mjs";

export function modelOf(state) {
  if (state?.mode === "generate") {
    const model = state.generate?.models?.[0];
    return (typeof model === "string" ? model : model?.id) || "";
  }
  return state?.engine?.model || "";
}

// Edit uses the engine operation. Generate's counter is the compose limit, so the tooltip matches it.
export function operationOf(state) {
  if (state?.mode === "generate") return "compose";
  return state?.engine?.params?.operation || "edit";
}

export function counterText(sent, limit) {
  return `${sent} / ${limit}`;
}

export function refRows(refs, limit, model) {
  const n = Math.max(0, Number(limit) || 0);
  const name = model || "this model";
  return (Array.isArray(refs) ? refs : []).map((ref, i) => ({
    ...ref,
    image: i + 1,
    sent: i < n,
    note: i < n ? "" : `not sent to ${name}`,
  }));
}

export function referenceView(state, config) {
  const limit = refLimit(state, config);
  const sent = sentRefs(state, config).length;
  return { limit, sent, counter: counterText(sent, limit), rows: refRows(state?.refs, limit, modelOf(state)) };
}

export function limitRows(config, operation) {
  const rows = [];
  for (const group of config?.cloud || []) {
    for (const model of group.models || []) {
      if (!model?.id) continue;
      rows.push({ id: model.id, limit: limitFor(config, model.id, operation) });
    }
  }
  return rows;
}

export function limitTooltip(rows) {
  return (rows || []).map((row) => `${row.id} — ${row.limit}`).join("\n");
}

export function reorderRefs(list, from, to) {
  const src = Array.isArray(list) ? list.slice() : [];
  if (!Number.isInteger(from) || !Number.isInteger(to)) return src;
  if (from < 0 || to < 0 || from >= src.length || to >= src.length || from === to) return src;
  const [item] = src.splice(from, 1);
  src.splice(to, 0, item);
  return src;
}

export function setRole(refs, index, role) {
  return (Array.isArray(refs) ? refs : []).map((item, i) => (i === index ? { ...item, role } : item));
}

export function removeRef(refs, index) {
  return (Array.isArray(refs) ? refs : []).filter((_, i) => i !== index);
}

export function pasteGoesToRefs(node) {
  return !!(node && typeof node.closest === "function" && node.closest("[data-slot='refs']"));
}

export function mountReferences(host, store, client) {
  const header = host.parentElement?.querySelector("header");
  const counter = el("span", { class: "ld-counter", tabindex: "0", title: "", text: "… / …", "aria-label": "References sent" });
  header?.append(counter);
  const status = el("p", { class: "ld-muted" });
  const list = el("div", { class: "ld-refs" });
  const hint = el("p", { class: "ld-muted", text: "role: character · style · object · or your own words" });
  const roles = el("datalist", { id: "ld-roles" }, ["character", "style", "object"].map((value) => el("option", { value })));
  const root = el("div", { tabindex: "0", "aria-label": "References" }, [status, list, hint, roles]);
  let config = null;
  let shape = "";
  let dragFrom = -1;

  function paintStatus(text, bad) {
    status.textContent = text || "";
    status.className = bad ? "ld-bad" : "ld-muted";
  }

  async function ingest(files, toRefs) {
    const batch = [...(files || [])];
    if (!batch.length) return;
    await enqueueImport(async () => {
      paintStatus(busyText(1, batch.length), false);
      const { refs: imported, error } = await importBatch(
        client, store.get().project || "default", batch, (text) => paintStatus(text, false),
      );
      const cur = store.get();
      const merged = mergeImported(cur.assets, cur.asset, imported);
      const patch = { assets: merged.assets, asset: merged.asset };
      if (toRefs) patch.refs = [...(cur.refs || []), ...imported.map((ref) => ({ ref, role: "" }))];
      store.set(patch);
      paintStatus(error, !!error);
    });
  }

  function paint(state) {
    const view = config ? referenceView(state, config) : null;
    counter.textContent = view ? view.counter : "… / …";
    counter.title = config ? limitTooltip(limitRows(config, operationOf(state))) : "";
    const rows = view ? view.rows : (state.refs || []).map((ref, i) => ({ ...ref, image: i + 1, sent: true, note: "" }));
    list.replaceChildren(...rows.map((row, index) => {
      const img = row.ref ? el("img", { src: client.viewUrl(row.ref), alt: "" }) : el("span", { class: "ld-muted", text: "—" });
      const role = el("input", {
        type: "text", list: "ld-roles", value: row.role || "", spellcheck: "false",
        "aria-label": `Role for Image ${row.image}`, placeholder: "role",
      });
      role.addEventListener("input", () => store.set({ refs: setRole(store.get().refs, index, role.value) }));
      const handle = el("button", {
        type: "button", class: "ld-btn", draggable: "true", text: "⋮⋮", "aria-label": `Drag Image ${row.image}`,
      });
      handle.addEventListener("dragstart", (ev) => {
        dragFrom = index;
        ev.dataTransfer?.setData("text/plain", String(index));
        if (ev.dataTransfer) ev.dataTransfer.effectAllowed = "move";
      });
      handle.addEventListener("dragend", () => { dragFrom = -1; });
      const card = el("div", { class: "ld-ref", "data-sent": row.sent ? "true" : "false" }, [
        handle, img, el("span", { text: `Image ${row.image}` }), role,
        row.note ? el("span", { class: "ld-muted", text: row.note }) : null,
        el("button", {
          type: "button", class: "ld-btn", text: "×", "aria-label": `Remove Image ${row.image}`,
          onclick: () => store.set({ refs: removeRef(store.get().refs, index) }),
        }),
      ]);
      card.addEventListener("dragover", (ev) => ev.preventDefault());
      card.addEventListener("drop", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        const from = dragFrom;
        if (from < 0) { ingest(ev.dataTransfer?.files, true); return; }
        dragFrom = -1;
        store.set({ refs: reorderRefs(store.get().refs, from, index) });
      });
      return card;
    }));
  }

  function render() {
    const state = store.get();
    const next = JSON.stringify({
      refs: (state.refs || []).map((item) => item?.ref),
      limit: config ? refLimit(state, config) : null,
      model: modelOf(state),
      op: operationOf(state),
    });
    if (!config) counter.title = "";
    if (next === shape) {
      if (config) counter.textContent = referenceView(state, config).counter;
      return;
    }
    shape = next;
    paint(state);
  }

  function onPaste(ev) {
    if (!root.isConnected) return;
    const files = clipboardFiles(ev.clipboardData);
    if (!files.length) return;
    ev.preventDefault();
    ingest(files, pasteGoesToRefs(document.activeElement));
  }

  function onPointerDown(ev) {
    if (ev.target.closest("input, select, textarea, button")) return;
    root.focus();
  }

  root.addEventListener("dragover", (ev) => ev.preventDefault());
  root.addEventListener("drop", (ev) => {
    if (ev.defaultPrevented) return;
    ev.preventDefault();
    ingest(ev.dataTransfer?.files, true);
  });
  root.addEventListener("pointerdown", onPointerDown);
  document.addEventListener("paste", onPaste);
  const off = store.subscribe(() => render());
  client.config().then((cfg) => { config = cfg || {}; shape = ""; render(); }).catch(() => { config = { cloud: [] }; shape = ""; render(); });
  render();
  host.append(root);

  return {
    destroy() {
      document.removeEventListener("paste", onPaste);
      off();
      counter.remove();
      root.remove();
    },
  };
}
