"""List prices used to turn usage into cost. Meta prices are the repo's own (app/prices.py); Claude's are below.

Claude: https://platform.claude.com/docs/en/about-claude/pricing, read 03/11/2026, USD per million tokens.
"""
from __future__ import annotations

CLAUDE_PRICE_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing (xem 03/11/2026)"
META_PRICE_SOURCE = "https://dev.meta.ai/docs/pricing-rate-limits (xem 31/10/2026, app/prices.py)"
USD_VND_DEFAULT = 26_300
USD_VND_SOURCE = "giá trị trong yêu cầu demo (03/11/2026); đổi bằng biến USD_VND"

# input, 5-minute cache write, cache read, output
CLAUDE = {
    "claude-sonnet-5-5": {"input": 2.00, "cache_write": 2.50, "cache_read": 0.10, "output": 10.00},
    "claude-opus-5-5": {"input": 4.00, "cache_write": 5.00, "cache_read": 0.20, "output": 20.00},
    "claude-haiku-5-5": {"input": 0.10, "cache_write": 0.125, "cache_read": 0.01, "output": 0.50},
}


def claude_cost(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    """Cost of one Messages call. `input_tokens` is the uncached input, as the API reports it. Unknown model → error."""
    rates = CLAUDE[model]
    return (input_tokens * rates["input"] + cache_write * rates["cache_write"] + cache_read * rates["cache_read"]
            + output_tokens * rates["output"]) / 1_000_000
