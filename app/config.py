"""Settings, read from the environment and from `.env.local` if it exists."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    """Minimal .env reader. A variable already set in the environment wins."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    mock: bool
    text_model: str
    image_model: str
    sam_model: str
    asr_model: str
    language_bias: str  # "" to let Voice Transcribe detect the language
    data_dir: Path
    timeout_s: float
    # Spark cannot switch reasoning off. Extraction and checking do not need depth. Measured 31/10/2026 on
    # the same two products: default effort 1,400-2,000 reasoning tokens, 34.7 s and $0.037 per listing;
    # "low" 26.4 s, $0.027; "minimal" ~200 tokens, 19.6 s, $0.023, with the same fields and verdicts.
    reasoning_effort: str = "minimal"


def load() -> Settings:
    _load_env_file(ROOT / ".env.local")
    key = os.environ.get("MODEL_API_KEY", "").strip()
    mode = os.environ.get("LISTING_MODE", "auto").strip().lower()
    if mode not in ("auto", "live", "mock"):
        raise RuntimeError(f"LISTING_MODE must be auto, live or mock, not {mode!r}")
    if mode == "live" and not key:
        raise RuntimeError("LISTING_MODE=live needs MODEL_API_KEY")
    return Settings(
        api_key=key,
        base_url=os.environ.get("MODEL_API_BASE_URL", "https://api.meta.ai/v1").rstrip("/"),
        # Mock is never silent: every step it answers is labelled "mock" in the agent log.
        mock=mode == "mock" or (mode == "auto" and not key),
        text_model=os.environ.get("LISTING_TEXT_MODEL", "muse-spark-1.3"),
        image_model=os.environ.get("LISTING_IMAGE_MODEL", "muse-image-1.0"),
        sam_model=os.environ.get("LISTING_SAM_MODEL", "sam-3.1"),
        asr_model=os.environ.get("LISTING_ASR_MODEL", "muse-voice-transcribe-1.0"),
        language_bias=os.environ.get("LISTING_LANGUAGE_BIAS", "Vietnamese").strip(),
        data_dir=Path(os.environ.get("LISTING_DATA_DIR", str(ROOT / "data"))),
        timeout_s=float(os.environ.get("LISTING_TIMEOUT_S", "90")),
        reasoning_effort=os.environ.get("LISTING_REASONING_EFFORT", "minimal").strip(),
    )
