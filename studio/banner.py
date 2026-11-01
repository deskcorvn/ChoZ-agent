"""banner: listing + the real cut-out → an animated product banner (GIF + MP4) for the seller's shop. Meta only.

Muse Spark plans the banner as data (`BannerPlan`, json_schema): headline, subline, call to action, an accent colour,
a prompt for the empty background, and a short timeline of effects from a fixed vocabulary. Muse Image paints the
background (no product, no text). Code animates it: the product is the seller's own pixels (SAM 3.1 cut-out),
never redrawn; the price is written from the listing, never by the model. About $0.012 and 10 s per banner.
"""
from __future__ import annotations

import io
import math
import re
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pydantic import BaseModel, Field

from app.config import Settings
from studio import log, require_prompt, work_dir

ROLE = "designer"
EFFECTS = ("product_rise", "headline_slide", "price_pop", "shine", "cta_pulse")
GIF_SIDE = 540          # a shop banner GIF stays a few MB; the MP4 keeps the full size
BANNED = ("nhất", "số một", "số 1", "duy nhất", "100%", "đầu tiên", "best", "#1")


class BannerInput(BaseModel):
    listing: dict
    packshot_path: str                               # RGBA cut-out of the real product (SAM 3.1)
    photos: list[str] = []                           # the seller's photos, for the palette
    work_dir: str | None = None
    size: int = Field(default=1080, ge=320, le=1440)  # square
    seconds: float = Field(default=4.0, ge=2.0, le=8.0)
    fps: int = Field(default=15, ge=8, le=30)


class Beat(BaseModel):
    effect: Literal["product_rise", "headline_slide", "price_pop", "shine", "cta_pulse"]
    start_s: float = Field(ge=0.0)
    end_s: float = Field(ge=0.0)


class BannerPlan(BaseModel):
    headline: str = Field(max_length=32)
    subline: str = Field(default="", max_length=48)
    cta: str = Field(max_length=28)
    accent: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    background_prompt: str
    beats: list[Beat] = Field(min_length=2, max_length=8)


class Banner(BaseModel):
    plan: BannerPlan
    price_text: str
    background_path: str
    gif_path: str
    mp4_path: str | None = None
    frames: int
    seconds: float


INPUT, OUTPUT = BannerInput, Banner

PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["headline", "subline", "cta", "accent", "background_prompt", "beats"],
    "properties": {
        "headline": {"type": "string"}, "subline": {"type": "string"}, "cta": {"type": "string"},
        "accent": {"type": "string"}, "background_prompt": {"type": "string"},
        "beats": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["effect", "start_s", "end_s"],
            "properties": {"effect": {"type": "string", "enum": list(EFFECTS)},
                           "start_s": {"type": "number"}, "end_s": {"type": "number"}}}},
    },
}

PROMPT = """Bạn là designer banner động cho gian hàng nhỏ trên Chợ Z (Việt Nam). Từ listing (JSON) và ảnh sản phẩm,
lên kế hoạch một banner vuông dài {seconds} giây, trả JSON theo schema:
- headline: ≤ 28 ký tự, tiếng Việt, nói món gì (lấy từ listing), không bịa công dụng.
- subline: ≤ 44 ký tự, một chi tiết CÓ trong listing hoặc đọc được trên bao bì (khối lượng, loại hộp, xuất xứ).
- cta: ≤ 24 ký tự, ví dụ "Đặt ngay trên Chợ Z".
- accent: một màu hex lấy từ bao bì để làm nhãn giá và nút (ví dụ đỏ của logo), đủ tương phản với chữ trắng.
- background_prompt: tiếng Anh, MỘT căn phòng/mặt bàn trống hợp với món này, ánh sáng mềm, tông màu hợp bao bì,
  khoảng trống ở giữa để đặt sản phẩm; bắt buộc ghi "no product, no packaging, no text, no letters, no people".
- beats: 3-6 hiệu ứng trong {effects}, có start_s/end_s trong [0, {seconds}]; sản phẩm xuất hiện trước,
  giá nảy lên sau, nút kêu gọi ở cuối. Không dùng hiệu ứng nào khác.
Cấm: "nhất", "số một", "duy nhất", "100%", chứng nhận hay lời hứa sức khỏe không có trong listing.
KHÔNG viết giá: giá do hệ thống ghi từ listing."""


