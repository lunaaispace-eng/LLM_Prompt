// The studio state: one shared object, shallow-patched, with named actions and subscribers.
export function defaultState() {
  return {
    project: null, mode: "edit", asset: null, refs: [], mask: null, tool: "brush", brush: 40,
    parent: null, // the history entry the next run is a child of (set by editFromHere)
    engine: {
      kind: "cloud", model: null, package: null, version: null,
      params: {
        operation: "inpaint", quality: "auto", aspect_ratio: "auto", resolution: "1K", background: "auto",
        n: 1, mask_mode: "auto", crop_padding: 0.25, feather_px: 0, outpaint: null, seed: null,
      },
    },
    writer: {
      provider: "Local GGUF", model: "", preset: { edit: null, generate: null }, send: { canvas: true, mask: true, refs: true },
      thinking: false, negativeOn: false, busy: false, error: null, positive: "", negative: "",
      visionMp: 1.0, serverUrl: "", gguf: {},
    },
    request: "", prompt: "", negative: "", jobs: {}, history: [], selected: null, dayCost: 0,
    compare: { on: false, a: null, b: null },
    resize: { state: { all: null, items: {} }, plan: null },
    generate: {
      idea: "", exactText: "", variantsMode: "same", variants: [], active: 0, models: [], tier: "draft",
      params: { aspect_ratio: "1:1", resolution: "1K", quality: "auto", background: "auto", count: 1, seed: null },
      batch: null, selected: null, refineText: "", estimate: null,
    },
  };
}

export function createStore(initial = {}, actions = {}) {
  let state = { ...defaultState(), ...initial };
  const subs = new Set();
  const table = new Map(Object.entries(actions));
  const store = {
    get: () => state,
    set(patch) {
      state = { ...state, ...patch };
      for (const fn of [...subs]) fn(state);
      return state;
    },
    subscribe(fn) {
      subs.add(fn);
      return () => subs.delete(fn);
    },
    register(name, fn) { table.set(name, fn); },
    has(name) { return table.has(name); },
    act(name, ...args) {
      const fn = table.get(name);
      if (!fn) throw new Error(`unknown store action: ${name}`);
      return fn(store, ...args);
    },
  };
  // Built-ins patch one key, so the rest survives a tab switch or an engine-kind change (stage-B seam).
  store.register("setMode", (s, mode) => s.set({ mode }));
  store.register("setEngineKind", (s, kind) => s.set({ engine: { ...s.get().engine, kind } }));
  return store;
}
