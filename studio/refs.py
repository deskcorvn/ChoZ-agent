"""refs: `ShotList` → reference images (character in 3 angles, empty location, plain props). Muse Image."""
from __future__ import annotations

from pathlib import Path

from app.config import Settings
from studio import log, require_prompt, work_dir
from studio.models import RefImage, Refs, RefsInput

INPUT, OUTPUT, ROLE = RefsInput, Refs, "art"

ANGLES = ("front", "three-quarter", "profile")

# TODO(prompt): one template per role. Character: neutral background, same clothes in every angle,
# no text. Location: empty, no people. Prop: alone on a plain surface. The product is never painted:
# it comes from the seller's photo.
CHARACTER_PROMPT = ""
LOCATION_PROMPT = ""
PROP_PROMPT = ""


async def _paint(client, step: log.Step, prompt: str, path: Path, mock: bool) -> None:
    image, meta = await client.generate_image(prompt if not mock else "mock reference", size="1024x1024")
    step.apply(meta)
    path.write_bytes(image)


async def run(client, settings: Settings, inp: RefsInput) -> Refs:
    folder = work_dir(settings, inp.work_dir, "refs")
    bible = inp.shotlist.bible
    roles = sorted({role for shot in inp.shotlist.shots for role in shot.ref_roles})
    describe = f"roles {', '.join(roles)}; {len(inp.photos)} product photo(s)"
    async with log.step("refs", "Shot list → reference images", ROLE, describe, mock=client.mock) as step:
        images: list[RefImage] = []
        source = "mock" if client.mock else "muse-image"
        if "character" in roles:
            for angle in ANGLES:
                path = folder / f"character-{angle}.png"
                prompt = "" if client.mock else require_prompt("refs", CHARACTER_PROMPT).format(
                    character=bible.character, angle=angle)
                await _paint(client, step, prompt, path, client.mock)
                images.append(RefImage(role="character", angle=angle, path=str(path), source=source))
        if "location" in roles:
            path = folder / "location.png"
            prompt = "" if client.mock else require_prompt("refs", LOCATION_PROMPT).format(location=bible.location)
            await _paint(client, step, prompt, path, client.mock)
            images.append(RefImage(role="location", path=str(path), source=source))
        for i, prop in enumerate(bible.props):
            path = folder / f"prop-{i + 1}.png"
            prompt = "" if client.mock else require_prompt("refs", PROP_PROMPT).format(prop=prop)
            await _paint(client, step, prompt, path, client.mock)
            images.append(RefImage(role="prop", angle=prop, path=str(path), source=source))
        for photo in inp.photos[:1]:                     # the real product, never repainted
            images.append(RefImage(role="product", path=str(photo), source="photo"))
        if client.mock:
            step.model, step.endpoint = settings.image_model, "POST /v1/images/generations"
        step.decision = f"{len(images)} reference images"
        step.output["refs"] = [i.model_dump() for i in images]
        return Refs(images=images)
