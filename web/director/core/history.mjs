// History entries (pure): parent chains, compare pairs, edit-from-here. The day total is the server's only.
export const ORIGINAL = "original"; // a compare side meaning the input picture (an entry with no parent)

const byId = (entries, id) => (entries || []).find((e) => e.id === id);
const outputOf = (e) => (Array.isArray(e.outputs) ? e.outputs[0] : e.output) || null;

export function childOf(entry, patch) {
  return { ...patch, parent: entry.id };
}

export function chainDepth(entries, id) {
  let depth = 0;
  const seen = new Set();
  let e = byId(entries, id);
  while (e && e.parent && !seen.has(e.id)) {
    seen.add(e.id);
    depth += 1;
    e = byId(entries, e.parent);
  }
  return depth;
}

// The input picture of a chain: the root entry's first input when it is an Edit run. A Generate root
// (it carries a batch) has no original picture, so there is nothing to compare against.
function originalOf(entries, id) {
  const seen = new Set();
  let e = byId(entries, id);
  while (e && e.parent && !seen.has(e.id)) { seen.add(e.id); e = byId(entries, e.parent); }
  if (!e || e.batch || !e.inputs || !e.inputs.length) return null;
  return e.inputs[0];
}

export function defaultComparePair(entries, selectedId) {
  const e = byId(entries, selectedId);
  if (!e) throw new Error(`unknown history entry: ${selectedId}`);
  if (e.parent) return [e.parent, selectedId];
  return originalOf(entries, selectedId) ? [ORIGINAL, selectedId] : null;
}

// {a, b} resolved in the given order; the ORIGINAL side carries the chain's input as outputs[0]. null when a
// chain has no original to show.
export function comparePair(entries, aId, bId) {
  const side = (id, other) => {
    if (id === ORIGINAL) {
      const ref = originalOf(entries, other);
      return ref ? { id: ORIGINAL, outputs: [ref] } : null;
    }
    const e = byId(entries, id);
    if (!e) throw new Error(`unknown history entry: ${id}`);
    return e;
  };
  const a = side(aId, bId);
  const b = side(bId, aId);
  return a && b ? { a, b } : null;
}

// The entry's output becomes the canvas, the mask is cleared, and the next run is its child. Given the state,
// the previous canvas's outpaint margins and resize plan are reset too.
export function editFromHere(entry, state = null) {
  const patch = { mode: "edit", asset: outputOf(entry), mask: null, parent: entry.id };
  if (state) {
    patch.engine = { ...state.engine, params: { ...state.engine.params, outpaint: null } };
    patch.resize = { ...state.resize, plan: null };
  }
  return patch;
}
