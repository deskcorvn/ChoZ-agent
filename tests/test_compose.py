"""The studio photo must show the seller's real product: its pixels come from the photo, never a model."""
import io

import numpy as np
import pytest
from PIL import Image

from app import compose
from tests.conftest import product_photo


def red_mask(photo):
    rgb = np.asarray(photo).astype(int)
    return (rgb[..., 0] > 150) & (rgb[..., 1] < 100)


def test_normalise_photo_shrinks_and_keeps_aspect():
    big = Image.new("RGB", (4000, 3000), (10, 20, 30))
    buffer = io.BytesIO()
    big.save(buffer, "JPEG")
    out = Image.open(io.BytesIO(compose.normalise_photo(buffer.getvalue())))
    assert out.size == (1280, 960) and out.format == "JPEG"


def test_cutout_is_cropped_to_the_mask_and_transparent_outside():
    photo = Image.open(io.BytesIO(product_photo())).convert("RGB")
    piece = compose.cutout(photo, red_mask(photo))
    assert piece.mode == "RGBA"
    assert abs(piece.width - 201) <= 2 and abs(piece.height - 241) <= 2
    alpha = np.asarray(piece.getchannel("A"))
    assert alpha[alpha.shape[0] // 2, alpha.shape[1] // 2] == 255


def test_studio_keeps_the_product_colour():
    photo = Image.open(io.BytesIO(product_photo(color=(200, 30, 40)))).convert("RGB")
    background = Image.new("RGB", (800, 600), (240, 240, 245))
    final = compose.studio(background, compose.cutout(photo, red_mask(photo)))
    assert final.size == (compose.STUDIO_SIDE, compose.STUDIO_SIDE)
    centre = np.asarray(final)[compose.STUDIO_SIDE // 2 - 20: compose.STUDIO_SIDE // 2 + 20,
                               compose.STUDIO_SIDE // 2 - 20: compose.STUDIO_SIDE // 2 + 20]
    assert np.abs(centre.reshape(-1, 3).mean(axis=0) - (200, 30, 40)).max() < 12


def test_cutout_refuses_a_mask_of_the_wrong_size_or_an_empty_one():
    photo = Image.new("RGB", (100, 80))
    with pytest.raises(ValueError, match="does not match"):
        compose.cutout(photo, np.ones((10, 10), bool))
    with pytest.raises(ValueError, match="empty"):
        compose.cutout(photo, np.zeros((80, 100), bool))


def test_contact_shadow_follows_a_slanted_bottom_edge():
    # A parallelogram whose bottom edge drops from left to right, as in a photo taken at an angle.
    size = (100, 80)
    alpha = np.zeros((80, 100), np.uint8)
    for x in range(100):
        alpha[: 40 + x // 4, x] = 255
    item = Image.new("RGBA", size, (10, 20, 30, 0))
    item.putalpha(Image.fromarray(alpha))
    shadow = np.asarray(compose.contact_shadow(item, pad=20))
    left_col, right_col = 20 + 5, 20 + 95
    assert shadow[:, left_col].argmax() < shadow[:, right_col].argmax()     # it slants with the edge
    assert shadow[20 + 5, left_col] < shadow.max() * 0.05                     # next to nothing at the top
