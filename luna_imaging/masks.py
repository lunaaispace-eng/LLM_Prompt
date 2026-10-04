"""Mask maths for the image studio. Pillow only: no numpy, no torch, no ComfyUI.

Masks are PIL mode "L"; 255 = region to change.
"""
import io

from PIL import Image, ImageChops, ImageFilter, ImageMath


def fit_mask(mask, size):
    """Any-mode mask -> L at `size`, NEAREST resized, hard 0/255 (threshold > 127)."""
    m = mask.convert("L")
    if m.size != tuple(size):
        m = m.resize(tuple(size), Image.NEAREST)
    return m.point(lambda v: 255 if v > 127 else 0)


def mask_bbox(mask):
    """(x0, y0, x1, y1) with exclusive end of the non-zero area, or None when empty."""
    return mask.convert("L").getbbox()


def expand_box(box, padding, size):
    """Grow each side by padding x the box side, clamped to the image."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    px, py = round(w * padding), round(h * padding)
    return (max(0, x0 - px), max(0, y0 - py), min(size[0], x1 + px), min(size[1], y1 + py))


def boxes_to_mask(boxes, size, normalized):
    """Union of [x, y, w, h] boxes as an L mask. Normalized boxes scale by the image size."""
    W, H = size
    mask = Image.new("L", (W, H), 0)
    for x, y, w, h in boxes:
        if normalized:
            x, y, w, h = x * W, y * H, w * W, h * H
        x0, y0 = max(0, min(W, round(x))), max(0, min(H, round(y)))
        x1, y1 = max(0, min(W, round(x + w))), max(0, min(H, round(y + h)))
        if x1 > x0 and y1 > y0:
            mask.paste(255, (x0, y0, x1, y1))
    return mask


def openai_alpha_mask(mask, size):
    """PNG bytes of an RGBA image: black, alpha 0 where the mask is 255, 255 elsewhere."""
    m = fit_mask(mask, size)
    alpha = m.point(lambda v: 0 if v else 255)
    img = Image.new("RGBA", tuple(size), (0, 0, 0, 255))
    img.putalpha(alpha)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def outpaint_canvas(img, margins, fill=(127, 127, 127)):
    """Pad `img` by margins (left, top, right, bottom). Returns (canvas, mask); mask is 255 on the new area."""
    left, top, right, bottom = margins
    w, h = img.size
    canvas = Image.new("RGB", (w + left + right, h + top + bottom), fill)
    canvas.paste(img.convert("RGB"), (left, top))
    mask = Image.new("L", canvas.size, 255)
    mask.paste(0, (left, top, left + w, top + h))
    return canvas, mask


def _feather_weight(hard, feather_px):
    """Inward-only feather, normalised locally so every region's core reaches full strength.

    weight = hard * min(1, blur / D), D = max of the blur (inside the mask only) within `feather_px` (about 2 sigma: far
    enough to see a wide region's interior, so wide regions are normalised by ~255 = unchanged, and
    near enough that a distant island is normalised by its own peak, not by another region's).
    """
    blurred = hard.filter(ImageFilter.GaussianBlur(feather_px / 2))
    inside = ImageChops.multiply(blurred, hard)  # blur tails outside a region must not count as its peak
    f = max(1, int(feather_px) // 8)
    if f > 1:  # max-pool (never average) so the neighbourhood max is not underestimated
        pooled = inside.filter(ImageFilter.MaxFilter(f | 1))
        small = pooled.resize((max(1, hard.width // f), max(1, hard.height // f)), Image.NEAREST)
    else:
        small = inside
    r = max(1, -(-int(feather_px) // f))
    denom = small.filter(ImageFilter.MaxFilter(2 * r + 1))
    if f > 1:
        denom = denom.resize(hard.size, Image.NEAREST)
    denom = ImageChops.lighter(denom, blurred).point(lambda v: max(v, 1))
    ratio = ImageMath.lambda_eval(lambda a: a["x"] * 255 / a["y"],
                                  x=blurred.convert("F"), y=denom.convert("F")).convert("L")
    return ImageChops.multiply(ratio, hard)


def composite(base, patch, box, mask, feather_px):
    """Blend `patch` (resized to `box`) into `base` inside the box, weighted by the mask.

    `mask` is full image size. Feather is inward only: the weight is 0 wherever the mask is 0, so
    the original stays exactly as it was. The box is clamped to the base. Result mode follows base
    (RGB -> RGB, RGBA -> RGBA).
    """
    out_mode = "RGBA" if base.mode == "RGBA" else "RGB"
    out = base.convert(out_mode)
    x0, y0, x1, y1 = box
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(out.width, x1), min(out.height, y1)
    if x1 <= x0 or y1 <= y0:
        return out
    box = (x0, y0, x1, y1)
    bw, bh = x1 - x0, y1 - y0
    hard = mask.convert("L")
    if hard.size != out.size:
        hard = hard.resize(out.size, Image.NEAREST)
    hard = hard.crop(box)
    weight = _feather_weight(hard, feather_px) if feather_px and feather_px > 0 else hard
    # A transparent patch pixel keeps the original (the API may return the edit area as alpha 0).
    if "A" in patch.getbands():
        weight = ImageChops.multiply(weight, patch.getchannel("A").resize((bw, bh), Image.LANCZOS))
    p = patch.convert("RGB").resize((bw, bh), Image.LANCZOS)
    region = out.crop(box)
    if out_mode == "RGBA":
        p = p.convert("RGBA")
        p.putalpha(region.getchannel("A"))
    out.paste(Image.composite(p, region, weight), (x0, y0))
    return out
