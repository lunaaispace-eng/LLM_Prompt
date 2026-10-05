// Luna Director entry. The only new auto-loaded script. The studio modules are .mjs.
import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";
import { createClient } from "./director/core/api_client.mjs";
import { createStore } from "./director/core/store.mjs";
import { installFrameActions, mountOverlay } from "./director/frames/overlay.mjs";
import { installQueueActions } from "./director/ui/queue_panel.mjs";
import { connectSocket } from "./director/socket.mjs";
import { registerLunaCollapse } from "./luna_collapse.mjs";
import { registerLunaHelp } from "./luna_help.mjs";

const LAUNCHER = "LunaDirectorLauncher";

// The kit registers a node from a list in the entry file (SaveSimple's luna_pack_theme.js
// does the same). The kit files themselves are not edited.
registerLunaHelp(app, [LAUNCHER], "Luna.Director.Help");
registerLunaCollapse(app, [LAUNCHER], "Luna.Director.Collapse");

let studio = null;

function wsBase() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const host = api.api_host || location.host;
  const base = api.api_base || "";
  return proto + "//" + host + base;
}

async function ensureStudio() {
  if (studio) return studio;
  const store = createStore();
  const client = createClient(api.fetchApi.bind(api), "");
  const queue = installQueueActions(store, client);
  let bound = null;
  installFrameActions(store, () => bound);
  const socket = await connectSocket({
    apiBase: wsBase(),
    onMessage(msg) { void queue.message(msg); },
    onBinary() {},
    onReconnect() { void queue.reconnect(); },
  });
  store.set({ socketSid: socket.sid });
  studio = {
    store,
    client,
    socket,
    bind(node) { bound = node || null; },
  };
  return studio;
}

/** Open the studio bound to `node`, or unbound when `node` is missing. */
export async function openStudio(node) {
  const current = await ensureStudio();
  current.bind(node || null);
  if (current.overlay) current.overlay.close();
  current.overlay = mountOverlay({
    app,
    api,
    store: current.store,
    client: current.client,
    socket: current.socket,
    node: node || null,
  });
  void installQueueActions(current.store, current.client).reconnect();
}

function selectedLauncher() {
  const found = [];
  for (const bag of [app.canvas?.selected_nodes, app.canvas?.selectedItems]) {
    if (!bag) continue;
    found.push(...(Array.isArray(bag) ? bag : Object.values(bag)));
  }
  for (const node of app.graph?.nodes || app.graph?._nodes || []) {
    if (node?.is_selected || node?.selected) found.push(node);
  }
  return found.find((node) => node && (node.type === LAUNCHER || node.comfyClass === LAUNCHER)) || null;
}

function hideState(node) {
  const widget = node.widgets?.find((w) => w.name === "director_state");
  if (!widget) return;
  widget.hidden = true;
  widget.type = "hidden";
  if (!widget.options) widget.options = {};
  widget.options.hidden = true;
  widget.computeSize = () => [0, -4];
}

app.registerExtension({
  name: "Luna.Director",
  commands: [
    {
      id: "Luna.OpenStudio",
      label: "Open Studio",
      function: () => { openStudio(selectedLauncher()); },
    },
  ],
  menuCommands: [
    { path: ["Workflow"], commands: ["Luna.OpenStudio"] },
  ],
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== LAUNCHER) return;
    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = onCreated?.apply(this, arguments);
      hideState(this);
      const button = this.addWidget("button", "Open Studio", null, () => openStudio(this), { serialize: false });
      if (button) {
        button.serialize = false;
        if (button.options) button.options.serialize = false;
      }
      return result;
    };
    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const result = onConfigure?.apply(this, arguments);
      hideState(this);
      return result;
    };
  },
});
