from __future__ import annotations

from dataclasses import dataclass, field

from PIL import Image


@dataclass
class EditRequest:
    provider: str  # "openai" | "gemini" | "xai"
    model: str
    operation: str  # "generate" | "edit" | "inpaint" | "outpaint" | "compose"
    prompt: str
    images: list[Image.Image] = field(default_factory=list)
    mask: Image.Image | None = None  # mode "L", 255 = region to change
    aspect_ratio: str = "auto"
    resolution: str = "1K"
    n: int = 1
    quality: str = "auto"
    background: str = "auto"
    outpaint: tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, right, bottom px
    mask_mode: str = "auto"  # "auto" | "native" | "crop"
    crop_padding: float = 0.25
    feather_px: int = 16
    width: int = 0
    height: int = 0
    timeout: float = 600.0
    max_retries: int = 2
    extra: dict = field(default_factory=dict)


@dataclass
class EditResult:
    images: list[Image.Image] = field(default_factory=list)  # RGBA
    text: str = ""
    cost_usd: float | None = None
    info: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
