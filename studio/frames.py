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

# TODO(prompt): the picker's system prompt. Reject before rank: extra hands or fingers, text or logos,
# a product that differs from the reference photo, a second person. Then rank by light, framing and
# the action the shot needs. Answer in the FramePick schema with the candidate index.
PICK_PROMPT = ""


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
            raw.setdefault("candidates", [str(c) for c in candidates])
            raw["shot_id"] = inp.shot.id
            pick = FramePick.model_validate(raw)
        if inp.second_opinion:
            pick.second_opinion = await second_opinion(candidates, inp.shot.frame_prompt)
        step.decision = f"picked {pick.picked + 1}/{len(candidates)}, rejected {len(pick.rejected)}"
        step.output["pick"] = pick.model_dump()
        return pick
