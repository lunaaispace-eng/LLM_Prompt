// Bounded undo / redo over opaque snapshots. The newest pushed snapshot is the current state.
export function createUndo(limit = 30) {
  let past = [];
  let future = [];
  return {
    push(snapshot) {
      past.push(snapshot);
      if (past.length > limit) past = past.slice(past.length - limit);
      future = [];
    },
    undo() {
      if (past.length < 2) return undefined;
      future.push(past.pop());
      return past[past.length - 1];
    },
    redo() {
      if (!future.length) return undefined;
      const s = future.pop();
      past.push(s);
      return s;
    },
    get canUndo() { return past.length > 1; },
    get canRedo() { return future.length > 0; },
  };
}
