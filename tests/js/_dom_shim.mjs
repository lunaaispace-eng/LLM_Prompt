// Minimal DOM for mounting Director panels under node --test. Not a browser.
const BOX = { x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600,
  toJSON() { return { x: 0, y: 0, width: 800, height: 600 }; } };

function styleOf() {
  const bag = {};
  return new Proxy(bag, {
    get(t, p) {
      if (p === "setProperty") return (k, v) => { t[k] = String(v); };
      if (p === "getPropertyValue") return (k) => t[k] || "";
      if (typeof p === "symbol") return t[p];
      return t[p] || "";
    },
    set(t, p, v) { t[p] = String(v); return true; },
  });
}

function datasetOf(el) {
  const attr = (key) => "data-" + String(key).replace(/[A-Z]/g, (m) => "-" + m.toLowerCase());
  return new Proxy({}, {
    get(_, key) {
      const v = el.getAttribute(attr(key));
      return v == null ? undefined : v;
    },
    set(_, key, value) { el.setAttribute(attr(key), value); return true; },
  });
}

function classListOf(el) {
  const list = () => (el.className || "").split(/\s+/).filter(Boolean);
  const write = (items) => { el.className = items.join(" "); };
  return {
    add(...xs) { const s = new Set(list()); for (const x of xs) s.add(x); write([...s]); },
    remove(...xs) { write(list().filter((c) => !xs.includes(c))); },
    contains(x) { return list().includes(x); },
    toggle(x, force) {
      const on = force != null ? !!force : !list().includes(x);
      if (on) this.add(x); else this.remove(x);
      return on;
    },
  };
}

function listen(node, type, fn, options) {
  const cap = options === true || !!options?.capture;
  const key = (cap ? "c:" : "") + type;
  if (!node._lsn) node._lsn = new Map();
  const list = node._lsn.get(key) || [];
  list.push(fn);
  node._lsn.set(key, list);
}

function unlisten(node, type, fn, options) {
  const cap = options === true || !!options?.capture;
  const key = (cap ? "c:" : "") + type;
  const list = node._lsn?.get(key);
  if (!list) return;
  const i = list.indexOf(fn);
  if (i >= 0) list.splice(i, 1);
}

function emit(node, ev) {
  const event = ev || {};
  event.type = event.type || "click";
  event.target = event.target || node;
  event.currentTarget = node;
  event.defaultPrevented = !!event.defaultPrevented;
  event.preventDefault = () => { event.defaultPrevented = true; };
  event.stopPropagation = () => { event._stop = true; };
  const run = (key) => {
    for (const fn of [...(node._lsn?.get(key) || [])]) {
      fn.call(node, event);
      if (event._stop) return;
    }
  };
  run("c:" + event.type);
  if (!event._stop) run(event.type);
  return !event.defaultPrevented;
}

function parseCompound(sel) {
  let i = 0;
  let tag = "*";
  const classes = [];
  const attrs = [];
  if (sel[0] !== "." && sel[0] !== "[") {
    const m = /^[A-Za-z][\w-]*/.exec(sel);
    if (m) { tag = m[0].toLowerCase(); i = m[0].length; }
  }
  while (i < sel.length) {
    if (sel[i] === ".") {
      const m = /^[\w-]+/.exec(sel.slice(i + 1));
      if (!m) break;
      classes.push(m[0]);
      i += 1 + m[0].length;
    } else if (sel[i] === "[") {
      const end = sel.indexOf("]", i);
      if (end < 0) break;
      const body = sel.slice(i + 1, end);
      i = end + 1;
      const eq = body.indexOf("=");
      if (eq < 0) attrs.push([body.trim(), null]);
      else {
        let value = body.slice(eq + 1).trim();
        if ((value.startsWith("'") && value.endsWith("'")) || (value.startsWith('"') && value.endsWith('"'))) {
          value = value.slice(1, -1);
        }
        attrs.push([body.slice(0, eq).trim(), value]);
      }
    } else break;
  }
  return { tag, classes, attrs };
}

function matchOne(el, sel) {
  if (!el || el.nodeType !== 1) return false;
  const spec = parseCompound(sel);
  if (spec.tag !== "*" && el.tagName.toLowerCase() !== spec.tag) return false;
  const classes = (el.className || "").split(/\s+/);
  if (spec.classes.some((c) => !classes.includes(c))) return false;
  return spec.attrs.every(([name, value]) => (value == null ? el.hasAttribute(name) : el.getAttribute(name) === value));
}

