"""Generator marks in uploads (W6) and the honest label on the agent's own studio photo."""
import asyncio
import io

from PIL import Image, PngImagePlugin

from app import origin
from app.meta_api import MockMetaClient
from app.pipeline import Agent
from tests.conftest import product_photo

MUSE_XMP = origin.COMPOSITE_XMP.replace("compositeWithTrainedAlgorithmicMedia", "trainedAlgorithmicMedia")


def png(**text) -> bytes:
    info = PngImagePlugin.PngInfo()
    for k, v in text.items():
        info.add_itxt(k, v)
    buf = io.BytesIO()
    Image.open(io.BytesIO(product_photo())).save(buf, "PNG", pnginfo=info)
    return buf.getvalue()


def test_marks():
    assert origin.marks(product_photo()) == []
    assert origin.marks(png(**{"XML:com.adobe.xmp": MUSE_XMP})) == ["IPTC trainedAlgorithmicMedia"]
    assert origin.marks(png(parameters="a box of crab noodles, Steps: 30, Sampler: Euler")) == ["PNG text 'parameters'"]
    assert origin.marks(png(Description="holiday photo")) == []


def test_generated_upload_warns_and_studio_photo_is_labelled(settings, tmp_path):
    agent = Agent(MockMetaClient(settings), tmp_path / "runs")
    run = agent.create([png(**{"XML:com.adobe.xmp": MUSE_XMP})], None, "Áo thun cotton nam, giá 120 nghìn, còn 5 cái")
    asyncio.run(agent.process(run))
    v = run.verification
    assert run.status == "ready" and v["verdict"] == "warn" and "W6" in v["rule_ids"]
    assert any(s.key == "origin" and s.status == "warn" for s in run.steps)

    clean = agent.create([product_photo()], None, "Áo thun cotton nam, giá 120 nghìn, còn 5 cái")
    asyncio.run(agent.process(clean))
    assert "W6" not in clean.verification["rule_ids"]
    studio = (tmp_path / "runs" / clean.id / clean.studio_image).read_bytes()
    assert origin.marks(studio) == ["IPTC compositeWithTrainedAlgorithmicMedia"]
