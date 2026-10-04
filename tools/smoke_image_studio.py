"""Live smoke for Luna Image Studio (phase 1). MANUAL - spends real money (< $0.25 total).

Run with the ComfyUI python (it ignores cwd / PYTHONPATH, so the repo root is inserted below):

    E:\\ComfyUI-Easy-Install\\python_embeded\\python.exe tools\\smoke_image_studio.py [--only openai|gemini|xai|outpaint] [--out DIR] [--dry]

--dry swaps the three provider run() functions for a fake that returns a solid patch, so the
script itself can be checked without a key or a network call. Keys are read from the
environment, then from E:\\ComfyUI-Easy-Install\\ComfyUI\\.env; they are never printed or saved.
Exit code = number of FAILs.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import Image, ImageChops, ImageDraw  # noqa: E402

from luna_imaging import studio  # noqa: E402
from luna_imaging.types import EditRequest, EditResult  # noqa: E402

ENV_FILE = r"E:\ComfyUI-Easy-Install\ComfyUI\.env"
DEFAULT_OUT = (r"C:\Users\Peti\AppData\Local\Temp\claude\D--Claude-LLM-Prompt"
               r"\0f9334b4-38b1-42fe-85e8-b963228fae33\scratchpad\smoke")
KEY_NAMES = {
    "openai": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_API_KEY"),
    "xai": ("XAI_API_KEY", "GROK_API_KEY"),
}
INPAINT_PROMPT = "a red apple on the table"
OUTPAINT_PROMPT = "more of the same scene, matching colours, lighting and style"


def load_env_file(path: str) -> dict:
    """KEY=VALUE lines, # comments. Returned as a local dict only - never put in os.environ."""
    out: dict = {}
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k.lower().startswith("export "):
                    k = k[7:].strip()
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[k] = v
    except OSError:
        pass
    return out


def find_key(provider: str, env_file: dict) -> str:
    for name in KEY_NAMES[provider]:
        val = os.environ.get(name) or env_file.get(name)
        if val:
            return val
    return ""