function matches(el, selector) {
  return selector.split(",").some((group) => {
    const parts = group.trim().split(/\s+/).filter(Boolean);
    if (!parts.length || !matchOne(el, parts[parts.length - 1])) return false;
    let node = el.parentElement;
    for (let i = parts.length - 2; i >= 0; i--) {
      while (node && !matchOne(node, parts[i])) node = node.parentElement;
      if (!node) return false;
      node = node.parentElement;
    }
    return true;
  });
}

function walk(node, selector, all, out) {
  for (const child of node.children || []) {
    if (child.nodeType === 1 && matches(child, selector)) {
      out.push(child);
      if (!all) return true;
    }
    if (walk(child, selector, all, out) && !all) return true;
  }
  return false;
}

const ctx2d = {
  createImageData(w, h) {
    return { width: w, height: h, data: new Uint8ClampedArray(Math.max(0, w * h * 4)) };
  },
  putImageData() {},
  fillRect() {},
  clearRect() {},
  drawImage() {},
};

class Element {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.nodeType = 1;
    this.nodeName = this.tagName;
    this.childNodes = [];
    this.children = [];
    this.parentNode = null;
    this.parentElement = null;
    this._attrs = {};
    this._class = "";
    this._lsn = new Map();
    this._style = styleOf();
    if (this.tagName === "CANVAS") {
      this.width = 0;
      this.height = 0;
      this.getContext = (kind) => (kind === "2d" ? ctx2d : null);
      this.toBlob = (cb) => { queueMicrotask(() => cb(new Blob(["png"]))); };
    }
    if (this.tagName === "IMG") {
      this.naturalWidth = 0;
      this.naturalHeight = 0;
    }
  }
  get className() { return this._class; }
  set className(v) { this._class = String(v ?? ""); }
  get classList() { return classListOf(this); }
  get style() { return this._style; }
  get dataset() { return datasetOf(this); }
  get id() { return this.getAttribute("id") || ""; }
  set id(v) { this.setAttribute("id", v); }
  get href() { return this.getAttribute("href") || ""; }
  set href(v) { this.setAttribute("href", v); }
  get hidden() { return this.hasAttribute("hidden"); }
  set hidden(v) { if (v) this.setAttribute("hidden", ""); else this.removeAttribute("hidden"); }
  get disabled() { return this._disabled ?? this.hasAttribute("disabled"); }
  set disabled(v) { this._disabled = !!v; }
  get checked() { return this._checked ?? this.hasAttribute("checked"); }
  set checked(v) { this._checked = !!v; }
  get value() {
    if (this._value != null) return this._value;
    if (this.tagName === "OPTION" || this.hasAttribute("value")) {
      const attr = this.getAttribute("value");
      if (attr != null) return attr;
    }
    if (this.tagName === "OPTION") return this.textContent;
    return "";
  }
  set value(v) { this._value = v == null ? "" : String(v); }
  get files() { return this._files || []; }
  get options() { return this.querySelectorAll("option"); }
  get src() { return this.getAttribute("src") || ""; }
  set src(v) { this.setAttribute("src", v == null ? "" : String(v)); }
  get textContent() {
    return this.childNodes.map((n) => (n.nodeType === 3 ? n.data : n.textContent || "")).join("");
  }
  set textContent(v) {
    this.childNodes = [];
    this.children = [];
    const text = v == null ? "" : String(v);
    if (text) this.childNodes.push(textNode(text, this));
  }
  get clientWidth() { return BOX.width; }
  get clientHeight() { return BOX.height; }
  get isConnected() {
    let n = this;
    while (n) {
      if (n === document) return true;
      n = n.parentNode;
    }
    return false;
  }
  setAttribute(name, value) {
    this._attrs[name] = String(value);
    if (name === "src" && this.tagName === "IMG") {
      this.naturalWidth = this.naturalWidth || 64;
      this.naturalHeight = this.naturalHeight || 64;
      const seq = (this._loadSeq = (this._loadSeq || 0) + 1);
      queueMicrotask(() => { if (this._loadSeq === seq) emit(this, { type: "load" }); });
    }
  }
  getAttribute(name) {
    if (name === "class") return this._class || null;
    return Object.prototype.hasOwnProperty.call(this._attrs, name) ? this._attrs[name] : null;
  }
  hasAttribute(name) {
    if (name === "class") return !!this._class;
    return Object.prototype.hasOwnProperty.call(this._attrs, name);
  }
  removeAttribute(name) { delete this._attrs[name]; }
  addEventListener(type, fn, options) { listen(this, type, fn, options); }
  removeEventListener(type, fn, options) { unlisten(this, type, fn, options); }
  dispatchEvent(ev) { return emit(this, ev); }
  click() { return emit(this, { type: "click" }); }
  focus() { document.activeElement = this; }
  blur() { if (document.activeElement === this) document.activeElement = null; }
  getBoundingClientRect() { return { ...BOX, toJSON: BOX.toJSON }; }
  matches(selector) { return matches(this, selector); }
  closest(selector) {
    let n = this;
    while (n && n.nodeType === 1) {
      if (matches(n, selector)) return n;
      n = n.parentElement;
    }
    return null;
  }
  contains(node) {
    let n = node;
    while (n) {
      if (n === this) return true;
      n = n.parentNode;
    }
    return false;
  }
  querySelector(selector) { const out = []; walk(this, selector, false, out); return out[0] || null; }
  querySelectorAll(selector) { const out = []; walk(this, selector, true, out); return out; }
  append(...nodes) {
    for (const item of nodes) {
      const node = item?.nodeType ? item : textNode(String(item), null);
      if (node.parentNode?.removeChild) node.parentNode.removeChild(node);
      else if (node.remove && node.parentNode) node.remove();
      node.parentNode = this;
      node.parentElement = this;
      this.childNodes.push(node);
      if (node.nodeType === 1) this.children.push(node);
    }
  }
  replaceChildren(...nodes) {
    this.childNodes = [];
    this.children = [];
    this.append(...nodes);
  }
  removeChild(node) {
    this.childNodes = this.childNodes.filter((n) => n !== node);
    this.children = this.children.filter((n) => n !== node);
    node.parentNode = null;
    node.parentElement = null;
    return node;
  }
  remove() { this.parentNode?.removeChild?.(this); }
  checkValidity() { return true; }
}

