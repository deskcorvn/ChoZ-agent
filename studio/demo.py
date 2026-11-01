"""The default chain on one sample product folder: script → shotlist → refs → voice → frames → render → qc → edit → judge.

The listing comes from the folder (expected.json, note.txt, photo-1.*) rather than from the app, so the
demo does not depend on a listing run. frames/render/qc loop at most `ROUNDS` times per shot; a frame
pick that rejects every candidate, or a qc "retry", folds its note into the next round's frame prompt.
The packshot is the seller's photo cut out by SAM 3.1 (never repainted); the TTS plug-in is `$STUDIO_TTS`
(`vieneu` when `$VIENEU_PYTHON` is set, else `mock`); the mp4 is assembled when ffmpeg is available.
"""
from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path

from PIL import Image

from app import compose
from app.config import Settings
from studio import edit, frames, judge, log, qc, refs, render, script, shotlist, voice, work_dir
from studio.models import (
    EditInput,
    FramesInput,
    JudgeInput,
    QcInput,
    QcVerdict,
    Refs,
    RefsInput,
    RenderInput,
    Script,
    ScriptInput,
    ShotList,
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


def tts_engine() -> str:
    engine = os.environ.get("STUDIO_TTS", "").strip().lower()
    if engine:
        return engine
    return "vieneu" if os.environ.get("VIENEU_PYTHON") and os.environ.get("VIENEU_VO") else "mock"


async def packshot(client, settings: Settings, photo: str, folder: Path, product: str, listing_phrase: str = "") -> str:
    """The real box cut out of the seller's photo by SAM 3.1; the photo itself when the cut-out fails."""
    out = folder / "packshot.png"
    async with log.step("packshot", "Cut the real product out of the seller's photo", "art", Path(photo).name,
                        mock=client.mock) as step:
        raw = compose.normalise_photo(Path(photo).read_bytes())
        try:
            # SAM wants a short concrete English noun phrase. Measured 1 Nov 2026 on the cháo hàu box:
            # "the product package" and "cup" find nothing; "instant porridge cup" and
            # "round food container with lid" find the box. Try the listing's own phrase first.
            phrases = [p for p in (listing_phrase, "the product package", "round food container with lid",
                                   "food package") if p]
            segments, used = [], ""
            for phrase in phrases:
                segments, meta = await client.segment(raw, phrase)
                step.apply(meta)
                if segments:
                    used = phrase
                    break
            if not segments:
                raise ValueError("SAM found no object")
            best = max(segments, key=lambda s: s.area)
            compose.cutout(Image.open(io.BytesIO(raw)), best.mask).save(out)
            step.decision = f"cut-out {best.area} px ('{used}')"
        except Exception as exc:  # the demo must still finish: the photo stands in for the cut-out
            Image.open(io.BytesIO(raw)).convert("RGBA").save(out)
            step.decision = f"photo used as-is ({type(exc).__name__})"
    return str(out)


async def run_demo(client, settings: Settings, product_dir: Path, out_dir: Path | None = None,
                   renderer: render.Renderer | None = None) -> dict:
    folder = work_dir(settings, str(out_dir) if out_dir else None, product_dir.name)
    listing, photos = listing_from_folder(product_dir)
    outputs: dict = {"product": product_dir.name, "work_dir": str(folder)}

    def save(name: str, model) -> None:
        (folder / f"{name}.json").write_text(model.model_dump_json(indent=2), encoding="utf-8")
        outputs[name] = model.model_dump()

    def resume(name: str, model_type):
        """`$STUDIO_RESUME=1`: reuse a saved stage from an earlier run in the same folder (no credits spent twice)."""
        path = folder / f"{name}.json"
        if os.environ.get("STUDIO_RESUME") and path.is_file():
            model = model_type.model_validate_json(path.read_text(encoding="utf-8"))
            outputs[name] = model.model_dump()
            outputs.setdefault("resumed", []).append(name)
            return model
        return None

    the_script = resume("script", Script) or await script.run(client, settings, ScriptInput(listing=listing, photos=photos))
    save("script", the_script)
    the_shots = resume("shotlist", ShotList) or await shotlist.run(client, settings, ShotListInput(script=the_script))
    save("shotlist", the_shots)
    the_refs = resume("refs", Refs) or await refs.run(client, settings, RefsInput(shotlist=the_shots, photos=photos, work_dir=str(folder)))
    save("refs", the_refs)
    the_voice = await voice.run(client, settings, VoiceInput(script=the_script, work_dir=str(folder), tts=tts_engine()))
    save("voice", the_voice)

    takes: list[QcVerdict] = []
    rounds: dict[str, int] = {}
    frames_rejected = 0
    for shot in the_shots.shots:
        for attempt in range(1, ROUNDS + 1):
            rounds[shot.id] = attempt
            pick = await frames.run(client, settings, FramesInput(shot=shot, refs=the_refs, work_dir=str(folder)))
            if len(pick.rejected) >= len(pick.candidates):          # no usable first frame: fix the prompt, no render
                frames_rejected += 1
                note = " ".join(pick.reasons)[:400]
                shot = shot.model_copy(update={"frame_prompt": f"{shot.frame_prompt} Fix: {note}".strip()})
                continue
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
            shot = shot.model_copy(update={"frame_prompt": f"{shot.frame_prompt}. {verdict.director_note}".strip(),
                                           "motion_prompt": f"{shot.motion_prompt} {verdict.director_note}".strip()})
    outputs["rounds"] = rounds
    outputs["frames_rejected_before_render"] = frames_rejected
    outputs["takes"] = [t.model_dump() for t in takes]
    if not takes:
        outputs["status"] = "no usable take"
        return outputs

    pack = await packshot(client, settings, photos[0], folder, the_script.product,
                          str(listing.get("product_phrase_en") or "")) if photos else None
    plan = await edit.run(client, settings, EditInput(script=the_script, voice=the_voice, takes=takes,
                                                      packshot_path=pack, work_dir=str(folder),
                                                      render=render.ffmpeg_path() is not None))
    save("edit", plan)
    if plan.rendered or client.mock:
        report = await judge.run(client, settings, JudgeInput(video_path=plan.output_path, script=the_script))
        save("judge", report)
    else:
        outputs["judge"] = None
    outputs["status"] = "done"
    return outputs
