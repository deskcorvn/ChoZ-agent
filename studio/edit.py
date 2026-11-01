"""edit: beat map + accepted takes + the real packshot → `EditPlan` (Spark writes it, Python validates it) → mp4.

Output: 1080×1920, subtitles from the script, the real box composited at the end, an end card labelled
AI, −14 LUFS. The assembly itself is ffmpeg; here the plan is built and checked, the render is `TODO(live)`.
"""
from __future__ import annotations

from app.config import Settings
from studio import log, require_prompt, work_dir
from studio.models import EDIT_PLAN_SCHEMA, SPOT_SECONDS, EditClip, EditInput, EditPlan, Subtitle

INPUT, OUTPUT, ROLE = EditInput, EditPlan, "editor"

# TODO(prompt): the editor's prompt. Cut on the beat map: each voice beat gets the take for its shot,
# trimmed inside its usable window; never stretch a take; subtitles are the beats verbatim; the real
# packshot and the AI label close the spot; total ≤ 30 s.
PROMPT = ""


def validate_plan(plan: EditPlan, takes_by_shot: dict[str, tuple[float, float]]) -> list[str]:
    """What is wrong with a plan, as a list; empty means it can be rendered."""
    problems = []
    for clip in plan.clips:
        window = takes_by_shot.get(clip.shot_id)
        if window is None:
            problems.append(f"{clip.shot_id}: not an accepted take")
            continue
        if clip.out_s <= clip.in_s:
            problems.append(f"{clip.shot_id}: empty cut {clip.in_s}-{clip.out_s}")
        if clip.in_s < window[0] or clip.out_s > window[1]:
            problems.append(f"{clip.shot_id}: cut {clip.in_s}-{clip.out_s} outside usable {window[0]}-{window[1]}")
    for a, b in zip(plan.clips, plan.clips[1:]):
        if b.at_s < a.at_s + (a.out_s - a.in_s) - 0.01:
            problems.append(f"{a.shot_id} and {b.shot_id} overlap on the timeline")
    if plan.seconds > SPOT_SECONDS + 5:
        problems.append(f"{plan.seconds:.1f} s is longer than a {SPOT_SECONDS:.0f} s spot")
    if plan.subtitles and any(s.end_s <= s.start_s for s in plan.subtitles):
        problems.append("a subtitle ends before it starts")
    return problems


def _sample(inp: EditInput, output_path: str) -> EditPlan:
    takes = {t.shot_id: t for t in inp.takes}
    clips, subtitles, cursor = [], [], 0.0
    for line in inp.voice.lines:
        shot_id = f"s{line.beat_index + 1:02d}"
        take = takes.get(shot_id) or inp.takes[line.beat_index % len(inp.takes)]
        length = min(max(line.end_s - line.start_s, 1.0), take.usable_seconds)
        clips.append(EditClip(shot_id=take.shot_id, clip_path=take.clip_path, in_s=take.usable_start_s,
                              out_s=round(take.usable_start_s + length, 2), at_s=round(cursor, 2)))
        subtitles.append(Subtitle(text=line.said, start_s=round(cursor, 2), end_s=round(cursor + length, 2)))
        cursor += length
    return EditPlan(clips=clips, subtitles=subtitles, voice_path=inp.voice.audio_path,
                    packshot_path=inp.packshot_path, output_path=output_path)


async def render_plan(plan: EditPlan, ffmpeg: str | None) -> bool:
    """Assemble the mp4 with ffmpeg. TODO(live): concat trimmed clips, voice, subtitles, packshot, loudnorm."""
    return False


async def run(client, settings: Settings, inp: EditInput) -> EditPlan:
    folder = work_dir(settings, inp.work_dir, "edit")
    output_path = str(folder / "spot.mp4")
    describe = f"{len(inp.voice.lines)} beats, {len(inp.takes)} takes, packshot {'yes' if inp.packshot_path else 'no'}"
    async with log.step("edit", "Beat map + takes → edit plan → mp4", ROLE, describe, mock=client.mock) as step:
        if client.mock:
            plan = _sample(inp, output_path)
            step.model, step.endpoint = settings.text_model, "POST /v1/chat/completions"
        else:
            prompt = require_prompt("edit", PROMPT)
            user = (f"Voice:\n{inp.voice.model_dump_json()}\n\nTakes:\n"
                    + "\n".join(t.model_dump_json() for t in inp.takes)
                    + f"\n\nPackshot: {inp.packshot_path}\nOutput: {output_path}")
            raw, meta = await client.chat_json(prompt, user, [], "EditPlan", EDIT_PLAN_SCHEMA)
            step.apply(meta)
            raw.update({"voice_path": inp.voice.audio_path, "output_path": output_path, "rendered": False})
            plan = EditPlan.model_validate(raw)
        problems = validate_plan(plan, {t.shot_id: (t.usable_start_s, t.usable_end_s) for t in inp.takes
                                        if t.verdict == "accept"})
        if problems:
            raise ValueError("edit plan rejected: " + "; ".join(problems))
        if inp.render:
            from studio.render import ffmpeg_path
            plan.rendered = await render_plan(plan, ffmpeg_path())
        step.decision = f"{len(plan.clips)} cuts, {plan.seconds:.1f} s, rendered={plan.rendered}"
        step.output["plan"] = plan.model_dump()
        return plan
