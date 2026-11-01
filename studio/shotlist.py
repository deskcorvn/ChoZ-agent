"""shotlist: `Script` + continuity bible → `ShotList` (5-8 s shots, one action each, locked camera). Muse Spark."""
from __future__ import annotations

from app.config import Settings
from studio import log, require_prompt
from studio.models import SHOTLIST_SCHEMA, Bible, Shot, ShotList, ShotListInput

INPUT, OUTPUT, ROLE = ShotListInput, ShotList, "director"

# TODO(prompt): the director's system prompt. One action per shot, camera locked, light follows the
# bible's time of day, a first-frame prompt (what is in the frame, no text, no extra people) and a
# motion prompt (what moves in 5-8 s) per shot, reference roles named per shot.
PROMPT = ""

_LIGHT = {"morning": "soft window light from the left", "noon": "bright overhead daylight",
          "evening": "warm low sun from the right"}


def _sample(inp: ShotListInput) -> ShotList:
    bible = inp.bible if inp.bible.character else Bible(
        character="a woman in her thirties, plain white shirt", location="a tidy Vietnamese kitchen table",
        props=["a white bowl", "a wooden spoon"], time_of_day="morning")
    light = _LIGHT.get(bible.time_of_day, _LIGHT["morning"])
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
        step.decision = f"{len(shotlist.shots)} shots, {shotlist.seconds:.0f} s"
        step.output["shotlist"] = shotlist.model_dump()
        return shotlist
