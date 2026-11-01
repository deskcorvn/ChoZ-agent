"""The default chain on one sample product folder: script → shotlist → refs → voice → frames → render → qc → edit → judge.

The listing comes from the folder (expected.json, note.txt, photo-1.*) rather than from the app, so the
demo does not depend on a listing run. frames/render/qc loop at most `ROUNDS` times per shot; the qc
verdict's director note is folded into the next round's frame prompt.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.config import Settings
from studio import edit, frames, judge, qc, refs, render, script, shotlist, voice, work_dir
from studio.models import (
    EditInput,
    FramesInput,
    JudgeInput,
    QcInput,
    QcVerdict,
    RefsInput,
    RenderInput,
    ScriptInput,
    ShotListInput,
    VoiceInput,
)

ROUNDS = 2


def listing_from_folder(folder: Path) -> tuple[dict, list[str]]:
    """A listing dict in the app's shape from a `samples/products/*` folder."""
    expected = json.loads((folder / "expected.json").read_text(encoding="utf-8-sig")) if (folder / "expected.json").is_file() else {}
    note = (folder / "note.txt").read_text(encoding="utf-8-sig").strip() if (folder / "note.txt").is_file() else ""
    photos = sorted(str(p) for p in folder.glob("photo-*") if p.suffix.lower() in (".png", ".jpg", ".jpeg"))
    first = re.split(r"[,.]", note)[0].strip() or folder.name
    listing = {"title": first[:1].upper() + first[1:], "description": note,
               "category": expected.get("category", ""), "price_vnd": expected.get("price_vnd"),
               "stock": expected.get("stock"), "attributes": [], "source": "sample folder"}
    return listing, photos


async def run_demo(client, settings: Settings, product_dir: Path, out_dir: Path | None = None,
                   renderer: render.Renderer | None = None) -> dict:
    folder = work_dir(settings, str(out_dir) if out_dir else None, product_dir.name)
    listing, photos = listing_from_folder(product_dir)
    outputs: dict = {"product": product_dir.name, "work_dir": str(folder)}

    def save(name: str, model) -> None:
        (folder / f"{name}.json").write_text(model.model_dump_json(indent=2), encoding="utf-8")
        outputs[name] = model.model_dump()

    the_script = await script.run(client, settings, ScriptInput(listing=listing, photos=photos))
    save("script", the_script)
    the_shots = await shotlist.run(client, settings, ShotListInput(script=the_script))
    save("shotlist", the_shots)
    the_refs = await refs.run(client, settings, RefsInput(shotlist=the_shots, photos=photos, work_dir=str(folder)))
    save("refs", the_refs)
    the_voice = await voice.run(client, settings, VoiceInput(script=the_script, work_dir=str(folder)))
    save("voice", the_voice)

    takes: list[QcVerdict] = []
    rounds: dict[str, int] = {}
    for shot in the_shots.shots:
        for attempt in range(1, ROUNDS + 1):
            rounds[shot.id] = attempt
            pick = await frames.run(client, settings, FramesInput(shot=shot, refs=the_refs, work_dir=str(folder)))
            job = await render.run(client, settings, RenderInput(shot=shot, frame_path=pick.path, work_dir=str(folder)),
                                   renderer=renderer)
            if job.status != "done" or not job.clip_path:
                break
            verdict = await qc.run(client, settings, QcInput(shot=shot, clip_path=job.clip_path, refs=the_refs))
            if verdict.verdict == "accept":
                takes.append(verdict)
                break
            if verdict.verdict == "reject":
                break
            shot = shot.model_copy(update={"frame_prompt": f"{shot.frame_prompt}. {verdict.director_note}".strip()})
    outputs["rounds"] = rounds
    outputs["takes"] = [t.model_dump() for t in takes]
    if not takes:
        outputs["status"] = "no usable take"
        return outputs

    plan = await edit.run(client, settings, EditInput(script=the_script, voice=the_voice, takes=takes,
                                                      packshot_path=photos[0] if photos else None, work_dir=str(folder)))
    save("edit", plan)
    report = await judge.run(client, settings, JudgeInput(video_path=plan.output_path, script=the_script))
    save("judge", report)
    outputs["status"] = "done"
    return outputs
