"""shotlist: `Script` + continuity bible → `ShotList` (5-8 s shots, one action each, locked camera). Muse Spark."""
from __future__ import annotations

import re

from app.config import Settings
from app.kb import fold
from studio import log, require_prompt
from studio.models import SHOTLIST_SCHEMA, Bible, Shot, ShotList, ShotListInput

INPUT, OUTPUT, ROLE = ShotListInput, ShotList, "director"

PROMPT = """Bạn là đạo diễn hình cho quảng cáo dọc 9:16 làm bằng AI (ảnh tĩnh trước, image-to-video sau).
Từ Script và Bible (nhân vật, địa điểm, đạo cụ, giờ trong ngày) viết ShotList (JSON theo schema).
Bible rỗng thì tự đặt: một người Việt Nam ~30 tuổi, trang phục giản dị, một bếp hoặc bàn ăn căn hộ Việt
Nam (gạch men, bát hoa lam Bát Tràng, ghế nhựa); ghi bible đã chọn vào trường bible. Giờ trong ngày
(time_of_day) GIỮ ĐÚNG như Bible, vì nó lấy từ lời dẫn: lời dẫn nói "tối muộn" thì mọi cảnh là buổi tối,
đèn trong nhà, ngoài cửa sổ trời tối; không cảnh nào được ghi "morning".

Quy tắc cảnh (học từ 41 clip Kling đã kiểm 1/11/2026):
- Một cảnh cho mỗi beat (beat_index), 5-8 giây, MỘT hành động duy nhất; không "rồi", "then", không
  hành động thứ hai (mô hình sẽ tự cắt sang góc máy khác và đổi mặt nhân vật).
- camera = "locked" hoặc một chuyển động rất chậm duy nhất ("slow push-in", "slow pull-back").
- light theo giờ trong Bible, viết cụ thể (nguồn sáng, hướng, bóng).
- frame_prompt (tiếng Anh, cho Muse Image /images/edits với ảnh tham chiếu): mở đầu bằng vai từng ảnh
  tham chiếu ("Reference image 1 is the character: keep her face, hair and earrings exactly; ignore its
  background. Reference image 2 is the location: keep its layout."), rồi tả khung đầu như một ảnh chụp
  9:16: bố cục, tư thế bắt đầu hành động, tay cầm sẵn vật với năm ngón rõ, ánh sáng, bối cảnh Việt Nam
  ("Setting: Vietnam, 2020s; not Japanese, Korean, Chinese or Thai interiors or tableware"), đồ vật trơn
  không nhãn ("plain unmarked kettle"), và kết bằng "Photorealistic, full-frame camera, natural colour.
  No packaging, no logos, no text, no letters, no watermark, no other people." Cảnh chỉ thấy tay: ghi
  "Exactly two hands in frame and no more" và KHÔNG đưa vai character vào ref_roles.
- motion_prompt (tiếng Anh, cho image-to-video từ khung đầu): "Start from the first frame. One continuous
  shot, camera locked (hoặc một chuyển động chậm), no cuts, framing unchanged." + hành động duy nhất với
  ≥ 3 dấu hiệu cụ thể (mắt, lông mày, miệng, vai / tay-điểm tiếp xúc) + "Motion settles as ..." + ánh
  sáng + "No subtitles, no text, no logos, no second person, no outfit change." Không mô tả lại ngoại hình
  nhân vật (đã có trong khung đầu).
- ref_roles: tập con của ["character", "location", "prop", "product"]; "product" chỉ cho cảnh cần thấy
  sản phẩm thật (bao bì sẽ được GHÉP khi dựng, không để AI vẽ; đừng tả bao bì trong prompt).
- Đồ ăn/uống: cảnh ăn, múc, nếm PHẢI thấy món đã nấu trong bát/cốc, tả cụ thể bằng tiếng Anh theo tên
  sản phẩm (cháo hàu → "a white bowl of hot rice porridge with oysters, chopped spring onion and fried
  shallots, steam rising"); không bát rỗng, không thìa rỗng. Ghi món đó vào props của bible.
- Không chữ, không logo, không bao bì trong bất kỳ prompt nào; không người thứ hai."""

_LIGHT = {"morning": "soft window light from the left", "noon": "bright overhead daylight",
          "evening": "warm low sun from the right", "night": "warm ceiling lamp indoors, dark window behind"}

# The voice-over and the pictures must tell the same time of day (a live run on 2 Nov 2026 said "tối muộn"
# over morning light because the bible defaulted to morning). Folded words, so "toi muon" counts too.
_TIMES = (("night", r"\b(toi|dem|khuya|bua toi)\b"), ("noon", r"\b(trua|bua trua)\b"),
          ("morning", r"\b(buoi sang|bua sang|sang nay|sang som)\b"))


def time_from_script(beats: list[str]) -> str:
    """The time of day the voice-over names first ("" when it names none)."""
    text = fold(" ".join(beats))
    hits = [(m.start(), name) for name, pattern in _TIMES for m in [re.search(pattern, text)] if m]
    return min(hits)[1] if hits else ""


def _sample(inp: ShotListInput) -> ShotList:
    bible = inp.bible if inp.bible.character else Bible(
        character="a woman in her thirties, plain white shirt", location="a tidy Vietnamese kitchen table",
        props=["a white bowl", "a wooden spoon"], time_of_day=inp.bible.time_of_day or "morning")
    light = _LIGHT.get(bible.time_of_day or "morning", _LIGHT["morning"])
    shots = []
    for i, beat in enumerate(inp.script.beats[:6]):
        shots.append(Shot(
            id=f"s{i + 1:02d}", beat_index=i, seconds=5.0,
            action=f"beat {i + 1}: {beat}", camera="locked", light=light,
            frame_prompt=f"{bible.location}, {bible.character}, {', '.join(bible.props)}, {light}, "
                         "9:16, no text, no logos",
            motion_prompt="hands move slowly, steam rises, camera locked",
            ref_roles=["character", "location"] + (["product"] if i in (0, len(inp.script.beats) - 1) else [])))
    return ShotList(shots=shots, bible=bible)


async def run(client, settings: Settings, inp: ShotListInput) -> ShotList:
    if not inp.bible.time_of_day:
        inp = inp.model_copy(update={"bible": inp.bible.model_copy(
            update={"time_of_day": time_from_script(inp.script.beats) or "morning"})})
    describe = f"{len(inp.script.beats)} beats; bible {'given' if inp.bible.character else 'default'}"
    async with log.step("shotlist", "Script → shot list (JSON schema)", ROLE, describe, mock=client.mock) as step:
        if client.mock:
            shotlist = _sample(inp)
            step.model, step.endpoint = settings.text_model, "POST /v1/chat/completions"
        else:
            prompt = require_prompt("shotlist", PROMPT)
            user = f"Script:\n{inp.script.model_dump_json()}\n\nBible:\n{inp.bible.model_dump_json()}"
            raw, meta = await client.chat_json(prompt, user, [], "ShotList", SHOTLIST_SCHEMA)
            step.apply(meta)
            shotlist = ShotList.model_validate(raw)
        if shotlist.bible.time_of_day != inp.bible.time_of_day:   # the script's time wins over the model's
            shotlist = shotlist.model_copy(update={"bible": shotlist.bible.model_copy(
                update={"time_of_day": inp.bible.time_of_day})})
        step.decision = f"{len(shotlist.shots)} shots, {shotlist.seconds:.0f} s, {inp.bible.time_of_day}"
        step.output["shotlist"] = shotlist.model_dump()
        return shotlist
