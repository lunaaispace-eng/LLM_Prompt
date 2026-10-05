// The Director's own websocket. One sid per tab, kept in sessionStorage.
// A duplicated tab copies sessionStorage, so a twin that already holds the sid answers on
// BroadcastChannel("luna.director") and this tab mints another. Otherwise the server keeps
// only the last socket and one tab loses luna.job events.

const SID_KEY = "luna.director.sid";
const CHANNEL = "luna.director";

function mint() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
  return "sid-" + Math.random().toString(16).slice(2) + Date.now().toString(16);
}

let channel = null;
let ownedSid = null;
let owned = false;

function storageGet() {
  try { return globalThis.sessionStorage?.getItem(SID_KEY) || ""; } catch { return ""; }
}

function storageSet(value) {
  try { globalThis.sessionStorage?.setItem(SID_KEY, value); } catch { /* private mode */ }
}

function ensureChannel() {
  if (channel || !globalThis.BroadcastChannel) return null;
  channel = new BroadcastChannel(CHANNEL);
  channel.onmessage = (ev) => {
    const msg = ev.data;
    if (owned && msg?.type === "claim" && msg.sid === ownedSid) {
      channel.postMessage({ type: "taken", sid: ownedSid });
    }
  };
  return channel;
}

/** Resolve the tab sid. `preferred` wins over sessionStorage. A twin's "taken" mints a new one. */
export function claimSid(preferred) {
  ensureChannel();
  let sid = preferred || storageGet() || mint();
  storageSet(sid);
  if (!channel) {
    ownedSid = sid;
    owned = true;
    return Promise.resolve(sid);
  }
  return new Promise((resolve) => {
    const previous = channel.onmessage;
    let timer = null;
    const arm = () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        channel.onmessage = previous;
        ownedSid = sid;
        owned = true;
        storageSet(sid);
        resolve(sid);
      }, 120);
    };
    channel.onmessage = (ev) => {
      const msg = ev.data;
      if (!msg || msg.sid !== sid) return;
      if (msg.type === "taken" || (msg.type === "claim" && !owned)) {
        sid = mint();
        storageSet(sid);
        channel.postMessage({ type: "claim", sid });
        arm();
      }
    };
    channel.postMessage({ type: "claim", sid });
    arm();
  });
}

function wsUrl(apiBase, sid) {
  const base = String(apiBase || "").replace(/\/$/, "");
  return base + "/ws?clientId=" + encodeURIComponent(sid);
}

/**
 * Open `/ws?clientId=<sid>` and reconnect until `close()`.
 * `onBinary` is wired for stage B and unused until then.
 */
export async function connectSocket({ apiBase, sid, onMessage, onBinary, onReconnect }) {
  const mine = await claimSid(sid);
  let closed = false;
  let socket = null;
  let attempt = 0;
  let openedOnce = false;
  let state = "connecting";
  const listeners = new Set();

  function setState(next) {
    state = next;
    for (const fn of listeners) fn(state);
  }

  function open() {
    if (closed) return;
    setState(openedOnce ? "reconnecting" : "connecting");
    socket = new WebSocket(wsUrl(apiBase, mine));
    socket.binaryType = "arraybuffer";
    socket.onopen = () => {
      attempt = 0;
      const again = openedOnce;
      openedOnce = true;
      setState("open");
      if (again) onReconnect?.();
    };
    socket.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        let msg = null;
        try { msg = JSON.parse(ev.data); } catch { return; }
        onMessage?.(msg);
        return;
      }
      onBinary?.(ev.data);
    };
    socket.onclose = () => {
      if (closed) return;
      setState("reconnecting");
      const delay = Math.min(15000, 1000 * 2 ** attempt);
      attempt += 1;
      setTimeout(open, delay);
    };
  }

  open();
  return {
    sid: mine,
    get state() { return state; },
    subscribe(fn) {
      listeners.add(fn);
      fn(state);
      return () => listeners.delete(fn);
    },
    close() {
      closed = true;
      try { socket?.close(); } catch { /* already closed */ }
    },
  };
}
