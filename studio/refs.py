"""refs: `ShotList` → reference images (character in 3 angles, empty location, plain props). Muse Image.

A face can be supplied instead of painted (`character_photos`): the same person in every shot and every
product, from a licensed source the owner chose. Those files are used as they are and never copied into git.
"""
from __future__ import annotations

from pathlib import Path

from app.config import Settings
from studio import log, require_prompt, work_dir
from studio.models import RefImage, Refs, RefsInput

INPUT, OUTPUT, ROLE = RefsInput, Refs, "art"

ANGLES = ("front", "three-quarter", "profile")

CHARACTER_PROMPT = (
    "Reference sheet of one person for a video character: {character}. {angle} view, head and shoulders, "
    "looking straight ahead with a neutral calm face, hair worn down and tucked behind the left ear, the same "
    "clothes and the same small earrings in every view, plain light-grey studio background, flat even light, "
    "no shadows on the background. Vietnamese person with Vietnamese features. Photorealistic, full-frame "
    "camera, natural colour, sharp. No text, no logos, no watermark, no props, no second person.")
LOCATION_PROMPT = (
    "Empty location reference, nobody in frame: {location}. Vertical 9:16, eye level, the whole room layout "
    "visible, furniture and objects in their places, Vietnamese home as it really is in the 2020s (not Japanese, "
    "Korean, Chinese or Thai interiors). Every object plain and unmarked. Soft natural light. Photorealistic, "
    "full-frame camera, natural colour. No people, no text, no logos, no packaging, no watermark.")
PROP_PROMPT = (
    "Product-photo reference of one prop: {prop}, alone on a plain wooden table, plain surface, no labels, "
    "no printing, no text, no logos, soft window light, 45-degree angle, photorealistic, natural colour, "
    "no people, no hands, no watermark.")


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
        supplied = [p for p in inp.character_photos if Path(p).is_file()]
        if "character" in roles and supplied:
            for i, photo in enumerate(supplied):
                images.append(RefImage(role="character", angle=ANGLES[0] if i == 0 else f"supplied-{i + 1}",
                                       path=str(photo), source="supplied"))
        elif "character" in roles:
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
        step.decision = f"{len(images)} reference images" + (f", character supplied ({len(supplied)})" if supplied else "")
        step.output["refs"] = [i.model_dump() for i in images]
        return Refs(images=images)