def price_text(listing: dict) -> str:
    price = listing.get("price_vnd")
    return f"{int(price):,}đ".replace(",", ".") if price else ""


def validate(plan: BannerPlan, seconds: float) -> list[str]:
    problems = []
    for field in ("headline", "subline", "cta"):
        text = getattr(plan, field).lower()
        problems += [f"{field}: banned word '{w}'" for w in BANNED if w in text]
    if re.search(r"\d{2,}\s*(k|đ|nghìn|ngàn|vnd)", f"{plan.headline} {plan.subline}".lower()):
        problems.append("a price was written by the model; prices come from the listing only")
    for beat in plan.beats:
        if beat.end_s <= beat.start_s or beat.end_s > seconds + 0.01:
            problems.append(f"{beat.effect}: bad timing {beat.start_s}-{beat.end_s}")
    if "no text" not in plan.background_prompt.lower():
        problems.append("background_prompt must forbid text")
    return problems


def _sample(inp: BannerInput) -> BannerPlan:
    title = str(inp.listing.get("title") or "Sản phẩm")
    return BannerPlan(headline=title[:28], subline=str(inp.listing.get("category") or "")[:44], cta="Đặt ngay trên Chợ Z",
                      accent="#c2410c", background_prompt="An empty wooden table in soft morning light, plain wall, "
                      "no product, no packaging, no text, no letters, no people.",
                      beats=[Beat(effect=e, start_s=round(a * inp.seconds, 2), end_s=round(b * inp.seconds, 2))
                             for e, a, b in (("product_rise", 0.0, 0.22), ("headline_slide", 0.1, 0.3),
                                             ("price_pop", 0.32, 0.45), ("shine", 0.5, 0.7), ("cta_pulse", 0.65, 1.0))])


# --------------------------------------------------------------------------------------------- rendering

