"""judge: the finished mp4 → `JudgeReport` (14 technical checks, 6 editorial criteria, timestamps, score). Muse Spark, file input."""
from __future__ import annotations

from pathlib import Path

from app.config import Settings
from studio import log, render, require_prompt
from studio.models import JUDGE_SCHEMA, SPOT_SECONDS, CheckItem, Criterion, JudgeInput, JudgeReport

INPUT, OUTPUT, ROLE = JudgeInput, JudgeReport, "judge"

QC_ITEMS = ("9:16 frame", "no black frames", "no frozen frames", "no extra hands", "no text artefacts",
            "product matches photo", "subtitles readable", "subtitles match voice", "voice intelligible",
            "loudness about -14 LUFS", "real packshot shown", "AI label present", "length 25-35 s", "no abrupt cut")
# Measured by code, not asked of the model: a live judge on 2 Nov 2026 failed "loudness" with "không thấy"
# on a spot measured at -14.3 LUFS.
MEASURED = ("loudness about -14 LUFS", "length 25-35 s")
CRITERIA = ("hook in first 3 s", "one clear message", "claims supported", "continuity", "pacing", "call to action")

PROMPT = f"""Bạn là giám khảo quảng cáo dọc (TikTok, Reels, Zalo Video) ở Việt Nam và kiểm định viên hậu kỳ.
Xem video đính kèm (hình và tiếng; lời dẫn tiếng Việt) rồi trả JSON theo schema JudgeReport:
- qc_items: đúng 14 mục sau, theo thứ tự, name giữ nguyên chữ: {list(QC_ITEMS)}. ok=true chỉ khi nhìn/nghe
  thấy; evidence ghi giây (mm:ss) và điều thấy; không thấy bằng chứng thì ok=false, evidence "không thấy".
- criteria: đúng 6 tiêu chí, name giữ nguyên: {list(CRITERIA)}; score 0-10 (5 = quảng cáo trung bình, 9-10 chỉ
  cho bản xuất sắc); at_s = các giây làm căn cứ; note một câu. "claims supported": đối chiếu lời dẫn với
  Script nếu có (claims và source); claim không có nguồn hoặc "nhất/đầu tiên/duy nhất" là lỗi nặng.
- total: trung bình 6 tiêu chí, trừ 1 điểm cho mỗi mục qc_items sai (không dưới 0).
- summary: một câu tiếng Việt cho người dựng: điểm mạnh nhất và việc cần sửa trước, kèm mốc giây.
Hai mục "loudness about -14 LUFS" và "length 25-35 s" do code đo: ghi ok=true, evidence "đo bằng code".
"real packshot shown" và "product matches photo": khung cuối là ẢNH CHỤP THẬT bao bì (dòng "Hình ảnh tạo bằng AI"
nói về các cảnh quay trước đó, không phải về khung cuối); so bao bì đó với ảnh sản phẩm trong Script/listing.
Chỉ chấm điều nhìn/nghe thấy trong video."""


def _sample(inp: JudgeInput) -> JudgeReport:
    return JudgeReport(video_path=inp.video_path,
                       qc_items=[CheckItem(name=n, ok=True, evidence="mock: not inspected") for n in QC_ITEMS],
                       criteria=[Criterion(name=n, score=7.0, note="mock") for n in CRITERIA],
                       total=7.0, summary="mock verdict: not inspected")


def measure(video_path: str) -> dict[str, CheckItem]:
    """Loudness and length from ffmpeg; {} when ffmpeg or the file is missing."""
    ffmpeg = render.ffmpeg_path()
    if not ffmpeg or not Path(video_path).is_file():
        return {}
    import re
    import subprocess

    from studio.edit import _measure
    lufs = _measure(ffmpeg, Path(video_path))["lufs"]
    err = subprocess.run([ffmpeg, "-hide_banner", "-i", video_path], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stderr
    h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err).groups()
    seconds = int(h) * 3600 + int(m) * 60 + float(s)
    return {MEASURED[0]: CheckItem(name=MEASURED[0], ok=abs(lufs + 14) <= 1.0, evidence=f"measured {lufs:.1f} LUFS"),
            MEASURED[1]: CheckItem(name=MEASURED[1], ok=SPOT_SECONDS - 5 <= seconds <= SPOT_SECONDS + 5,
                                   evidence=f"measured {seconds:.1f} s")}


def with_measurements(report: JudgeReport, measured: dict[str, CheckItem]) -> JudgeReport:
    """Replace the model's answer on measured items and recompute the total the prompt defines."""
    if not measured:
        return report
    items = [measured.get(i.name, i) for i in report.qc_items]
    mean = sum(c.score for c in report.criteria) / max(len(report.criteria), 1)
    total = max(0.0, mean - sum(1 for i in items if not i.ok))
    return report.model_copy(update={"qc_items": items, "total": round(total, 2)})


async def run(client, settings: Settings, inp: JudgeInput) -> JudgeReport:
    describe = f"video {Path(inp.video_path).name}, script {'given' if inp.script else 'none'}"
    async with log.step("judge", "Judge the finished spot", ROLE, describe, mock=client.mock) as step:
        if client.mock:
            report = _sample(inp)
            step.model, step.endpoint = settings.text_model, "POST /v1/files + /v1/responses"
        else:
            prompt = require_prompt("judge", PROMPT)
            if inp.script:
                prompt += f"\n\nScript:\n{inp.script.model_dump_json()}"
            raw, meta = await client.responses_file(inp.video_path, prompt, JUDGE_SCHEMA, "video/mp4")
            step.apply(meta)
            raw["video_path"] = inp.video_path
            report = with_measurements(JudgeReport.model_validate(raw), measure(inp.video_path))
        failed = [i.name for i in report.qc_items if not i.ok]
        step.decision = f"score {report.total:.1f}/10" + (f", failed {failed}" if failed else "")
        step.output["report"] = report.model_dump()
        return report
