"""Director writer service (task A4): presets map, vision inputs, context, dispatch to GGUF / API /
subscription CLIs, edit and generate modes.

The pure parts import `luna_director.*` directly (no ComfyUI). The dispatch tests load the writer
through `tests/_comfy.py`, so the writer and the tests share one `llm_prompt_node._RUNNER_LOCK`.
Every send is a recorder: no network, CLI, model or GPU.
"""
import base64
import contextlib
import inspect
import io
import json
import os
import subprocess
import sys
import threading
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PIL import Image  # noqa: E402

import _comfy  # noqa: E402
from luna_director import presets_map  # noqa: E402
from luna_director import writer as PW  # noqa: E402  (pure parts)
from luna_imaging import studio  # noqa: E402
from luna_imaging.masks import expand_box, fit_mask, mask_bbox  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "director", "assemble_cases.json")
GEMINI_T = "gemini-3.1-flash-image-preview"
FLARE = "gpt-image-2.5-flare"
SIZE = (64, 48)
BOX = (16, 12, 32, 28)
SECTIONS = ("subject", "style", "composition", "lighting", "camera")
PRESETS = {"Edit Rewrite - GPT Image": "gpt edit", "Edit Rewrite - Gemini Image": "gemini edit",
           "Edit Rewrite - Grok Imagine": "grok edit", "Generate - GPT Image": "gpt gen",
           "Generate - Gemini": "gemini gen", "Generate - Grok": "grok gen", "Image Edit": "general"}


def _canvas(size=SIZE, color=(10, 120, 200)):
    return Image.new("RGB", size, color)


def _mask(size=SIZE, box=BOX):
    m = Image.new("L", size, 0)
    m.paste(255, box)
    return m


def _req(W, **over):
    kw = dict(provider="Gemini", model="gemini-3.5-flash-lite", preset=None, target_model=GEMINI_T,
              engine="cloud", package_prompt=None, operation="inpaint", request="make the scarf red",
              canvas=_canvas(), mask=_mask(), refs=[])
    kw.update(over)
    return W.WriterRequest(**kw)


