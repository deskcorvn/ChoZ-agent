"""Read what a photo says about where it came from, before it is re-encoded (re-encoding drops metadata).

Looks for the marks generators write: IPTC DigitalSourceType in XMP (Muse Image writes `trainedAlgorithmicMedia`),
C2PA content credentials (a JUMBF `c2pa` box), and generator names in EXIF Software or PNG text chunks (Stable
Diffusion `parameters`, ComfyUI `prompt`/`workflow`). A mark means the photo was generated or edited by a model; no
mark proves nothing, because metadata is easy to strip. Local only, no model call.

Also writes the honest label on the agent's own output: the studio photo is the seller's real pixels on a room
painted by Muse Image, i.e. IPTC `compositeWithTrainedAlgorithmicMedia`.
"""
from __future__ import annotations

import io
import re

from PIL import Image

IPTC = "http://cv.iptc.org/newscodes/digitalsourcetype/"
GENERATED = ("trainedAlgorithmicMedia", "compositeSynthetic", "algorithmicMedia",
             "compositeWithTrainedAlgorithmicMedia")
GENERATOR_NAMES = re.compile(r"dall[·\-\s]?e|midjourney|stable diffusion|firefly|imagen|gemini|chatgpt|gpt-image|"
                             r"meta ai|muse image|flux|ideogram|leonardo|novelai|comfyui|automatic1111", re.I)
COMPOSITE_XMP = ('<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
                 '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about="" '
                 'xmlns:iptcExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/" '
                 f'iptcExt:DigitalSourceType="{IPTC}compositeWithTrainedAlgorithmicMedia"/></rdf:RDF></x:xmpmeta>'
                 '<?xpacket end="w"?>')


def marks(photo: bytes) -> list[str]:
    """Generator marks found in one uploaded photo, e.g. ["IPTC trainedAlgorithmicMedia", "C2PA manifest"]."""
    found = []
    for kind in GENERATED:
        if (IPTC + kind).encode() in photo or f'DigitalSourceType="{kind}"'.encode() in photo:
            found.append(f"IPTC {kind}")
            break
    if b"c2pa" in photo and b"jumb" in photo:
        found.append("C2PA manifest")
    try:
        im = Image.open(io.BytesIO(photo))
        software = str(im.getexif().get(0x0131, "")) + " " + str(im.getexif().get(0x010F, ""))
        if GENERATOR_NAMES.search(software):
            found.append(f"EXIF software: {software.strip()[:40]}")
        for key in ("parameters", "prompt", "workflow", "Dream", "Software", "Description", "Comment"):
            value = str(im.info.get(key, ""))
            if value and (key in ("parameters", "prompt", "workflow", "Dream") or GENERATOR_NAMES.search(value)):
                found.append(f"PNG text '{key}'")
                break
    except Exception:  # unreadable metadata is not evidence either way
        pass
    return found
