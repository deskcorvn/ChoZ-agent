"""script: listing + brief → `Script` (4-8 voice-over beats, supers, CTA, sourced claims). Muse Spark, json_schema."""
from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings
from studio import log, require_prompt
from studio.models import SCRIPT_SCHEMA, Claim, Script, ScriptInput

INPUT, OUTPUT, ROLE = ScriptInput, Script, "writer"

# TODO(prompt): the writer's system prompt. Rules it must carry: one idea per beat, at most 12 words a
# beat, claims only from the listing, the label or the seller's words (with the source named), no
# superlatives the listing does not support, Vietnamese, CTA last.
PROMPT = ""


def _sample(inp: ScriptInput) -> Script:
    """Mock answer: beats cut from the listing's own words, so the chain runs on any product."""
    listing = inp.listing
    product = str(listing.get("title") or "Sản phẩm")
    description = str(listing.get("description") or "")
    sentences = [s.strip() for s in re.split(r"[.!?]\s*", description) if s.strip()]
    beats = [product] + [" ".join(s.split()[:12]) for s in sentences[:3]]
    price = listing.get("price_vnd")
    if price:
        beats.append(f"Giá {int(price):,} đồng".replace(",", "."))
    while len(beats) < 4:
        beats.append("Hàng có sẵn, giao nhanh")
    claims = [Claim(text=product, source="listing", evidence="title")]
    for attribute in listing.get("attributes") or []:
        claims.append(Claim(text=f"{attribute.get('name')}: {attribute.get('value')}", source="listing",
                            evidence="attributes"))
    claims += [Claim(text=c, source="seller") for c in inp.allowed_claims]
    return Script(product=product, beats=beats[:8], supers=[product, "Đặt ngay trên Chợ Z"],
                  cta="Đặt ngay trên Chợ Z", claims=claims)


async def run(client, settings: Settings, inp: ScriptInput) -> Script:
    describe = f"listing {len(inp.listing)} fields, {len(inp.photos)} photo(s), brief {len(inp.brief)} chars"
    async with log.step("script", "Listing → script (JSON schema)", ROLE, describe, mock=client.mock) as step:
        if client.mock:
            script = _sample(inp)
            step.model, step.endpoint = settings.text_model, "POST /v1/chat/completions"
        else:
            prompt = require_prompt("script", PROMPT)
            photos = [Path(p).read_bytes() for p in inp.photos]
            user = f"Listing:\n{inp.listing}\n\nBrief: {inp.brief}\nAllowed claims: {inp.allowed_claims}"
            raw, meta = await client.chat_json(prompt, user, photos, "Script", SCRIPT_SCHEMA)
            step.apply(meta)
            script = Script.model_validate(raw)
        step.decision = f"{len(script.beats)} beats, {len(script.claims)} claims"
        step.output["script"] = script.model_dump()
        return script
