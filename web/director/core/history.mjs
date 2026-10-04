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

export function defaultComparePair(entries, selectedId) {
  const e = byId(entries, selectedId);
  if (!e) throw new Error(`unknown history entry: ${selectedId}`);
  return [e.parent || ORIGINAL, selectedId];
}

export function comparePair(entries, aId, bId) {
  const side = (id) => {
    if (id === ORIGINAL) return { id: ORIGINAL };
    const e = byId(entries, id);
    if (!e) throw new Error(`unknown history entry: ${id}`);
    return e;
  };
  return { a: side(aId), b: side(bId) };
}

// The entry's output becomes the canvas, the mask is cleared, and the next run is its child.
export function editFromHere(entry) {
  return { mode: "edit", asset: outputOf(entry), mask: null, parent: entry.id };
}