def _variant_block(i, subject, neg=None):
    lines = [f"[VARIANT {i}]", f"[SUBJECT] {subject}", f"[STYLE] style {i}",
             f"[COMPOSITION] composition {i}", f"[LIGHTING] lighting {i}", f"[CAMERA] camera {i}"]
    if neg:
        lines.append(f"[NEGATIVE] {neg}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# Pure parts (no ComfyUI)
# ---------------------------------------------------------------------------------------------

class PresetsMapTest(unittest.TestCase):
    def test_preset_for_target(self):
        f = presets_map.preset_for_target
        self.assertEqual(f("gpt-image-2.5-flare"), "Edit Rewrite - GPT Image")
        self.assertEqual(f("chatgpt-image-latest"), "Edit Rewrite - GPT Image")
        self.assertEqual(f("nano-banana-pro-preview"), "Edit Rewrite - Gemini Image")
        self.assertEqual(f(GEMINI_T), "Edit Rewrite - Gemini Image")
        self.assertEqual(f("grok-imagine-image"), "Edit Rewrite - Grok Imagine")
        self.assertIsNone(f("dall-e-3"))

    def test_preset_for_target_generate(self):
        f = presets_map.preset_for_target
        self.assertEqual(f("gpt-image-2.5-flare", "generate"), "Generate - GPT Image")
        self.assertEqual(f("gpt-image-2.5-flare", "compose"), "Generate - GPT Image")
        self.assertEqual(f("nano-banana-pro-preview", "generate"), "Generate - Gemini")
        self.assertEqual(f("grok-imagine-image", "generate"), "Generate - Grok")
        self.assertEqual(f("gpt-image-2.5-flare", "inpaint"), "Edit Rewrite - GPT Image")
        self.assertEqual(f("gpt-image-2.5-flare"), "Edit Rewrite - GPT Image")
        self.assertIsNone(f("dall-e-3", "generate"))

    def test_maps_exact(self):
        self.assertEqual(presets_map.EDIT_REWRITE, {
            "gpt-image": "Edit Rewrite - GPT Image", "chatgpt-image": "Edit Rewrite - GPT Image",
            "gemini": "Edit Rewrite - Gemini Image", "nano-banana": "Edit Rewrite - Gemini Image",
            "grok-imagine": "Edit Rewrite - Grok Imagine"})
        self.assertEqual(presets_map.GENERATE, {
            "gpt-image": "Generate - GPT Image", "chatgpt-image": "Generate - GPT Image",
            "gemini": "Generate - Gemini", "nano-banana": "Generate - Gemini", "grok-imagine": "Generate - Grok"})

    def test_presets_map_no_comfy_import(self):
        code = (f"import sys; sys.path.insert(0, {ROOT!r}); import luna_director.presets_map; "
                "bad = [m for m in ('torch', 'comfy_api', 'server', 'folder_paths', 'nodes') if m in sys.modules]; "
                "assert not bad, bad")
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class AssembleParseTest(unittest.TestCase):
    def test_assemble_prompt_matches_fixture(self):
        with open(FIXTURE, encoding="utf-8") as f:
            cases = json.load(f)["cases"]
        self.assertGreater(len(cases), 0)
        for case in cases:
            with self.subTest(case["name"]):
                self.assertEqual(PW.assemble_prompt(case["sections"]), case["prompt"])

    def test_parse_generate_output_variants_and_sections(self):
        reply = "\n\n".join([_variant_block(1, "a lighthouse keeper", "blurry"),
                             _variant_block(2, "a fox on ice", "lowres, text"),
                             _variant_block(3, "a tram at night")])
        vs = PW.parse_generate_output(reply, 3)
        self.assertEqual(len(vs), 3)
        for v in vs:
            self.assertEqual(sorted(v["sections"]), sorted(SECTIONS))
        self.assertEqual(vs[0]["sections"]["subject"], "a lighthouse keeper")
        self.assertEqual(vs[0]["positive"], "a lighthouse keeper. style 1. composition 1. lighting 1. camera 1.")
        self.assertEqual(vs[0]["negative"], "blurry")
        self.assertEqual(vs[1]["negative"], "lowres, text")
        self.assertEqual(vs[2]["negative"], "")
        for v in vs:
            self.assertNotIn("[", v["positive"])

        one = PW.parse_generate_output("A misty harbour at dawn, oil on canvas.", 1)
        self.assertEqual(one, [{"positive": "A misty harbour at dawn, oil on canvas.", "negative": "",
                                "sections": None}])

        neg = PW.parse_generate_output("A harbour\n[NEGATIVE]\nblur", 1)
        self.assertEqual((neg[0]["positive"], neg[0]["negative"], neg[0]["sections"]), ("A harbour", "blur", None))

    def test_parse_keeps_at_most_n(self):
        reply = "\n".join(_variant_block(i, f"subject {i} unique words here") for i in (1, 2, 3))
        self.assertEqual(len(PW.parse_generate_output(reply, 2)), 2)

    def test_distinct_variants(self):
        self.assertTrue(PW.distinct_variants(["a red fox in snow", "a lighthouse at dusk"]))
        self.assertFalse(PW.distinct_variants(["a red fox in snow", "A red fox in snow."]))
        self.assertTrue(PW.distinct_variants(["one"]))
        # Jaccard exactly at the threshold is not distinct.
        self.assertFalse(PW.distinct_variants(["a b c d e", "a b c d f"], threshold=4 / 6))
        self.assertTrue(PW.distinct_variants(["a b c d e", "a b c d f"], threshold=0.8))


class RegionPlanTest(unittest.TestCase):
    def test_region_plan_modes(self):
        m = _mask()
        self.assertEqual(studio.region_plan(FLARE, "inpaint", m), ("native", (0, 0) + SIZE))
        self.assertEqual(studio.region_plan(GEMINI_T, "inpaint", m),
                         ("crop", expand_box(mask_bbox(m), 0.25, SIZE)))
        self.assertEqual(studio.region_plan(GEMINI_T, "inpaint", m, crop_padding=0.5),
                         ("crop", expand_box(mask_bbox(m), 0.5, SIZE)))
        self.assertEqual(studio.region_plan(GEMINI_T, "outpaint", m), ("outpaint", (0, 0) + SIZE))
        self.assertEqual(studio.region_plan(FLARE, "outpaint", m, mask_mode="crop"), ("outpaint", (0, 0) + SIZE))
        self.assertEqual(studio.region_plan(FLARE, "inpaint", m, mask_mode="crop")[0], "crop")
        with self.assertRaisesRegex(ValueError, "mask is empty"):
            studio.region_plan(FLARE, "inpaint", Image.new("L", SIZE, 0))

    def test_studio_run_uses_region_plan(self):
        from luna_imaging.types import EditRequest, EditResult
        req = EditRequest(provider="gemini", model=GEMINI_T, operation="inpaint", prompt="x",
                          images=[_canvas()], mask=_mask())
        patch = Image.new("RGBA", (8, 8), (0, 255, 0, 255))
        with mock.patch.object(studio, "region_plan", wraps=studio.region_plan) as spy, \
                mock.patch.object(studio, "_dispatch", return_value=EditResult(images=[patch])):
            studio.run(req, "k")
        spy.assert_called_once()

    def test_region_plan_shared(self):
        cases = [(FLARE, "inpaint", "native", "Image 1 = the full picture with a mask"),
                 (GEMINI_T, "inpaint", "crop", "Image 1 = the region crop"),
                 (GEMINI_T, "outpaint", "outpaint", "Image 1 = the full picture on a larger canvas")]
        for target, op, mode, line in cases:
            with self.subTest(target=target, op=op):
                req = _req(PW, target_model=target, operation=op, refs=[("fabric", _canvas())])
                seen = []
                real = studio.region_plan

                def spy(*a, **kw):
                    out = real(*a, **kw)
                    seen.append(out)
                    return out
                with mock.patch.object(PW._studio, "region_plan", spy):
                    legend = PW.model_image_legend(req)
                self.assertEqual([s[0] for s in seen], [mode])
                self.assertTrue(legend[0].startswith(line), legend)
                self.assertEqual(legend[1], "Image 2 = reference 'fabric'")

    def test_model_legend_crop_vs_native(self):
        self.assertEqual(PW.model_image_legend(_req(PW, target_model=GEMINI_T))[0], "Image 1 = the region crop")
        self.assertIn("full picture with a mask", PW.model_image_legend(_req(PW, target_model=FLARE))[0])

    def test_model_legend_generate_and_compose(self):
        self.assertEqual(PW.model_image_legend(_req(PW, operation="generate", canvas=None, mask=None)), [])
        refs = [("character", _canvas()), ("style", _canvas())]
        self.assertEqual(PW.model_image_legend(_req(PW, operation="compose", canvas=None, mask=None, refs=refs)),
                         ["Image 1 = reference 'character'", "Image 2 = reference 'style'"])


class ImagesTest(unittest.TestCase):
    def test_mark_overlay_tints_region_only(self):
        c = _canvas()
        out = PW.mark_overlay(c, _mask())
        self.assertEqual(out.mode, "RGB")
        self.assertEqual(out.size, SIZE)
        self.assertEqual(out.getpixel((2, 2)), c.getpixel((2, 2)))
        self.assertNotEqual(out.getpixel((24, 20)), c.getpixel((24, 20)))
        self.assertEqual(out.getpixel(BOX[:2]), (255, 0, 255))  # outline

    def test_mark_overlay_rgba_on_grey(self):
        c = Image.new("RGBA", SIZE, (0, 0, 0, 0))
        out = PW.mark_overlay(c, _mask())
        self.assertEqual(out.getpixel((2, 2)), PW.NEUTRAL_GREY)

    def test_contact_sheet(self):
        items = [(f"Image {i}", _canvas((300, 200))) for i in range(1, 5)]
        sheet = PW.contact_sheet(items, max_side=512)
        self.assertEqual(sheet.mode, "RGB")
        self.assertLessEqual(max(sheet.size), 512)

    def test_writer_request_default_gguf_not_shared(self):
        a, b = PW.WriterRequest(), PW.WriterRequest()
        self.assertIsNot(a.gguf, b.gguf)
        self.assertIsNot(a.refs, b.refs)


# ---------------------------------------------------------------------------------------------
# Through the pack (ComfyUI python): vision inputs, context, dispatch
# ---------------------------------------------------------------------------------------------

class _Api:
    """A recording stand-in for write_prompt_api; `replies` are returned in order."""

    def __init__(self, *replies, error=None):
        self.replies = list(replies) or ["[POSITIVE]\nA\n[NEGATIVE]\nB"]
        self.calls = []
        self.error = error

    def __call__(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        reply = self.replies[min(len(self.calls), len(self.replies)) - 1]
        pos, neg = self.split(reply, kw.get("split_output", True))
        return pos, neg, ""

    @staticmethod
    def split(reply, do_split):
        from output_cleaner import split_positive_negative
        return split_positive_negative(reply, do_split)


class WriterDispatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.W = _comfy.load("luna_director.writer")
        cls.api = _comfy.load("llm_prompt_api_node")
        cls.node = _comfy.load("llm_prompt_node")

    def setUp(self):
        p = mock.patch.object(self.W, "_presets", lambda: dict(PRESETS))
        p.start()
        self.addCleanup(p.stop)

    def _write_api(self, req, fake=None):
        fake = fake or _Api()
        with mock.patch.object(self.api, "write_prompt_api", fake):
            out = self.W.write(req)
        return out, fake

    # ---- vision inputs -----------------------------------------------------------------------

    def test_vision_inputs_order_and_flags(self):
        W = self.W
        refs = [("fabric", _canvas(color=(1, 2, 3))), ("style", _canvas(color=(4, 5, 6)))]
        items = W.build_vision_inputs(_req(W, refs=refs))
        self.assertEqual(len(items), 4)
        for i, (label, img) in enumerate(items, 1):
            self.assertTrue(label.startswith(f"Image {i}: "), label)
            self.assertIsInstance(img, Image.Image)
        self.assertIn("tinted", items[0][0])
        self.assertIn("close-up", items[1][0])
        self.assertIn("'fabric'", items[2][0])
        self.assertIn("'style'", items[3][0])
        self.assertNotEqual(items[0][1].getpixel((24, 20)), _canvas().getpixel((24, 20)))

        plain = W.build_vision_inputs(_req(W, refs=refs, send_mask=False))
        self.assertEqual(len(plain), 3)
        self.assertEqual(plain[0][1].getpixel((24, 20)), _canvas().getpixel((24, 20)))
        self.assertNotIn("tinted", plain[0][0])
        self.assertNotIn("close-up", " ".join(l for l, _ in plain))

        self.assertEqual(len(W.build_vision_inputs(_req(W, refs=refs, send_refs=False))), 2)
        self.assertEqual(len(W.build_vision_inputs(_req(W, refs=refs, send_canvas=False))), 3)

    def test_crop_box_matches_core(self):
        W = self.W
        big = (640, 480)
        m = _mask(big, (100, 90, 300, 250))
        items = W.build_vision_inputs(_req(W, canvas=_canvas(big), mask=m, vision_mp=0))
        x0, y0, x1, y1 = expand_box(mask_bbox(fit_mask(m, big)), 0.25, big)
        self.assertEqual(items[1][1].size, (x1 - x0, y1 - y0))

    def test_vision_inputs_downscaled_and_flattened(self):
        W = self.W
        ref = Image.new("RGBA", (2000, 1000), (0, 0, 0, 0))
        items = W.build_vision_inputs(_req(W, canvas=_canvas((2000, 1000)), mask=_mask((2000, 1000), (10, 10, 50, 50)),
                                           refs=[("x", ref)], vision_mp=0.5))
        for _, img in items:
            self.assertLessEqual(img.width * img.height, 500_000)
            self.assertEqual(img.mode, "RGB")
        self.assertEqual(items[-1][1].getpixel((5, 5)), W.NEUTRAL_GREY)

    def test_generate_vision_inputs_refs_only(self):
        W = self.W
        refs = [("character", _canvas(color=(1, 1, 1))), ("style", _canvas(color=(2, 2, 2)))]
        items = W.build_vision_inputs(_req(W, operation="compose", canvas=None, mask=None, refs=refs))
        self.assertEqual([l.split(":")[0] for l, _ in items], ["Image 1", "Image 2"])
        self.assertIn("'character'", items[0][0])
        self.assertEqual(items[0][1].getpixel((0, 0)), (1, 1, 1))
        self.assertEqual(W.build_vision_inputs(_req(W, operation="generate", canvas=None, mask=None)), [])

    # ---- context -----------------------------------------------------------------------------

    def test_context_names_target_and_operation(self):
        W = self.W
        req = _req(W, refs=[("fabric", _canvas())])
        ctx = W.build_context(req, [l for l, _ in W.build_vision_inputs(req)], W.model_image_legend(req))
        self.assertIn(f"TARGET IMAGE MODEL: {GEMINI_T} (cloud)", ctx)
        self.assertIn("OPERATION: inpaint", ctx)
        self.assertIn("YOUR IMAGES:", ctx)
        self.assertIn("THE IMAGE MODEL WILL RECEIVE:", ctx)
        self.assertIn("Image 1 = the region crop, Image 2 = reference 'fabric'", ctx)
        self.assertNotIn("make the scarf red", ctx)  # the request goes in as user_prompt only

    def test_generate_context_values(self):
        W = self.W
        req = _req(W, operation="generate", canvas=None, mask=None, variants=3, sections=True,
                   exact_text="SEA WATCH 1887", target_model=FLARE)
        ctx = W.build_context(req, [], W.model_image_legend(req))
        self.assertIn("OPERATION: generate", ctx)
        self.assertIn("VARIANTS: 3", ctx)
        self.assertIn("SECTIONS: subject, style, composition, lighting, camera", ctx)
        self.assertIn('EXACT TEXT: "SEA WATCH 1887"', ctx)
        self.assertNotIn("THE IMAGE MODEL WILL RECEIVE", ctx)

    # ---- dispatch: API and CLIs --------------------------------------------------------------

    def test_dispatch_api_passes_png_b64(self):
        W = self.W
        res, fake = self._write_api(_req(W, refs=[("fabric", _canvas())]))
        kw = fake.calls[0]
        self.assertEqual(len(kw["images_b64"]), 3)
        for b in kw["images_b64"]:
            im = Image.open(io.BytesIO(base64.b64decode(b)))
            self.assertEqual(im.format, "PNG")
        self.assertEqual(kw["provider"], "Gemini")
        self.assertEqual(kw["model_name"], "gemini-3.5-flash-lite")
        self.assertEqual(kw["user_prompt"], "make the scarf red")
        self.assertEqual(kw["system_prompt"], "Edit Rewrite - Gemini Image")
        self.assertEqual(kw["custom_system_prompt"], "gemini edit")
        self.assertTrue(kw["split_output"])
        self.assertIn("TARGET IMAGE MODEL", kw["context"])
        self.assertEqual((res.positive, res.negative), ("A", "B"))
        self.assertEqual(res.preset_used, "Edit Rewrite - Gemini Image")
        self.assertEqual(res.variants, [{"positive": "A", "negative": "B", "sections": None}])
        self.assertEqual(len(res.legend), 3)
        self.assertGreaterEqual(res.seconds, 0)

    def test_user_preset_wins(self):
        res, fake = self._write_api(_req(self.W, preset="Image Edit"))
        self.assertEqual(fake.calls[0]["system_prompt"], "Image Edit")
        self.assertEqual(res.preset_used, "Image Edit")

    def test_custom_passes_server_url(self):
        _, fake = self._write_api(_req(self.W, provider="Custom", model="local-model",
                                       server_url="http://127.0.0.1:8080/v1"))
        self.assertEqual(fake.calls[0]["server_url"], "http://127.0.0.1:8080/v1")

    def test_grok_supergrok_packs_one_image(self):
        W = self.W
        refs = [("fabric", _canvas())]
        _, fake = self._write_api(_req(W, provider="Grok (SuperGrok)", model="grok-4.5", refs=refs))
        self.assertEqual(len(fake.calls[0]["images_b64"]), 1)
        self.assertIn("contact sheet", fake.calls[0]["context"])
        _, fake = self._write_api(_req(W, provider="Grok (SuperGrok)", model="grok-4.5", refs=refs,
                                       pack_images=False))
        self.assertEqual(len(fake.calls[0]["images_b64"]), 3)
        _, fake = self._write_api(_req(W, refs=refs, pack_images=True))
        self.assertEqual(len(fake.calls[0]["images_b64"]), 1)

    def test_inpaint_sends_no_canvas_size(self):
        W = self.W
        _, fake = self._write_api(_req(W))
        self.assertEqual((fake.calls[0]["width"], fake.calls[0]["height"]), (0, 0))
        _, fake = self._write_api(_req(W, operation="outpaint"))
        self.assertEqual((fake.calls[0]["width"], fake.calls[0]["height"]), SIZE)

    def test_negative_off_drops_negative_and_never_leaks(self):
        res, fake = self._write_api(_req(self.W, negative=False), _Api("A\n[NEGATIVE]\nB"))
        self.assertEqual((res.positive, res.negative), ("A", ""))
        self.assertTrue(fake.calls[0]["split_output"])
        self.assertIn("positive prompt only", fake.calls[0]["context"])
        self.assertEqual(res.variants[0]["negative"], "")

    def test_package_engine_refused_in_stage_a(self):
        W = self.W
        req = _req(W, engine="package", package_prompt="sys")
        fake = _Api()
        with mock.patch.object(self.api, "write_prompt_api", fake):
            with self.assertRaises(W.WriterError) as cm:
                W.write(req)
        self.assertEqual(cm.exception.code, "provider")
        self.assertIn("stage B", str(cm.exception))
        self.assertEqual(req.request, "make the scarf red")
        self.assertEqual(fake.calls, [])

    def test_write_error_is_named_and_request_kept(self):
        W = self.W
        req = _req(W, provider="Claude (Max)", model="claude-haiku-4-5")
        with mock.patch.object(self.api, "write_prompt_api", _Api(error=RuntimeError("Not logged in"))):
            with self.assertRaises(W.WriterError) as cm:
                W.write(req)
        self.assertEqual(cm.exception.code, "provider")
        self.assertIn("Not logged in", str(cm.exception))
        self.assertEqual(req.request, "make the scarf red")

    def test_error_codes(self):
        W = self.W
        cases = [(RuntimeError("Gemini requires an API key. Two ways to set it"), "no_key"),
                 (RuntimeError("The `claude` CLI was not found on PATH (looked also in [])."), "cli_missing"),
                 (RuntimeError("claude.exe did not answer within 180s."), "timeout"),
                 (RuntimeError("Timed out after 180.0s waiting for http://x"), "timeout"),
                 (TimeoutError("slow"), "timeout"),
                 (RuntimeError("API error: bad request"), "provider")]
        for err, code in cases:
            with self.subTest(code=code):
                with mock.patch.object(self.api, "write_prompt_api", _Api(error=err)):
                    with self.assertRaises(W.WriterError) as cm:
                        W.write(_req(W))
                self.assertEqual(cm.exception.code, code)

    def test_missing_preset_is_named(self):
        W = self.W
        with mock.patch.object(W, "_presets", lambda: {}):
            with self.assertRaises(W.WriterError) as cm:
                self._write_api(_req(W))
        self.assertEqual(cm.exception.code, "provider")
        self.assertEqual(str(cm.exception), "preset missing: Edit Rewrite - Gemini Image")
        with self.assertRaises(W.WriterError) as cm:
            self._write_api(_req(W, target_model="dall-e-3"))
        self.assertIn("dall-e-3", str(cm.exception))

    def test_thinking_switch_maps_per_route(self):
        W = self.W
        for thinking, effort, level, budget in ((False, "none", "None", 0), (True, "medium", "medium", 8192)):
            with self.subTest(thinking=thinking):
                _, fake = self._write_api(_req(W, provider="OpenAI", model="gpt-5.6-luna", thinking=thinking))
                kw = fake.calls[0]
                self.assertEqual(kw["disable_thinking"], not thinking)
                self.assertEqual(kw["reasoning_effort"], effort)
                _, fake = self._write_api(_req(W, provider="Claude (Max)", model="claude-haiku-4-5",
                                               thinking=thinking))
                self.assertEqual(fake.calls[0]["reasoning_effort"], effort)
                # Gemini 3: thinking level (the node maps "None" to its lowest, "low").
                _, fake = self._write_api(_req(W, thinking=thinking))
                self.assertEqual(fake.calls[0]["gemini_thinking_level"], level)
                # Gemini 2.5: a thinking budget (0 = off), no level.
                _, fake = self._write_api(_req(W, model="gemini-2.5-flash", thinking=thinking))
                self.assertEqual(fake.calls[0]["gemini_thinking_budget"], budget)
                self.assertEqual(fake.calls[0]["gemini_thinking_level"], "None")
        # The CLI route turns the mapped effort into its own flag: off is its lowest effort.
        self.assertEqual(self.api._cli_effort("none"), "low")
        self.assertEqual(self.api._cli_effort("medium"), "medium")

    # ---- generate mode -----------------------------------------------------------------------

    def _gen_req(self, **over):
        kw = dict(operation="generate", canvas=None, mask=None, target_model=FLARE, size=(1536, 1024),
                  request="a lighthouse keeper")
        kw.update(over)
        return _req(self.W, **kw)

    def test_generate_sends_size_for_canvas_block(self):
        _, fake = self._write_api(self._gen_req())
        self.assertEqual((fake.calls[0]["width"], fake.calls[0]["height"]), (1536, 1024))
        self.assertEqual(fake.calls[0]["system_prompt"], "Generate - GPT Image")

    def test_generate_calls_without_split(self):
        _, fake = self._write_api(self._gen_req())
        self.assertFalse(fake.calls[0]["split_output"])

    def test_refine_sends_result_first_with_feedback(self):
        W = self.W
        result = _canvas(color=(9, 9, 9))
        req = self._gen_req(result=result, prior_prompt="a keeper on rocks", feedback="warmer light",
                            refs=[("style", _canvas(color=(7, 7, 7)))])
        items = W.build_vision_inputs(req)
        self.assertTrue(items[0][0].startswith("Image 1"))
        self.assertIn("result", items[0][0])
        self.assertEqual(items[0][1].getpixel((0, 0)), (9, 9, 9))
        self.assertEqual(items[1][1].getpixel((0, 0)), (7, 7, 7))
        _, fake = self._write_api(req)
        ctx = fake.calls[0]["context"]
        self.assertIn("PREVIOUS PROMPT:\na keeper on rocks", ctx)
        self.assertIn("FEEDBACK:\nwarmer light", ctx)
        self.assertEqual(fake.calls[0]["user_prompt"], "a lighthouse keeper")
        self.assertEqual(req.request, "a lighthouse keeper")

    def test_varied_variants_distinct(self):
        W = self.W
        dup = "\n\n".join([_variant_block(1, "a lighthouse keeper on wet rocks"),
                           _variant_block(2, "a lighthouse keeper on wet rocks"),
                           _variant_block(3, "a fox crossing a frozen lake")])
        ok3 = "\n\n".join([_variant_block(1, "a lighthouse keeper on wet rocks"),
                           _variant_block(2, "a tram in neon rain"),
                           _variant_block(3, "a fox crossing a frozen lake")])
        fix = _variant_block(1, "an old diver mending nets at the pier")
        # Two equal of three -> exactly one retry listing ALREADY USED; the retry fills the gap.
        res, fake = self._write_api(self._gen_req(variants=3, sections=True), _Api(dup, fix))
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("ALREADY USED:", fake.calls[1]["context"])
        self.assertIn("VARIANTS: 1", fake.calls[1]["context"])
        self.assertNotIn("ALREADY USED:", fake.calls[0]["context"])
        self.assertEqual(len(res.variants), 3)
        self.assertTrue(W.distinct_variants([v["positive"] for v in res.variants]))
        self.assertEqual(res.positive, res.variants[0]["positive"])
        # The retry still duplicates -> named error, never padded.
        with self.assertRaises(W.WriterError) as cm:
            self._write_api(self._gen_req(variants=3, sections=True), _Api(dup, dup))
        self.assertEqual(cm.exception.code, "provider")
        self.assertEqual(str(cm.exception), "writer returned 2 distinct prompts of 3")
        # Three distinct -> no retry.
        res, fake = self._write_api(self._gen_req(variants=3, sections=True), _Api(ok3))
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(res.variants), 3)

    def test_generate_negative_off_drops_every_negative(self):
        reply = "\n\n".join([_variant_block(1, "a keeper", "blur"), _variant_block(2, "a tram in rain", "text")])
        res, fake = self._write_api(self._gen_req(variants=2, negative=False), _Api(reply))
        self.assertEqual([v["negative"] for v in res.variants], ["", ""])
        self.assertEqual(res.negative, "")
        self.assertIn("positive prompt only", fake.calls[0]["context"])

    # ---- dispatch: local GGUF ----------------------------------------------------------------

    def _gguf(self, req, reply=("A", "B", "")):
        seen = {}

        def fake_generate(**kw):
            seen.update(kw)
            return reply
        with mock.patch.object(self.node._RUNNER, "generate", fake_generate), \
                contextlib.redirect_stdout(io.StringIO()):
            res = self.W.write(req)
        return res, seen

    def test_dispatch_gguf_uses_media_override(self):
        W = self.W
        req = _req(W, provider="Local GGUF", model="Qwen3-VL-8B.gguf", refs=[("fabric", _canvas())])
        res, kw = self._gguf(req)
        media = kw["media_override"]
        self.assertEqual([m["type"] for m in media], ["text", "image_url"] * 3)
        self.assertTrue(media[0]["text"].startswith("Image 1"))
        self.assertTrue(media[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(kw["model_name"], "Qwen3-VL-8B.gguf")
        self.assertEqual(kw["user_prompt"], "make the scarf red")
        self.assertEqual(kw["system_prompt"], "Edit Rewrite - Gemini Image")
        self.assertEqual(kw["custom_system_prompt"], "gemini edit")
        self.assertIn("TARGET IMAGE MODEL", kw["context"])
        self.assertEqual((kw["width"], kw["height"]), (0, 0))
        self.assertTrue(kw["split_output"])
        self.assertNotIn("image", kw)
        self.assertEqual((res.positive, res.negative), ("A", "B"))
        self.assertFalse(self.node._RUNNER_LOCK.locked())

    def test_gguf_generate_without_images_sends_size(self):
        _, kw = self._gguf(self._gen_req(provider="Local GGUF", model="q.gguf"), reply=("A", "", ""))
        self.assertEqual(kw["media_override"], [])
        self.assertEqual((kw["width"], kw["height"]), (1536, 1024))
        self.assertFalse(kw["split_output"])

    def test_gguf_kwargs_cover_generate_signature(self):
        W = self.W
        with contextlib.redirect_stdout(io.StringIO()):
            kw = W.gguf_kwargs(_req(W, provider="Local GGUF", model="q.gguf", gguf={"n_ctx": 8192, "seed": 7}),
                               context="C", media=[], width=0, height=0, split_output=True)
        sig = inspect.signature(self.node._LLMRunner.generate)
        required = [n for n, p in sig.parameters.items()
                    if n != "self" and p.default is inspect.Parameter.empty]
        for name in required:
            self.assertIn(name, kw)
        self.assertTrue(set(kw) <= set(sig.parameters))
        self.assertEqual((kw["n_ctx"], kw["seed"]), (8192, 7))

    def test_gguf_thinking(self):
        W = self.W
        _, kw = self._gguf(_req(W, provider="Local GGUF", model="Qwen3-VL-8B.gguf", thinking=False))
        self.assertTrue(kw["disable_thinking"])
        self.assertTrue(kw["auto_settings"])
        _, kw = self._gguf(_req(W, provider="Local GGUF", model="Qwen3-VL-8B.gguf", thinking=True))
        self.assertFalse(kw["disable_thinking"])
        # auto_settings would force thinking off, so the writer applies the family's settings itself.
        self.assertFalse(kw["auto_settings"])
        resolved = self.node._resolve_model_settings("qwen3-vl-8b.gguf")
        self.assertEqual(kw["temperature"], resolved["temperature"])
        self.assertEqual(kw["top_k"], resolved["top_k"])

    def test_gguf_busy(self):
        W = self.W
        lock = self.node._RUNNER_LOCK
        calls = []
        req = _req(W, provider="Local GGUF", model="q.gguf", timeout=0.1)
        self.assertTrue(lock.acquire(timeout=1))
        try:
            with mock.patch.object(self.node._RUNNER, "generate", lambda **kw: calls.append(kw)):
                with self.assertRaises(W.WriterError) as cm:
                    W.write(req)
        finally:
            lock.release()
        self.assertEqual(cm.exception.code, "busy")
        self.assertIn("request kept", str(cm.exception))
        self.assertEqual(calls, [])
        self.assertEqual(req.request, "make the scarf red")

    def test_gguf_lock_wait_is_short(self):
        W = self.W
        self.assertLessEqual(W.GGUF_LOCK_WAIT_S, 5)
        lock = self.node._RUNNER_LOCK
        req = _req(W, provider="Local GGUF", model="q.gguf", timeout=180)
        waits = []

        class _Lock:
            def acquire(self, timeout=-1):
                waits.append(timeout)
                return False

            def release(self):  # pragma: no cover - never acquired
                raise AssertionError
        with mock.patch.object(self.node, "_RUNNER_LOCK", _Lock()):
            with self.assertRaises(W.WriterError):
                W.write(req)
        self.assertEqual(waits, [W.GGUF_LOCK_WAIT_S])
        self.assertFalse(lock.locked())

    def test_gpu_guard_wraps_gguf_only(self):
        W = self.W
        kinds = []

        @contextlib.contextmanager
        def guard(kind):
            kinds.append(kind)
            yield
        with mock.patch.object(W, "gpu_guard", guard):
            self._gguf(_req(W, provider="Local GGUF", model="q.gguf"))
            self._write_api(_req(W))
        self.assertEqual(kinds, ["gguf"])


if __name__ == "__main__":
    unittest.main()
