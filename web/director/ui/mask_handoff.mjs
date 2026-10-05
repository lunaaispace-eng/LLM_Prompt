// Working masks survive canvas remounts; uploaded refs are keyed by revision and project.
import { isEmpty } from "../core/maskops.mjs";
const working = new WeakMap(), caches = new WeakMap();

export function createMaskHandoff(store, client, exportPng) {
  const uploads = caches.get(store) || new Map();
  caches.set(store, uploads);
  let alive = true;
  if (typeof store.get().maskEmpty !== "boolean" || !Number.isFinite(store.get().maskVersion)) {
    store.set({ maskEmpty: true, maskVersion: 0 });
  }
  store.register("uploadMask", async () => {
    if (!alive) throw new Error("Open the canvas before uploading its mask");
    for (;;) {
      const { maskVersion, maskEmpty, project } = store.get();
      if (maskEmpty !== false) throw new Error("The mask is empty");
      const key = `${project}:${maskVersion}`;
      if (!uploads.has(key)) {
        const pending = Promise.resolve(exportPng()).then((blob) => client.importAsset(blob, project))
          .then((asset) => asset.ref);
        uploads.set(key, pending);
        pending.catch(() => uploads.delete(key));
      }
      const ref = await uploads.get(key);
      const current = store.get();
      if (current.maskVersion !== maskVersion || current.project !== project) continue;
      if (!alive) throw new Error("Canvas closed during mask upload");
      if (!ref?.name) throw new Error("Mask import returned no file reference");
      store.set({ mask: ref });
      return ref;
    }
  });
  return {
    changed(mask, assetKey) {
      working.set(store, { mask, assetKey });
      uploads.clear();
      store.set({ mask: null, maskEmpty: !mask || isEmpty(mask), maskVersion: (store.get().maskVersion || 0) + 1 });
    },
    saved(assetKey) { const s = working.get(store); return s?.assetKey === assetKey ? s.mask : null; },
    destroy() { alive = false; },
  };
}
