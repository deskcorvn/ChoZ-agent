import io
import wave

import pytest
from PIL import Image, ImageDraw

from app.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(api_key="test-key", base_url="https://api.meta.ai/v1", mock=True,
                    text_model="muse-spark-1.3", image_model="muse-image-1.0", sam_model="sam-3.1",
                    asr_model="muse-voice-transcribe-1.0", language_bias="Vietnamese",
                    data_dir=tmp_path / "data", timeout_s=5)


def product_photo(size=(640, 480), color=(200, 30, 40)) -> bytes:
    """A red rectangle (the 'product') on a pale backdrop, like a quick phone shot."""
    image = Image.new("RGB", size, (232, 228, 220))
    ImageDraw.Draw(image).rectangle((220, 140, 420, 380), fill=color)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=95)
    return buffer.getvalue()


def wav_bytes(seconds=1.5, rate=16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buffer.getvalue()