function textNode(data, parent) {
  return { nodeType: 3, data, textContent: data, parentNode: parent, parentElement: parent };
}

function DocumentRoot() {
  this.nodeType = 9;
  this.activeElement = null;
  this.head = new Element("head");
  this.body = new Element("body");
  this.head.parentNode = this;
  this.body.parentNode = this;
  this._lsn = new Map();
}
DocumentRoot.prototype.createElement = function createElement(tag) {
  const el = new Element(tag);
  el.ownerDocument = this;
  return el;
};
DocumentRoot.prototype.createTextNode = function createTextNode(text) {
  return textNode(String(text), null);
};
DocumentRoot.prototype.querySelector = function querySelector(selector) {
  return this.head.querySelector(selector) || this.body.querySelector(selector);
};
DocumentRoot.prototype.querySelectorAll = function querySelectorAll(selector) {
  return [...this.head.querySelectorAll(selector), ...this.body.querySelectorAll(selector)];
};
DocumentRoot.prototype.addEventListener = function addEventListener(type, fn, options) {
  listen(this, type, fn, options);
};
DocumentRoot.prototype.removeEventListener = function removeEventListener(type, fn, options) {
  unlisten(this, type, fn, options);
};
DocumentRoot.prototype.dispatchEvent = function dispatchEvent(ev) { return emit(this, ev); };

class ResizeObserver {
  constructor(cb) { this.cb = cb; this.nodes = []; }
  observe(node) {
    this.nodes.push(node);
    queueMicrotask(() => { if (this.nodes.includes(node)) this.cb([{ target: node, contentRect: node.getBoundingClientRect() }]); });
  }
  unobserve(node) { this.nodes = this.nodes.filter((n) => n !== node); }
  disconnect() { this.nodes = []; }
}

class BroadcastChannel {
  constructor(name) { this.name = name; this.onmessage = null; }
  postMessage() {}
  close() {}
  addEventListener() {}
  removeEventListener() {}
}

export function installDomShim() {
  if (globalThis.document?.__lunaShim) return globalThis.document;
  const document = new DocumentRoot();
  document.__lunaShim = true;
  let raf = 0;
  const rafs = new Map();
  const storage = new Map();
  globalThis.document = document;
  globalThis.Element = Element;
  globalThis.Node = Element;
  globalThis.Image = function Image() { return document.createElement("img"); };
  globalThis.ResizeObserver = ResizeObserver;
  globalThis.BroadcastChannel = BroadcastChannel;
  globalThis.sessionStorage = {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: (k) => storage.delete(k),
    clear: () => storage.clear(),
  };
  globalThis.requestAnimationFrame = (fn) => {
    const id = ++raf;
    rafs.set(id, fn);
    queueMicrotask(() => {
      if (!rafs.has(id)) return;
      rafs.delete(id);
      fn(0);
    });
    return id;
  };
  globalThis.cancelAnimationFrame = (id) => rafs.delete(id);
  if (typeof globalThis.confirm !== "function") globalThis.confirm = () => false;
  if (!globalThis.window) globalThis.window = globalThis;
  return document;
}

installDomShim();