def make_test_image(size: int = 512) -> Image.Image:
    """Gradient + shapes, so a seam or a shifted pixel is obvious."""
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            px[x, y] = (x * 255 // (size - 1), y * 255 // (size - 1), 160)
    d = ImageDraw.Draw(img)
    d.ellipse((40, 40, 200, 200), fill=(250, 220, 40), outline=(0, 0, 0), width=4)
    d.rectangle((330, 60, 470, 160), fill=(30, 90, 200), outline=(255, 255, 255), width=4)
    d.polygon([(60, 440), (160, 340), (260, 440)], fill=(20, 160, 80), outline=(0, 0, 0))
    d.line((0, 256, size, 256), fill=(255, 255, 255), width=3)
    d.line((256, 0, 256, size), fill=(0, 0, 0), width=3)
    return img


def make_mask(size: int = 512, lo: int = 192, hi: int = 320) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    m.paste(255, (lo, lo, hi, hi))
    return m


def install_fakes() -> None:
    """--dry: replace the provider run() functions with a solid-colour patch of the sent size."""
    def fake(req, key, mask=None):
        base = req.images[0]
        patch = Image.new("RGBA", base.size, (200, 30, 30, 255))
        return EditResult(images=[patch], cost_usd=0.0, info=["FAKE provider (--dry)"])
    studio._openai.run = fake
    studio._gemini.run = lambda req, key: fake(req, key)
    studio._xai.run = lambda req, key: fake(req, key)


def build_cases(src: Image.Image, mask: Image.Image) -> list[dict]:
    def inpaint(provider, model, **kw):
        return EditRequest(provider=provider, model=model, operation="inpaint",
                           prompt=INPAINT_PROMPT, images=[src.copy()], mask=mask.copy(),
                           n=1, max_retries=0, **kw)
    return [
        {"name": "openai", "provider": "openai",
         "req": inpaint("openai", "gpt-image-2.5-flare", quality="low")},
        {"name": "gemini", "provider": "gemini",
         "req": inpaint("gemini", "gemini-3.1-flash-image", resolution="1K")},
        {"name": "xai", "provider": "xai",
         "req": inpaint("xai", "grok-imagine-image", resolution="1K")},
        {"name": "outpaint", "provider": "openai",
         "req": EditRequest(provider="openai", model="gpt-image-2.5-flare", operation="outpaint",
                            prompt=OUTPAINT_PROMPT, images=[src.copy()], quality="low",
                            outpaint=(256, 0, 256, 0), n=1, max_retries=0)},
    ]


def check_outside_unchanged(case: dict, src: Image.Image, result: Image.Image,
                            mask_used: Image.Image) -> str | None:
    """None if OK, else a failure reason. Compares only where the used mask is 0."""
    req = case["req"]
    if req.operation == "outpaint":
        left, top, right, bottom = req.outpaint
        expect = (src.width + left + right, src.height + top + bottom)
        if result.size != expect:
            return f"result size {result.size}, expected {expect}"
        ref = Image.new("RGB", expect, (0, 0, 0))
        ref.paste(src.convert("RGB"), (left, top))
    else:
        if result.size != src.size:
            return f"result size {result.size}, expected {src.size}"
        ref = src.convert("RGB")
    if mask_used is None or mask_used.size != result.size:
        return "no usable mask returned"
    keep = mask_used.convert("L").point(lambda v: 255 if v == 0 else 0)
    diff = ImageChops.difference(result.convert("RGB"), ref)
    black = Image.new("RGB", diff.size, (0, 0, 0))
    outside = Image.composite(diff, black, keep)
    hi = max(b[1] for b in outside.getextrema())
    if hi != 0:
        return f"outside-mask pixels changed (max diff {hi})"
    if keep.getbbox() is None:
        return "mask covers everything - nothing to compare"
    return None


def run_case(case: dict, key: str, src: Image.Image, mask: Image.Image, out_dir: str) -> tuple[bool, float]:
    name = case["name"]
    req = case["req"]
    t0 = time.time()
    try:
        result, mask_used = studio.run(req, key)
    except Exception as exc:  # noqa: BLE001 - a smoke reports every failure and goes on
        print(f"FAIL {name}: {type(exc).__name__}: {_scrub(str(exc), key)}")
        return False, 0.0
    elapsed = time.time() - t0
    cost = result.cost_usd or 0.0

    src.save(os.path.join(out_dir, f"{name}_original.png"))
    if mask_used is not None:
        mask_used.save(os.path.join(out_dir, f"{name}_mask.png"))
    for line in result.info:
        print(f"  info: {_scrub(str(line), key)}")
    print(f"  cost: {('$%.4f' % result.cost_usd) if result.cost_usd is not None else 'n/a'}"
          f"   elapsed: {elapsed:.1f}s")
    if not result.images:
        print(f"FAIL {name}: no image returned")
        return False, cost
    out = result.images[0]
    out.save(os.path.join(out_dir, f"{name}_result.png"))
    reason = check_outside_unchanged(case, src, out, mask_used)
    if reason:
        print(f"FAIL {name}: {reason}")
        return False, cost
    print(f"OK {name}")
    return True, cost


def _scrub(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Live smoke for Luna Image Studio (spends money).")
    ap.add_argument("--only", choices=["openai", "gemini", "xai", "outpaint"])
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--dry", action="store_true", help="fake providers, no network, no keys")
    args = ap.parse_args(argv)

    os.makedirs(args.out, exist_ok=True)
    env_file = {} if args.dry else load_env_file(ENV_FILE)
    if args.dry:
        install_fakes()

    src, mask = make_test_image(), make_mask()
    fails, total = 0, 0.0
    for case in build_cases(src, mask):
        if args.only and case["name"] != args.only:
            continue
        print(f"== {case['name']}: {case['req'].operation} on {case['req'].model}")
        key = "dry-run" if args.dry else find_key(case["provider"], env_file)
        if not key:
            print(f"SKIP {case['name']}: no key")
            continue
        ok, cost = run_case(case, key, src, mask, args.out)
        total += cost
        fails += 0 if ok else 1
    print(f"TOTAL cost: ${total:.4f}   FAILs: {fails}   out: {args.out}")
    return fails


if __name__ == "__main__":
    sys.exit(main())
