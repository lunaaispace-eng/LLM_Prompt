import test from "node:test";
import assert from "node:assert/strict";
import {
  applyOne, buildResizeState, readoutText, resetResize, resizeRequest,
} from "../../web/director/ui/resize_card.mjs";
import {
  GRAPH_EMPTY, graphImageRefs, mergeImported, refFromUi, uploadErrorText, UPLOAD_ERROR,
} from "../../web/director/ui/assets_panel.mjs";
import {
  counterText, limitRows, limitTooltip, modelOf, operationOf, pasteGoesToRefs, referenceView, refRows, reorderRefs,
} from "../../web/director/ui/references_panel.mjs";

const config = {
  cloud: [{ provider: "xai", models: [
    { id: "grok-imagine-image-quality", ref_limit: { generate: 0, compose: 3, edit: 2, inpaint: 2, outpaint: 2 }, ratio_presets: ["", "1:1", "16:9"] },
    { id: "grok-imagine-image", ref_limit: { edit: 4, compose: 5 } },
  ] }, { provider: "openai", models: [
    { id: "gpt-image-2", ref_limit: { edit: 15, compose: 16 } },
  ] }],
};

const edit = (over = {}) => ({
  mode: "edit",
  refs: [],
  engine: { model: "grok-imagine-image-quality", params: { operation: "edit" } },
  generate: { models: [] },
  ...over,
});

test("buildResizeState folds the canvas anchor into the current item", () => {
  const anchor = { x: 0, y: 0.25 };
  const state = { all: null, items: [{ mode: "longest_side" }, {}], crop_anchor: anchor };
  const out = buildResizeState(state, { index: 1, count: 2, applyAll: false });
  assert.deepEqual(out.items[1].crop_anchor, { x: 0, y: 0.25 });
  assert.equal(out.items[0].crop_anchor, undefined);
  assert.equal(Object.hasOwn(out, "crop_anchor"), false);
  assert.equal(state.items[1].crop_anchor, undefined);
  out.items[1].crop_anchor.x = 9;
  assert.equal(anchor.x, 0);
});

test("buildResizeState accepts the store default items object and folds into all", () => {
  const state = { all: { mode: "longest_side" }, items: { "0": { crop_anchor: "left" } }, crop_anchor: { x: 1, y: 0 } };
  const single = buildResizeState({ all: null, items: {}, crop_anchor: { x: 0, y: 1 } }, { index: 0, count: 1, applyAll: false });
  assert.deepEqual(single.items[0].crop_anchor, { x: 0, y: 1 });
  const all = buildResizeState(state, { index: 0, count: 1, applyAll: true });
  assert.deepEqual(all.all.crop_anchor, { x: 1, y: 0 });
  assert.equal(all.all.mode, "longest_side");
  assert.equal(all.items[0].crop_anchor, undefined);
  const req = resizeRequest(state, { index: 0, applyAll: true, assets: [{ name: "a.png", type: "input" }, { name: "b.png", type: "input" }] });
  assert.equal(req.refs.length, 2);
  assert.equal(req.state.items.length, 0);
  assert.deepEqual(req.state.all.crop_anchor, { x: 1, y: 0 });
  const one = resizeRequest(state, { index: 1, applyAll: false, assets: req.refs });
  assert.deepEqual(one.refs, [{ name: "b.png", type: "input" }]);
  assert.deepEqual(one.state.items[0].crop_anchor, { x: 1, y: 0 });
});

test("resetResize restores defaults and drops the pending anchor", () => {
  const next = resetResize({ all: null, items: [{ mode: "max_mp" }], crop_anchor: { x: 0, y: 0 } }, { index: 0, applyAll: false });
  assert.equal(next.items[0].mode, "off");
  assert.equal(next.items[0].longest_side, 1024);
  assert.equal(next.crop_anchor, undefined);
});

test("readout uses the server plan, including round-half-even sizes", () => {
  assert.equal(readoutText({ in: [2048, 1365], out: [1024, 682] }), "2048×1365 → 1024×682");
  assert.equal(readoutText(null), "");
});

