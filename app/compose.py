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
PRODUCT_FILL = 0.72      # product fits inside this share of the studio frame
FLOOR_LINE = 0.84        # product's bottom edge sits at this height


def normalise_photo(raw: bytes) -> bytes:
    """Upright (EXIF), RGB, longest side ≤ 1280 px, JPEG. Fewer tokens, same content."""
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
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

    # A soft contact shadow: the product's own silhouette, squashed onto the floor and blurred.
    shadow_h = max(4, item.height // 12)
    silhouette = item.getchannel("A").resize((item.width, shadow_h))
    shadow = Image.new("L", (item.width + 80, shadow_h + 80), 0)
    shadow.paste(silhouette.point(lambda v: v * 0.45), (40, 40))
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    dark = Image.new("RGB", shadow.size, (40, 40, 40))
    canvas.paste(dark, (left - 40, top + item.height - shadow_h // 2 - 40), shadow)

    canvas.paste(item, (left, top), item)
    return canvas


def jpeg(image: Image.Image, quality: int = 90) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()
