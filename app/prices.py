"""Meta Model API list prices, Standard mode, copied from dev.meta.ai/docs/pricing-rate-limits on 31/10/2026.

Contributor mode is cheaper but lets Meta train on the request, so it is not used for sellers' data.
"""
from __future__ import annotations

# USD per 1M tokens.
TEXT = {
    "muse-spark-1.3": {"input": 1.25, "cached_input": 0.15, "output": 4.25},
    "muse-spark-1.2": {"input": 1.25, "cached_input": 0.15, "output": 4.25},
    "muse-wonder-1.0": {"input": 4.00, "cached_input": 0.20, "output": 20.00},
}
IMAGE_PER_IMAGE = 0.01        # muse-image-1.0
SAM_PER_IMAGE = 2.50 / 1000   # sam-3.1, image segmentation
ASR_PER_HOUR = 0.18           # muse-voice-transcribe-1.0, audio processed


def text_cost(model: str, usage: dict | None) -> float:
    """Cost of one Chat Completions call from its `usage` block. Unknown model → 0, never a guess."""
    rates = TEXT.get(model)
    if not rates or not usage:
        return 0.0
    prompt = int(usage.get("prompt_tokens") or 0)
    cached = int(((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    output = int(usage.get("completion_tokens") or 0)
    return ((prompt - cached) * rates["input"] + cached * rates["cached_input"]
            + output * rates["output"]) / 1_000_000


def asr_cost(seconds: float) -> float:
    return max(seconds, 0.0) / 3600 * ASR_PER_HOUR
