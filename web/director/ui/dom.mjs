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
