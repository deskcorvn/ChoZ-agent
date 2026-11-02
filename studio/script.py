"""script: listing + brief → `Script` (4-8 voice-over beats, supers, CTA, sourced claims). Muse Spark, json_schema."""
from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings
from app.kb import fold
from studio import log, require_prompt
from studio.models import MIN_SPOT_WORDS, SCRIPT_SCHEMA, Claim, Script, ScriptInput

INPUT, OUTPUT, ROLE = ScriptInput, Script, "writer"

PROMPT = """Bạn là biên kịch quảng cáo dọc 30 giây cho người bán nhỏ trên Chợ Z (Việt Nam).
Từ listing (JSON) và ảnh sản phẩm, viết Script (JSON theo schema):
- product: tên sản phẩm đúng như listing.
- beats: 6-8 ý lời dẫn, MỖI Ý 8-12 TỪ, TỔNG 70-90 TỪ (đọc vừa đủ 27 giây), một ý một câu, đọc liền mạch như một người kể; ý đầu là
  câu hỏi hoặc tình huống đời thường (hook), ý cuối trước CTA là giá đúng như listing (viết số thành chữ:
  "hai mươi hai nghìn"). Không viết tên thương hiệu nước ngoài, không viết "Z" (đọc là "Dét"); viết
  "Chợ Dét" trong beats để giọng đọc đúng, còn supers/cta viết "Chợ Z".
- supers: 2-4 dòng chữ trên hình, ngắn (≤ 6 từ), gồm giá và "Đặt ngay trên Chợ Z".
- cta: một câu kêu gọi, kết bằng "trên Chợ Z".
- claims: MỌI khẳng định về sản phẩm (xuất xứ, cách dùng, thành phần, khối lượng, giá) phải có
  source là "listing" (có trong listing) hoặc "label" (đọc được trên ảnh bao bì, ghi evidence là dòng chữ
  đọc được) hoặc "seller" (trong allowed_claims). Không có nguồn thì KHÔNG đưa vào beats.
- Cấm: "nhất", "số một", "đầu tiên", "duy nhất", "100%", số gam/khối lượng không có trong listing hoặc
  nhãn, chứng nhận (OCOP, ISO) không có nguồn, so sánh với đối thủ, lời hứa sức khỏe.
- language: "vi". Giọng thân tình, miền Bắc, không hô hào."""


# How to prepare or use a product is a claim like any other: a live script on 2 Nov 2026 said "chỉ cần thêm nước
# sôi" and "mở nắp chờ vài phút" with no source. Words that state preparation, checked against the listing and the
# script's own sourced claims (folded, so "nuoc soi" counts too).
PREPARATION = ("nuoc soi", "do nuoc", "chan nuoc", "phut", "ngam", "dun ", "lo vi song", "mo nap", "day nap", "pha ")


def unsupported_beats(script: Script, listing: dict) -> list[str]:
    """Beats that state a preparation step found neither in the listing nor in a sourced claim."""
    known = fold(" ".join([str(listing.get("description") or ""), str(listing.get("title") or "")] +
                          [f"{c.text} {c.evidence}" for c in script.claims]))
    found = []
    for beat in script.beats:
        text = fold(beat) + " "
        if any(word in text and word not in known + " " for word in PREPARATION):
            found.append(beat)
    return found


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
            words = sum(len(b.split()) for b in script.beats)
            if words < MIN_SPOT_WORDS:          # too short for a 30 s spot (a live run read 27 words in 8.8 s)
                again = (f"{user}\n\nBản trước chỉ có {words} từ lời dẫn, đọc chưa tới 10 giây. Viết lại: 6-8 ý, "
                         f"mỗi ý 8-12 từ, tổng 70-90 từ, giữ nguyên các claim có nguồn.")
                raw, meta = await client.chat_json(prompt, again, photos, "Script", SCRIPT_SCHEMA)
                step.apply(meta)
                script = Script.model_validate(raw)
                step.output["first_try_words"] = words
            unsupported = unsupported_beats(script, inp.listing)
            if unsupported:                     # one rewrite; what is still unsourced is reported, not hidden
                again = (f"{user}" + "\n" + "\n" + "Các ý sau nói cách pha/dùng mà listing và claims không có nguồn: "
                         f"{unsupported}. Viết lại, bỏ các ý đó hoặc thêm claim có nguồn (label/seller), giữ 6-8 ý, 70-90 từ.")
                raw, meta = await client.chat_json(prompt, again, photos, "Script", SCRIPT_SCHEMA)
                step.apply(meta)
                script = Script.model_validate(raw)
                step.output["unsupported_first_try"] = unsupported
                step.output["unsupported"] = unsupported_beats(script, inp.listing)
        step.decision = (f"{len(script.beats)} beats, {sum(len(b.split()) for b in script.beats)} words, "
                         f"{len(script.claims)} claims")
        step.output["script"] = script.model_dump()
        return script
