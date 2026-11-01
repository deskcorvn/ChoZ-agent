"""frames: one `Shot` + refs → 3 painted 9:16 first frames (Muse Image edits) → Spark vision picks 1 (`FramePick`).

A bad first frame (three hands, text on the kettle) is rejected here, before any render credit is spent.
`--second-opinion` asks the external reviewer plug-in as well; it is a plug-in, not the main path.
"""
from __future__ import annotations

from pathlib import Path

from app.config import Settings
from studio import log, require_prompt, work_dir
from studio.models import FRAME_PICK_SCHEMA, FRAME_SIZE, FramePick, FramesInput

INPUT, OUTPUT, ROLE = FramesInput, FramePick, "cinematographer"

PICK_PROMPT = """Bạn là kiểm định viên tiền kỳ. Nhận mô tả cảnh (Shot) và N ảnh khung đầu ứng viên theo thứ tự.
Loại trước, xếp hạng sau. LOẠI ngay (ghi vào rejected với lý do nhìn thấy) khi: số bàn tay khác yêu cầu
(đếm từng bàn tay; cảnh chỉ tay phải đúng hai tay), ngón tay thừa/thiếu/biến dạng, có chữ/logo/nhãn trên
đồ vật hoặc nền, có bao bì sản phẩm do AI vẽ, có người thứ hai, mặt/tóc/trang phục khác ảnh nhân vật tham
chiếu, đồ vật nhân đôi (hai ấm), hành động đã xảy ra xong (nước đã đầy khi cảnh là rót).
Xếp hạng phần còn lại theo: đúng bố cục và tư thế bắt đầu của frame_prompt, ánh sáng đúng light, bối cảnh
Việt Nam, góc trống dành cho ghép bao bì nếu prompt yêu cầu, độ nét.
Trả JSON theo schema FramePick: picked = chỉ số 0-based của ứng viên tốt nhất CÒN LẠI; nếu tất cả bị loại,
picked = 0 và ghi rõ trong reasons rằng không bản nào dùng được, cùng câu cần đổi trong prompt.
reasons: 2-4 dòng ngắn, mỗi dòng một bằng chứng cụ thể (vị trí trong khung). Chỉ nhận xét điều nhìn thấy."""


async def second_opinion(candidates: list[Path], shot_prompt: str) -> str | None:
    """Reviewer plug-in (outside Meta). Not wired yet; returns None so the main path is unaffected."""
    return None  # TODO(live): optional external reviewer behind --second-opinion


async def run(client, settings: Settings, inp: FramesInput) -> FramePick:
    folder = work_dir(settings, inp.work_dir, "frames", inp.shot.id)
    refs = [Path(r.path).read_bytes() for r in inp.refs.images if r.role in inp.shot.ref_roles and Path(r.path).is_file()]
    describe = f"shot {inp.shot.id}, {len(refs)} ref(s), {inp.candidates} candidates"
    async with log.step("frames", f"First frames for {inp.shot.id} → pick one", ROLE, describe, mock=client.mock) as step:
        candidates: list[Path] = []
        for i in range(inp.candidates):
            image, meta = await client.images_edit(inp.shot.frame_prompt, refs, size=FRAME_SIZE)
            step.apply(meta)
            path = folder / f"frame-{i + 1}.png"
            path.write_bytes(image)
            candidates.append(path)
        if client.mock:
            pick = FramePick(shot_id=inp.shot.id, candidates=[str(c) for c in candidates], picked=0,
                             reasons=["mock: first candidate"],
                             rejected={str(c): "mock: not inspected" for c in candidates[1:]})
            step.model, step.endpoint = settings.text_model, "POST /v1/images/edits + /v1/chat/completions"
        else:
            prompt = require_prompt("frames", PICK_PROMPT)
            user = f"Shot: {inp.shot.model_dump_json()}\nCandidates are images 1..{len(candidates)} in order."
            raw, meta = await client.chat_json(prompt, user, [c.read_bytes() for c in candidates],
                                               "FramePick", FRAME_PICK_SCHEMA)
            step.apply(meta)
            raw["candidates"] = [str(c) for c in candidates]         # the model may echo descriptions; paths are ours
            raw["shot_id"] = inp.shot.id
            raw["picked"] = min(max(int(raw.get("picked", 0)), 0), len(candidates) - 1)
            raw["rejected"] = {str(candidates[int(k) - 1]) if str(k).isdigit() and 0 < int(k) <= len(candidates) else str(k): v
                               for k, v in (raw.get("rejected") or {}).items()}
            pick = FramePick.model_validate(raw)
        if inp.second_opinion:
            pick.second_opinion = await second_opinion(candidates, inp.shot.frame_prompt)
        step.decision = f"picked {pick.picked + 1}/{len(candidates)}, rejected {len(pick.rejected)}"
        step.output["pick"] = pick.model_dump()
        return pick
