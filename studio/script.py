"""script: listing + brief → `Script` (4-8 voice-over beats, supers, CTA, sourced claims). Muse Spark, json_schema."""
from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings
from studio import log, require_prompt
from studio.models import SCRIPT_SCHEMA, Claim, Script, ScriptInput

INPUT, OUTPUT, ROLE = ScriptInput, Script, "writer"

PROMPT = """Bạn là biên kịch quảng cáo dọc 30 giây cho người bán nhỏ trên Chợ Z (Việt Nam).
Từ listing (JSON) và ảnh sản phẩm, viết Script (JSON theo schema):
- product: tên sản phẩm đúng như listing.
- beats: 4-8 ý lời dẫn, MỖI Ý TỐI ĐA 12 TỪ, một ý một câu, đọc liền mạch như một người kể; ý đầu là
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