def _font(size: int, bold: bool = True) -> ImageFont.ImageFont:
    for name in (("segoeuib.ttf", "seguisb.ttf") if bold else ("segoeui.ttf",)) + ("DejaVuSans-Bold.ttf", "Arial.ttf"):
        for folder in (Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu"), Path("/Library/Fonts")):
            if (folder / name).is_file():
                return ImageFont.truetype(str(folder / name), size)
    return ImageFont.load_default(size=size)


def _ease(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) ** 3


def _progress(plan: BannerPlan, effect: str, t: float) -> float | None:
    """0..1 through the effect's beat; 1 after it; None when the plan does not use it (shown at rest)."""
    beats = [b for b in plan.beats if b.effect == effect]
    if not beats:
        return None
    b = beats[0]
    return min(max((t - b.start_s) / max(b.end_s - b.start_s, 0.01), 0.0), 1.0)


def _wrap(draw, text: str, f, width: int) -> list[str]:
    words, lines, line = text.split(), [], ""
    for w in words:
        trial = f"{line} {w}".strip()
        if draw.textlength(trial, font=f) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = w
    return lines + ([line] if line else [])


def render_frame(plan: BannerPlan, price: str, background: Image.Image, product: Image.Image, t: float,
                 size: int) -> Image.Image:
    s = size / 1080
    im = background.copy()
    d = ImageDraw.Draw(im, "RGBA")
    # A soft paper band behind the text keeps it readable on any background.
    d.rectangle((0, 0, size, int(250 * s)), fill=(246, 241, 231, 215))
    # Product: rises into place; at rest it sits low and centred.
    rise = _progress(plan, "product_rise", t)
    k = 1.0 if rise is None else _ease(rise)
    if k > 0:
        pw, ph = product.size
        px, py = (size - pw) // 2, int(size - ph - 120 * s + (1 - k) * 160 * s)
        shadow = Image.new("RGBA", im.size, (0, 0, 0, 0))     # a soft contact shadow so the box sits on the scene
        sd = ImageDraw.Draw(shadow)
        sd.ellipse((px + pw * 0.08, py + ph - 26 * s, px + pw * 0.92, py + ph + 22 * s), fill=(0, 0, 0, int(95 * k)))
        im.paste(shadow.filter(ImageFilter.GaussianBlur(14 * s)), (0, 0), shadow.filter(ImageFilter.GaussianBlur(14 * s)))
        layer = product.copy()
        if k < 1:
            layer.putalpha(layer.getchannel("A").point(lambda a: int(a * k)))
        shine = _progress(plan, "shine", t)
        if shine is not None and 0 < shine < 1:      # a light band sweeps across the product only
            band = Image.new("L", product.size, 0)
            bd = ImageDraw.Draw(band)
            x = int(-pw * 0.4 + shine * pw * 1.8)
            bd.polygon([(x, 0), (x + int(70 * s), 0), (x - int(pw * 0.25) + int(70 * s), ph), (x - int(pw * 0.25), ph)],
                       fill=110)
            band = Image.composite(band, Image.new("L", product.size, 0), product.getchannel("A"))
            white = Image.new("RGBA", product.size, (255, 255, 255, 255))
            white.putalpha(band)
            layer = Image.alpha_composite(layer, white)
        im.paste(layer, (px, py), layer)
    # Headline slides in from the left; subline under it.
    slide = _progress(plan, "headline_slide", t)
    h = 1.0 if slide is None else _ease(slide)
    if h > 0:
        x = int(60 * s - (1 - h) * 500 * s)
        f1, f2 = _font(int(62 * s)), _font(int(36 * s), bold=False)
        y = int(46 * s)
        for line in _wrap(d, plan.headline, f1, int(960 * s))[:2]:
            d.text((x, y), line, font=f1, fill=(31, 26, 20, int(255 * h)))
            y += int(72 * s)
        if plan.subline:
            d.text((x, y + int(4 * s)), plan.subline, font=f2, fill=(90, 82, 72, int(255 * h)))
    # Price badge pops (overshoot) at the top right of the product.
    pop = _progress(plan, "price_pop", t)
    if price and (pop is None or pop > 0):
        z = 1.0 if pop is None else (_ease(pop) * 1.15 if pop < 0.7 else 1.15 - 0.15 * (pop - 0.7) / 0.3)
        f3 = _font(max(int(58 * s * z), 8))
        w = d.textlength(price, font=f3)
        cx, cy = int(size * 0.76), int(size * 0.40)
        pad = int(26 * s * z)
        d.rounded_rectangle((cx - w / 2 - pad, cy - 44 * s * z, cx + w / 2 + pad, cy + 44 * s * z), radius=int(22 * s),
                            fill=plan.accent)
        d.text((cx, cy), price, font=f3, fill="#ffffff", anchor="mm")
    # Call to action: a pill at the bottom that breathes.
    pulse = _progress(plan, "cta_pulse", t)
    if pulse is None or pulse > 0:
        z = 1.0 + (0.05 * math.sin(t * 2 * math.pi * 1.6) if pulse is not None else 0.0)
        f4 = _font(int(40 * s * z))
        w = d.textlength(plan.cta, font=f4)
        cx, cy = size // 2, int(size - 62 * s)
        d.rounded_rectangle((cx - w / 2 - 34 * s, cy - 36 * s * z, cx + w / 2 + 34 * s, cy + 36 * s * z),
                            radius=int(36 * s), fill=plan.accent)
        d.text((cx, cy), plan.cta, font=f4, fill="#ffffff", anchor="mm")
    return im


def render(plan: BannerPlan, price: str, background_png: bytes, packshot: Path, out_dir: Path, size: int,
           seconds: float, fps: int) -> tuple[Path, Path | None, int]:
    background = Image.open(io.BytesIO(background_png)).convert("RGB")
    side = min(background.size)
    background = background.crop(((background.width - side) // 2, (background.height - side) // 2,
                                  (background.width + side) // 2, (background.height + side) // 2)).resize((size, size))
    product = Image.open(packshot).convert("RGBA")
    bbox = product.getchannel("A").getbbox()
    product = product.crop(bbox) if bbox else product
    product.thumbnail((int(size * 0.62), int(size * 0.55)))
    frames = [render_frame(plan, price, background, product, i / fps, size) for i in range(int(seconds * fps))]
    gif = out_dir / "banner.gif"
    small = [f.resize((GIF_SIDE, GIF_SIDE), Image.Resampling.LANCZOS) if size > GIF_SIDE else f for f in frames]
    small[0].save(gif, save_all=True, append_images=small[1:], duration=int(1000 / fps), loop=0, optimize=True)
    mp4 = None
    from studio.render import ffmpeg_path
    ffmpeg = ffmpeg_path()
    if ffmpeg:
        mp4 = out_dir / "banner.mp4"
        tmp = out_dir / "_frames"
        tmp.mkdir(exist_ok=True)
        for i, fr in enumerate(frames):
            fr.save(tmp / f"{i:04d}.png")
        subprocess.run([ffmpeg, "-v", "error", "-y", "-framerate", str(fps), "-i", str(tmp / "%04d.png"),
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", str(mp4)], check=True)
        shutil.rmtree(tmp, ignore_errors=True)
    return gif, mp4, len(frames)


async def run(client, settings: Settings, inp: BannerInput) -> Banner:
    folder = work_dir(settings, inp.work_dir, "banner")
    price = price_text(inp.listing)
    async with log.step("banner", "Listing → banner plan (JSON schema)", ROLE,
                        f"listing + packshot, {inp.seconds:.0f} s", mock=client.mock) as step:
        if client.mock:
            plan = _sample(inp)
            step.model, step.endpoint = settings.text_model, "POST /v1/chat/completions"
        else:
            prompt = require_prompt("banner", PROMPT).format(seconds=inp.seconds, effects=", ".join(EFFECTS))
            photos = [Path(p).read_bytes() for p in inp.photos[:2]]
            raw, meta = await client.chat_json(prompt, f"Listing:\n{inp.listing}", photos, "BannerPlan", PLAN_SCHEMA)
            step.apply(meta)
            plan = BannerPlan.model_validate(raw)
        problems = validate(plan, inp.seconds)
        if problems:
            raise ValueError("banner plan rejected: " + "; ".join(problems))
        step.decision = f"'{plan.headline}' · {len(plan.beats)} beats · accent {plan.accent}"
        step.output["plan"] = plan.model_dump()
    async with log.step("banner-bg", "Paint the empty background", ROLE, plan.background_prompt[:80],
                        mock=client.mock) as step:
        background, meta = await client.generate_image(plan.background_prompt, "1024x1024")
        step.apply(meta)
        (folder / "background.png").write_bytes(background)
    async with log.step("banner-render", "Animate (local): real product pixels, price from the listing", ROLE,
                        f"{inp.size}px, {inp.seconds} s @ {inp.fps} fps", mock=False) as step:
        gif, mp4, n = render(plan, price, background, Path(inp.packshot_path), folder, inp.size, inp.seconds, inp.fps)
        step.model, step.endpoint = "local (Pillow + ffmpeg)", ""
        step.decision = f"{n} frames → {gif.name}" + (f", {mp4.name}" if mp4 else "")
    return Banner(plan=plan, price_text=price, background_path=str(folder / "background.png"), gif_path=str(gif),
                  mp4_path=str(mp4) if mp4 else None, frames=n, seconds=inp.seconds)