test("apply keeps the original and inserts the resized copy beside it", () => {
  const assets = [{ name: "a.png", type: "input" }, { name: "b.png", type: "input" }];
  const same = applyOne(assets, 0, { changed: false, ref: assets[0] });
  assert.equal(same.assets.length, 2);
  assert.equal(same.index, 0);
  const placed = applyOne(assets, 0, { changed: true, ref: { name: "c.png", type: "input" } });
  assert.deepEqual(placed.assets.map((item) => item.name), ["a.png", "c.png", "b.png"]);
  assert.equal(placed.index, 1);
  const again = applyOne(placed.assets, 0, { changed: true, ref: { name: "c.png", type: "input" } });
  assert.equal(again.assets.length, 3);
  assert.equal(again.index, 1);
});

test("references past the limit are kept, greyed, and the counter counts only sent ones", () => {
  const refs = [{ role: "character", ref: { name: "1.png" } }, { role: "style", ref: { name: "2.png" } }, { role: "object", ref: { name: "3.png" } }];
  const state = edit({ refs });
  const view = referenceView(state, config);
  assert.equal(view.counter, "2 / 2");
  assert.equal(counterText(view.sent, view.limit), "2 / 2");
  assert.deepEqual(view.rows.map((row) => [row.image, row.sent, row.note]), [
    [1, true, ""],
    [2, true, ""],
    [3, false, "not sent to grok-imagine-image-quality"],
  ]);
  assert.deepEqual(refRows(refs, 0, "gpt-image-2").map((row) => row.sent), [false, false, false]);
});

test("reorder changes image numbers and which rows are sent", () => {
  const refs = [{ role: "a" }, { role: "b" }, { role: "c" }];
  const next = reorderRefs(refs, 1, 0);
  assert.deepEqual(next.map((row) => row.role), ["b", "a", "c"]);
  const rows = refRows(next, 2, "grok-imagine-image-quality");
  assert.deepEqual(rows.map((row) => `${row.image}:${row.role}:${row.sent}`), ["1:b:true", "2:a:true", "3:c:false"]);
  assert.deepEqual(reorderRefs(next, 0, 0).map((row) => row.role), ["b", "a", "c"]);
});

test("the limit tooltip lists every cloud model for the current operation", () => {
  assert.equal(operationOf(edit()), "edit");
  assert.equal(operationOf({ mode: "generate", generate: { models: ["gpt-image-2"] } }), "compose");
  assert.equal(modelOf({ mode: "generate", generate: { models: [{ id: "gpt-image-2" }] } }), "gpt-image-2");
  const rows = limitRows(config, "edit");
  assert.deepEqual(rows, [
    { id: "grok-imagine-image-quality", limit: 2 },
    { id: "grok-imagine-image", limit: 4 },
    { id: "gpt-image-2", limit: 15 },
  ]);
  assert.equal(limitTooltip(rows), "grok-imagine-image-quality — 2\ngrok-imagine-image — 4\ngpt-image-2 — 15");
});

test("the same imported ref is one asset, and from-graph reads filename as a temp ref", () => {
  const ref = { name: "abc.png", subfolder: "luna_director/p", type: "input" };
  const once = mergeImported([], null, [ref]);
  const twice = mergeImported(once.assets, once.asset, [ref]);
  assert.equal(twice.assets.length, 1);
  assert.equal(twice.asset, once.asset);
  const ui = graphImageRefs({ images: [{ filename: "ComfyUI_temp_x.png", subfolder: "", type: "temp" }, { filename: "ComfyUI_temp_x.png", subfolder: "", type: "temp" }] });
  assert.deepEqual(ui, [{ name: "ComfyUI_temp_x.png", subfolder: "", type: "temp" }]);
  assert.equal(refFromUi({ filename: "a.png" }).type, "temp");
  assert.equal(uploadErrorText({ message: "not an image" }), UPLOAD_ERROR);
  assert.equal(uploadErrorText({ message: "ref does not exist" }), "upload failed: ref does not exist");
  assert.equal(GRAPH_EMPTY, "run the graph once to send its image here");
  assert.equal(pasteGoesToRefs({ closest: (sel) => (String(sel).includes("refs") ? {} : null) }), true);
  assert.equal(pasteGoesToRefs({ closest: () => null }), false);
});
