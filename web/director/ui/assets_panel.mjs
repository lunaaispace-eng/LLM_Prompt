// Assets: every drop, pick, paste and "from graph" image is imported once. The list lives on
// store.assets (refs only). The selected ref is store.asset, which the canvas already reads.
import { el } from "./dom.mjs";
import { mountResize, sameRef } from "./resize_card.mjs";

export const GRAPH_EMPTY = "run the graph once to send its image here";
export const UPLOAD_ERROR = "upload failed: file is not an image";

let tail = Promise.resolve();

export function enqueueImport(fn) {
  const job = tail.then(fn, fn);
  tail = job.then(() => {}, () => {});
  return job;
}

export function refFromUi(img) {
  if (!img || typeof img !== "object") return null;
  const name = typeof img.name === "string" && img.name ? img.name
    : (typeof img.filename === "string" ? img.filename : "");
  if (!name) return null;
  const type = typeof img.type === "string" && img.type ? img.type : "temp";
  return { name, subfolder: typeof img.subfolder === "string" ? img.subfolder : "", type };
}

export function graphImageRefs(node) {
  const out = [];
  const seen = new Set();
  for (const bag of [node?.images, node?.ui?.images, node?.imgs]) {
    if (!Array.isArray(bag)) continue;
    for (const img of bag) {
      const ref = refFromUi(img);
      if (!ref) continue;
      const key = `${ref.name}|${ref.subfolder}|${ref.type}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(ref);
    }
  }
  return out;
}

export function uploadErrorText(err) {
  const msg = String(err?.message || "");
  if (/not an image/i.test(msg)) return UPLOAD_ERROR;
  return "upload failed: " + (msg || "request failed");
}

export function busyText(index, total) {
  return `Uploading… ${index} of ${total}`;
}

export function clipboardFiles(data) {
  const out = [];
  const seen = new Set();
  const push = (file) => {
    if (!file) return;
    const key = [file.name, file.size, file.type, file.lastModified].join("|");
    if (seen.has(key)) return;
    seen.add(key);
    out.push(file);
  };
  if (data?.files) for (const file of data.files) push(file);
  if (data?.items) for (const item of data.items) if (item.kind === "file") push(item.getAsFile?.());
  return out;
}

export function mergeImported(assets, asset, incoming) {
  const list = Array.isArray(assets) ? assets.slice() : [];
  let selected = asset || null;
  for (const ref of incoming || []) {
    if (!ref || typeof ref !== "object" || list.some((item) => sameRef(item, ref))) continue;
    list.push(ref);
    selected = ref;
  }
  return { assets: list, asset: selected };
}

export async function importBatch(client, project, files, onStep) {
  const refs = [];
  let error = "";
  const list = [...(files || [])];
  for (let i = 0; i < list.length; i++) {
    onStep?.(busyText(i + 1, list.length));
    try {
      const res = await client.importAsset(list[i], project);
      if (res?.ref) refs.push(res.ref);
    } catch (err) {
      error = uploadErrorText(err);
    }
  }
  return { refs, error };
}

export async function importGraphRefs(client, project, node, onStep) {
  const sources = graphImageRefs(node);
  if (!sources.length) return { refs: [], error: "", empty: true };
  const refs = [];
  let error = "";
  for (let i = 0; i < sources.length; i++) {
    onStep?.(busyText(i + 1, sources.length));
    try {
      const res = await client.importFromRef(sources[i], project);
      if (res?.ref) refs.push(res.ref);
    } catch (err) {
      error = uploadErrorText(err);
    }
  }
  return { refs, error, empty: false };
}

function paintStatus(node, text, bad) {
  node.textContent = text || "";
  node.className = bad ? "ld-bad" : "ld-muted";
}

export function mountAssets(host, store, client) {
  const file = el("input", { type: "file", accept: "image/*", multiple: true, hidden: true, "aria-label": "Upload images" });
  const upload = el("button", { type: "button", class: "ld-btn", text: "Upload" });
  const fromGraph = el("button", { type: "button", class: "ld-btn", text: "From graph" });
  const asRef = el("button", { type: "button", class: "ld-btn", text: "As reference" });
  const drop = el("div", { class: "ld-drop", text: "Drop images here" });
  const status = el("p", { class: "ld-muted" });
  const thumbs = el("div", { class: "ld-thumbs" });
  const resizeHost = el("div");
  const root = el("div", { class: "ld-assets" }, [
    el("div", { class: "ld-actions" }, [upload, fromGraph, asRef]),
    file, drop, status, thumbs, resizeHost,
  ]);
  const resize = mountResize(resizeHost, store, client);
  let shown = "";

  function commit(incoming) {
    const cur = store.get();
    const merged = mergeImported(cur.assets, cur.asset, incoming);
    store.set({ assets: merged.assets, asset: merged.asset });
    return merged;
  }

  async function takeFiles(files) {
    const list = [...(files || [])];
    if (!list.length) return;
    await enqueueImport(async () => {
      paintStatus(status, busyText(1, list.length), false);
      const { refs, error } = await importBatch(client, store.get().project || "default", list, (text) => paintStatus(status, text, false));
      commit(refs);
      paintStatus(status, error, !!error);
    });
  }

  function render() {
    const state = store.get();
    const assets = Array.isArray(state.assets) ? state.assets : [];
    if (state.asset && !assets.some((item) => sameRef(item, state.asset))) {
      store.set({ assets: [state.asset, ...assets] });
      return;
    }
    const key = JSON.stringify([assets, state.asset]);
    if (key === shown) return;
    shown = key;
    thumbs.replaceChildren(...assets.map((ref) => {
      const btn = el("button", {
        type: "button", class: "ld-thumb", "aria-pressed": sameRef(ref, state.asset) ? "true" : "false",
        "aria-label": ref.name || "asset",
      }, [el("img", { src: client.viewUrl(ref), alt: "" })]);
      btn.addEventListener("click", () => store.set({ asset: ref }));
      return btn;
    }));
  }

  upload.addEventListener("click", () => file.click());
  file.addEventListener("change", () => { takeFiles(file.files); file.value = ""; });
  fromGraph.addEventListener("click", () => enqueueImport(async () => {
    const node = store.boundNode;
    const { refs, error, empty } = await importGraphRefs(client, store.get().project || "default", node, (text) => paintStatus(status, text, false));
    if (empty) { paintStatus(status, GRAPH_EMPTY, false); return; }
    commit(refs);
    paintStatus(status, error, !!error);
  }));
  asRef.addEventListener("click", () => {
    const asset = store.get().asset;
    if (!asset) return;
    store.set({ refs: [...(store.get().refs || []), { ref: asset, role: "" }] });
  });
  for (const node of [root, drop]) {
    node.addEventListener("dragover", (ev) => { ev.preventDefault(); });
    node.addEventListener("drop", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      takeFiles(ev.dataTransfer?.files);
    });
  }

  const off = store.subscribe(() => render());
  render();
  host.append(root);
  return { destroy() { off(); resize.destroy(); root.remove(); } };
}
