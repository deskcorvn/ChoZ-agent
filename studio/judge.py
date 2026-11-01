"""judge: the finished mp4 → `JudgeReport` (14 technical checks, 6 editorial criteria, timestamps, score). Muse Spark, file input."""
from __future__ import annotations

from pathlib import Path

from app.config import Settings
from studio import log, require_prompt
from studio.models import JUDGE_SCHEMA, CheckItem, Criterion, JudgeInput, JudgeReport

INPUT, OUTPUT, ROLE = JudgeInput, JudgeReport, "judge"

QC_ITEMS = ("9:16 frame", "no black frames", "no frozen frames", "no extra hands", "no text artefacts",
            "product matches photo", "subtitles readable", "subtitles match voice", "voice intelligible",
            "loudness about -14 LUFS", "real packshot shown", "AI label present", "length 25-35 s", "no abrupt cut")
CRITERIA = ("hook in first 3 s", "one clear message", "claims supported", "continuity", "pacing", "call to action")

# TODO(prompt): the judge's prompt: the 14 items as pass/fail with the second seen, the 6 criteria 0-10
# with timestamps, a one-line summary. The script, when given, is the reference for the claims.
PROMPT = ""


def _sample(inp: JudgeInput) -> JudgeReport:
    return JudgeReport(video_path=inp.video_path,
                       qc_items=[CheckItem(name=n, ok=True, evidence="mock: not inspected") for n in QC_ITEMS],
                       criteria=[Criterion(name=n, score=7.0, note="mock") for n in CRITERIA],
                       total=7.0, summary="mock verdict: not inspected")


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
            report = JudgeReport.model_validate(raw)
        failed = [i.name for i in report.qc_items if not i.ok]
        step.decision = f"score {report.total:.1f}/10" + (f", failed {failed}" if failed else "")
        step.output["report"] = report.model_dump()
        return report
