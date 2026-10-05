// Small DOM helpers for the Director shell. Later panels use the same builders.

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "hidden") node.hidden = !!value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of [].concat(children)) {
    if (child == null || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

const RESERVED = new Set(["con", "prn", "aux", "nul",
  ...Array.from({ length: 9 }, (_, i) => `com${i + 1}`), ...Array.from({ length: 9 }, (_, i) => `lpt${i + 1}`)]);

// The same rule as luna_director/store.py project_slug: the routes accept only [a-z0-9-], at most 48.
export function projectSlug(name) {
  const ascii = String(name ?? "").normalize("NFKD").replace(/[^\x00-\x7f]/g, "").toLowerCase();
  const slug = ascii.replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 48).replace(/^-+|-+$/g, "")
    || "default";
  return RESERVED.has(slug) ? `${slug}-project` : slug;
}

export function clear(node) {
  node.replaceChildren();
}

export function ensureCss(url) {
  const href = url.href || String(url);
  for (const link of document.querySelectorAll("link[rel='stylesheet']")) {
    if (link.href === href) return;
  }
  document.head.append(el("link", { rel: "stylesheet", href }));
}
