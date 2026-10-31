"""Studio photo = the seller's real product, cut out by SAM 3.1, placed on a background from Muse Image.

The product's pixels always come from the seller's photo (scaled, never redrawn), so the listing
cannot show a colour, logo or shape the real item does not have. Muse Image only paints the room.
"""
from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageFilter, ImageOps

MAX_UPLOAD_SIDE = 1280
STUDIO_SIDE = 1024
PRODUCT_FILL = 0.66      # product fits inside this share of the studio frame
FLOOR_LINE = 0.86        # product's bottom edge sits at this height


def normalise_photo(raw: bytes) -> bytes:
    """Upright (EXIF), RGB, longest side ≤ 1280 px, JPEG. Fewer tokens, same content."""
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
    if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info):
        # Cut-out product PNGs: transparent pixels hold arbitrary colour (often black). Put them on white.
        rgba = image.convert("RGBA")
        image = Image.new("RGB", rgba.size, (255, 255, 255))
        image.paste(rgba, mask=rgba.getchannel("A"))
    image = image.convert("RGB")
    image.thumbnail((MAX_UPLOAD_SIDE, MAX_UPLOAD_SIDE), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=88)
    return buffer.getvalue()


def cutout(photo: Image.Image, mask: np.ndarray) -> Image.Image:
    """RGBA crop of the masked product. The alpha edge is softened by one pixel; colour is untouched."""
    if mask.shape != (photo.height, photo.width):
        raise ValueError(f"mask {mask.shape} does not match photo {(photo.height, photo.width)}")
    if not mask.any():
        raise ValueError("empty mask")
    alpha = Image.fromarray(mask.astype(np.uint8) * 255).filter(ImageFilter.GaussianBlur(1))
    rgba = photo.convert("RGB").copy()
    rgba.putalpha(alpha)
    rows, cols = np.where(mask)
    return rgba.crop((int(cols.min()), int(rows.min()), int(cols.max()) + 1, int(rows.max()) + 1))


def _square(background: Image.Image, side: int) -> Image.Image:
    return ImageOps.fit(background.convert("RGB"), (side, side), Image.Resampling.LANCZOS)


def studio(background: Image.Image, product: Image.Image, side: int = STUDIO_SIDE) -> Image.Image:
    canvas = _square(background, side)
    scale = min(side * PRODUCT_FILL / product.width, side * PRODUCT_FILL / product.height)
    size = (max(1, round(product.width * scale)), max(1, round(product.height * scale)))
    item = product.resize(size, Image.Resampling.LANCZOS)
    left = (side - item.width) // 2
    top = round(side * FLOOR_LINE) - item.height

    shadow = contact_shadow(item)
    pad = (shadow.width - item.width) // 2
    canvas.paste(Image.new("RGB", shadow.size, (30, 28, 26)), (left - pad, top - pad), shadow)
    canvas.paste(item, (left, top), item)
    return canvas


def contact_shadow(item: Image.Image, pad: int = 48) -> Image.Image:
    """An alpha mask, `pad` px larger than `item` on each side, that darkens just under the product's
    lowest edge, column by column. Phone photos are taken at an angle, so that edge is often a slant;
    a flat shadow under a slanted edge makes the product look like it floats."""
    solid = np.asarray(item.getchannel("A")) > 128
    h, w = solid.shape
    columns = solid.any(axis=0)
    lowest = h - 1 - np.argmax(solid[::-1], axis=0)                 # lowest opaque row per column
    rows = np.arange(h + 2 * pad)[:, None]
    edge = (lowest + pad)[None, :]
    band = h // 40 + 3
    near = ((rows >= edge - 2) & (rows <= edge + band) & columns[None, :]).astype(np.uint8) * 255
    mask = np.zeros((h + 2 * pad, w + 2 * pad), np.uint8)
    mask[:, pad:pad + w] = near
    tight = Image.fromarray(mask).filter(ImageFilter.GaussianBlur(4)).point(lambda v: round(v * 0.7))
    wide = Image.fromarray(mask).filter(ImageFilter.GaussianBlur(22)).point(lambda v: round(v * 0.45))
    return Image.fromarray(np.maximum(np.asarray(tight), np.asarray(wide)))


def jpeg(image: Image.Image, quality: int = 90) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()
