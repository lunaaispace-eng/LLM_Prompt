// Studio settings. Stage A has no server settings bag; these fields are the shared writer values.
// API keys are never shown or accepted here.
import { el, projectSlug } from "./dom.mjs";

function field(label, input) {
  return el("label", { class: "ld-field" }, [el("span", { text: label }), input]);
}

export function mountSettings(root, store, client) {
  const project = el("input", { type: "text", spellcheck: "false", "aria-label": "Project" });
  const vision = el("input", { type: "number", min: "0.1", max: "8", step: "0.1", "aria-label": "Vision megapixels" });
  const server = el("input", { type: "text", spellcheck: "false", "aria-label": "Writer server URL" });
  const engines = el("p", { class: "ld-muted", text: "Engines: …" });

  function fill() {
    const state = store.get();
    if (document.activeElement !== project) project.value = state.project || "";
    if (document.activeElement !== vision) vision.value = String(state.writer?.visionMp ?? 1);
    if (document.activeElement !== server) server.value = state.writer?.serverUrl || "";
  }

  project.addEventListener("change", () => {
    store.set({ project: projectSlug(project.value) });
  });
  vision.addEventListener("change", () => {
    const n = Number(vision.value);
    if (!Number.isFinite(n) || n <= 0) return;
    store.set({ writer: { ...store.get().writer, visionMp: n } });
  });
  server.addEventListener("change", () => {
    store.set({ writer: { ...store.get().writer, serverUrl: server.value.trim() } });
  });

  const off = store.subscribe(() => fill());
  fill();
  root.append(
    el("h3", { text: "Settings" }),
    field("Project", project),
    field("Vision megapixels", vision),
    field("Writer server URL", server),
    engines,
    el("p", { class: "ld-muted", text: "API keys stay in the server environment. This panel never asks for one." }),
  );

  client.config().then((cfg) => {
    const list = Array.isArray(cfg?.engines) ? cfg.engines : [];
    engines.textContent = "Engines: " + (list.join(", ") || "none");
  }).catch((err) => {
    engines.textContent = "Config unavailable: " + (err?.message || "request failed");
  });

  return { destroy() { off(); } };
}
