"""qc: a rendered clip + acceptance criteria + refs → `QcVerdict` (per-criterion evidence, beats, usable window).

Muse Spark with the clip as a file input (Files API). The verdict's `director_note` is what the next
frames/render round changes in the prompt; at most 2 rounds per shot.
"""
from __future__ import annotations

from pathlib import Path

from app.config import Settings
from studio import log, require_prompt
from studio.models import QC_SCHEMA, Beat, CheckItem, QcInput, QcVerdict

INPUT, OUTPUT, ROLE = QcInput, QcVerdict, "inspector"

# TODO(prompt): the inspector's prompt. For each acceptance criterion: ok or not, with the second it
# was seen. Beats: the seconds where something happens (hand reaches, steam, lid opens). Usable window:
# the longest stretch with no glitch. Verdict accept / retry (with a director note) / reject.
PROMPT = ""


async def second_opinion(clip_path: Path, accept: list[str]) -> str | None:
    """Reviewer plug-in (outside Meta), optional. Not wired yet."""
    return None  # TODO(live): optional external reviewer behind --second-opinion


def _sample(inp: QcInput) -> QcVerdict:
    seconds = inp.shot.seconds
    return QcVerdict(
        shot_id=inp.shot.id, clip_path=inp.clip_path,
        checks=[CheckItem(name=name, ok=True, evidence="mock: not inspected") for name in inp.accept],
        beats=[Beat(at_s=0.0, what="start"), Beat(at_s=round(seconds / 2, 1), what="mid action")],
        usable_start_s=0.0, usable_end_s=seconds, verdict="accept", director_note="")


async def run(client, settings: Settings, inp: QcInput) -> QcVerdict:
    describe = f"shot {inp.shot.id}, clip {Path(inp.clip_path).name}, {len(inp.accept)} criteria"
    async with log.step("qc", f"Check clip {inp.shot.id} against the shot", ROLE, describe, mock=client.mock) as step:
        if client.mock:
            verdict = _sample(inp)
            step.model, step.endpoint = settings.text_model, "POST /v1/files + /v1/responses"
        else:
            prompt = require_prompt("qc", PROMPT) + f"\n\nShot: {inp.shot.model_dump_json()}\nCriteria: {inp.accept}"
            raw, meta = await client.responses_file(inp.clip_path, prompt, QC_SCHEMA, "video/mp4")
            step.apply(meta)
            raw.update({"shot_id": inp.shot.id, "clip_path": inp.clip_path})
            verdict = QcVerdict.model_validate(raw)
        if inp.second_opinion:
            verdict.second_opinion = await second_opinion(Path(inp.clip_path), inp.accept)
        failed = [c.name for c in verdict.checks if not c.ok]
        step.decision = f"{verdict.verdict}, usable {verdict.usable_seconds:.1f} s" + (f", failed {failed}" if failed else "")
        step.output["verdict"] = verdict.model_dump()
        return verdict
