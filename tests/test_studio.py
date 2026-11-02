"""The studio chain on the mock client (nine services, one sample product), the log, the two new client calls, the routes."""
import asyncio
import base64
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import ROOT
from app.meta_api import MetaClient, MockMetaClient
from app.server import create_app
from studio import SERVICES, PromptMissing, log, script
from studio.demo import run_demo
from studio.models import (
    EditClip,
    EditPlan,
    FramePick,
    JudgeReport,
    QcVerdict,
    Refs,
    RenderJob,
    Script,
    ScriptInput,
    ShotList,
    VoiceCheck,
)
from studio.render import KlingMCPRenderer, MockRenderer, ffmpeg_path
from tests.conftest import product_photo

PRODUCT = ROOT / "samples" / "products" / "05-chao-hau-hop"


@pytest.fixture(autouse=True)
def studio_log(tmp_path, monkeypatch):
    path = tmp_path / "log.jsonl"
    monkeypatch.setenv("STUDIO_LOG", str(path))
    return path


class FakeRenderer:
    """A render plug-in for tests without ffmpeg: the clip is the frame, renamed."""

    name = "mock"

    def __init__(self, out_dir: Path):
        self.out_dir, self.jobs = out_dir, {}

    async def submit(self, frame_png, prompt, seconds):
        job_id = f"job{len(self.jobs) + 1}"
        clip = self.out_dir / f"{job_id}.mp4"
        clip.write_bytes(frame_png[:64])
        self.jobs[job_id] = {"status": "done", "path": str(clip), "credits": 0}
        return job_id

    async def poll(self, job_id):
        return self.jobs[job_id]


def test_the_mock_chain_runs_all_nine_services_on_a_sample_product(settings, studio_log, tmp_path):
    out = asyncio.run(run_demo(MockMetaClient(settings), settings, PRODUCT, renderer=FakeRenderer(tmp_path)))
    assert out["status"] == "done", out
    Script.model_validate(out["script"])
    shots = ShotList.model_validate(out["shotlist"])
    refs = Refs.model_validate(out["refs"])
    VoiceCheck.model_validate(out["voice"])
    assert all(QcVerdict.model_validate(t).verdict == "accept" for t in out["takes"])
    plan = EditPlan.model_validate(out["edit"])
    JudgeReport.model_validate(out["judge"])
    assert len(out["takes"]) == len(shots.shots) and all(r == 1 for r in out["rounds"].values())
    assert plan.seconds <= 35 and plan.end_card
    assert [r.role for r in refs.images].count("character") == 3 and refs.for_role("product")[0].source == "photo"
    assert out["script"]["product"].startswith("Cháo hàu") and any("22.000" in b for b in out["script"]["beats"])
    folder = Path(out["work_dir"])
    assert (folder / "script.json").is_file() and (folder / "voice" / "voice.wav").is_file()

    lines = [json.loads(line) for line in studio_log.read_text(encoding="utf-8").splitlines()]
    assert {line["key"] for line in lines} >= set(SERVICES) - {"banner"}   # banner is not part of the spot chain
    for line in lines:
        assert line["status"] == "done" and line["mock"] is True and line["cost_usd"] == 0.0, line
        assert line["role"] and line["model"] and line["endpoint"] and line["decision"] and line["ms"] >= 0
    assert log.tail(3) == lines[-3:]


def test_a_supplied_face_is_the_character_in_every_shot_and_nothing_is_painted_for_it(settings, studio_log, tmp_path):
    face = tmp_path / "face.png"
    face.write_bytes(product_photo())
    out = asyncio.run(run_demo(MockMetaClient(settings), settings, PRODUCT, out_dir=tmp_path / "w",
                               renderer=FakeRenderer(tmp_path), characters=[str(face)]))
    assert out["status"] == "done", out
    characters = Refs.model_validate(out["refs"]).for_role("character")
    assert [(c.path, c.source) for c in characters] == [(str(face), "supplied")]
    assert not (Path(out["work_dir"]) / "refs" / "character-front.png").exists()


def test_the_shots_keep_the_time_of_day_the_voice_over_names(settings):
    from studio import shotlist
    from studio.models import ShotListInput
    beats = ["Tối muộn bụng đói mà ngại nấu nướng không?", "Có hộp cháo hàu ăn liền.",
             "Vị đặc sản Hải Phòng.", "Một hộp hai mươi hai nghìn."]
    the_script = Script(product="Cháo hàu", beats=beats, supers=["22 nghìn"], cta="Đặt trên Chợ Z", claims=[])
    shots = asyncio.run(shotlist.run(MockMetaClient(settings), settings, ShotListInput(script=the_script)))
    assert shots.bible.time_of_day == "night" and all("lamp" in shot.light for shot in shots.shots)
    assert shotlist.time_from_script(["Cháo hàu ngon"]) == ""


def test_every_service_exposes_input_output_and_run():
    from studio import service
    for name in SERVICES:
        module = service(name)
        assert hasattr(module, "INPUT") and hasattr(module, "OUTPUT") and callable(module.run), name


def test_a_live_client_without_a_prompt_is_refused_before_any_call(settings, monkeypatch):
    monkeypatch.setattr(script, "PROMPT", "")                           # as the skeleton shipped
    live = type("Live", (), {"mock": False})()
    with pytest.raises(PromptMissing, match="TODO\\(prompt\\)"):
        asyncio.run(script.run(live, settings, ScriptInput(listing={"title": "x"})))
    lines = log.tail()
    assert lines[-1]["status"] == "failed" and lines[-1]["mock"] is False


def test_a_long_beat_fails_validation():
    with pytest.raises(ValueError, match="longer than 12 words"):
        Script(product="x", beats=["một " * 13] + ["a"] * 3, cta="x")


def test_qc_window_must_end_after_it_starts():
    with pytest.raises(ValueError, match="before it starts"):
        QcVerdict(shot_id="s01", clip_path="c.mp4", usable_start_s=3, usable_end_s=1, verdict="accept")


def test_edit_plan_validator_rejects_cuts_outside_the_usable_window():
    from studio.edit import validate_plan
    plan = EditPlan(clips=[EditClip(shot_id="s01", clip_path="a.mp4", in_s=0, out_s=4, at_s=0),
                           EditClip(shot_id="s02", clip_path="b.mp4", in_s=1, out_s=2, at_s=3)],
                    voice_path="v.wav", output_path="o.mp4")
    problems = validate_plan(plan, {"s01": (1.0, 5.0), "s02": (0.0, 5.0)})
    assert any("outside usable" in p for p in problems) and any("overlap" in p for p in problems)
    assert validate_plan(plan, {"s01": (0.0, 5.0), "s02": (0.0, 5.0), "x": (0, 1)})[0].endswith("overlap on the timeline")


def test_the_mock_renderer_reports_a_missing_ffmpeg_instead_of_pretending(tmp_path, monkeypatch):
    monkeypatch.setattr("studio.render.ffmpeg_path", lambda: None)
    renderer = MockRenderer(tmp_path)
    job_id = asyncio.run(renderer.submit(product_photo(), "steam rises", 5.0))
    state = asyncio.run(renderer.poll(job_id))
    assert state["status"] == "failed" and "ffmpeg" in state["error"]


@pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg on PATH or $FFMPEG")
def test_the_mock_renderer_makes_a_five_second_clip_with_ffmpeg(tmp_path):
    renderer = MockRenderer(tmp_path)
    job_id = asyncio.run(renderer.submit(product_photo(), "steam rises", 5.0))
    state = asyncio.run(renderer.poll(job_id))
    assert state["status"] == "done", state
    assert Path(state["path"]).stat().st_size > 1000


def test_the_kling_renderer_takes_its_token_from_the_environment_only(monkeypatch):
    monkeypatch.delenv("KLING_MCP_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="KLING_MCP_TOKEN"):
        KlingMCPRenderer()


def test_the_kling_renderer_speaks_the_live_mcp_dialect(monkeypatch):
    calls, uploads = [], []

    def handler(request):
        if request.url.host == "upload.example":
            uploads.append(request.content)
            return httpx.Response(200, json={"url": "https://files.example/frame.png"})
        body = json.loads(request.content)
        auth = request.headers["authorization"]
        if body["method"] == "initialize":
            calls.append((auth, "initialize", None, None))
            return httpx.Response(200, headers={"Mcp-Session-Id": "s1"}, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})
        if "id" not in body:
            return httpx.Response(202)
        name, args = body["params"]["name"], body["params"]["arguments"]
        calls.append((auth, request.headers.get("mcp-session-id"), name, args))
        answers = {"query_membership_and_credits": {"availableRemainCredits": 3000},
                   "file_upload": {"ticket": "tk", "uploadUrl": "https://upload.example/files"},
                   "image_to_video": {"generationId": "g1"},
                   "query_tasks": {"status": "completed", "creditsConsumed": 40,
                                   "works": [{"urlWithoutWatermark": "https://v/g1.mp4"}]}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
            "content": [{"type": "text", "text": json.dumps(answers[name])}]}})

    async def go():
        renderer = KlingMCPRenderer(token="kling-test-token", credit_floor=1877, transport=httpx.MockTransport(handler))
        try:
            job_id = await renderer.submit(b"png", "steam rises", 5)
            return job_id, await renderer.poll(job_id)
        finally:
            await renderer.aclose()

    job_id, state = asyncio.run(go())
    assert job_id == "g1" and state == {"status": "done", "url": "https://v/g1.mp4", "credits": 40, "error": ""}
    assert [c[2] for c in calls] == [None, "query_membership_and_credits", "file_upload", "image_to_video", "query_tasks"]
    assert all(c[0] == "Bearer kling-test-token" for c in calls) and all(c[1] == "s1" for c in calls[1:])
    i2v = calls[3][3]
    assert i2v["model"] == "kling-video-v3_0" and i2v["inputs"][0]["name"] == "first_image"
    assert {"name": "enable_audio", "value": "false"} in i2v["arguments"] and uploads


def test_the_kling_renderer_refuses_below_the_credit_floor():
    def handler(request):
        body = json.loads(request.content)
        if body["method"] == "initialize" or "id" not in body:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": {}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
            "content": [{"type": "text", "text": json.dumps({"availableRemainCredits": 1800})}]}})

    async def go():
        renderer = KlingMCPRenderer(token="t", credit_floor=1877, transport=httpx.MockTransport(handler))
        try:
            await renderer.submit(b"png", "x", 5)
        finally:
            await renderer.aclose()

    with pytest.raises(RuntimeError, match="floor"):
        asyncio.run(go())


# ---- the two new MetaClient calls, request shapes only -----------------------------------------

def client_with(settings, handler):
    return MetaClient(replace(settings, mock=False), transport=httpx.MockTransport(handler))


def test_images_edit_sends_references_as_data_urls(settings):
    png = b"\x89PNG edited"
    seen = {}

    def handler(request):
        seen["path"], seen["body"] = request.url.path, json.loads(request.content)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png).decode()}]})

    async def go():
        client = client_with(settings, handler)
        try:
            return await client.images_edit("same woman, kitchen", [product_photo(), product_photo()])
        finally:
            await client.aclose()

    image, meta = asyncio.run(go())
    body = seen["body"]
    assert image == png and seen["path"] == "/v1/images/edits"
    assert body["model"] == "muse-image-1.0" and body["n"] == 1 and body["size"] == "1080x1920"
    assert body["output_format"] == "png" and body["prompt"] == "same woman, kitchen"
    assert len(body["images"]) == 2 and body["images"][0]["image_url"].startswith("data:image/jpeg;base64,")
    assert meta["cost_usd"] == 0.01 and meta["usage"]["references"] == 2


def test_responses_file_uploads_asks_with_a_schema_and_deletes(settings, tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"not really mp4")
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/v1/files":
            text = request.content.decode("utf-8", "replace")
            assert 'name="purpose"' in text and "user_data" in text and 'filename="clip.mp4"' in text
            assert "video/mp4" in text
            return httpx.Response(200, json={"id": "file_1", "purpose": "user_data"})
        if request.url.path == "/v1/responses":
            body = json.loads(request.content)
            assert body["model"] == "muse-spark-1.3" and body["reasoning"] == {"effort": "minimal"}
            content = body["input"][0]["content"]
            assert content[0] == {"type": "input_file", "file_id": "file_1"} and content[1]["text"] == "judge it"
            assert body["text"]["format"]["type"] == "json_schema" and body["text"]["format"]["name"] == "QcVerdict"
            return httpx.Response(200, json={
                "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"verdict": "accept"}'}]}],
                "usage": {"input_tokens": 4000, "output_tokens": 200}})
        if request.url.path == "/v1/files/file_1":
            return httpx.Response(200, json={"deleted": True})
        raise AssertionError(request.url.path)

    async def go():
        client = client_with(settings, handler)
        try:
            return await client.responses_file(str(clip), "judge it", {"title": "QcVerdict", "type": "object"}, "video/mp4")
        finally:
            await client.aclose()

    result, meta = asyncio.run(go())
    assert result == {"verdict": "accept"}
    assert requests == [("POST", "/v1/files"), ("POST", "/v1/responses"), ("DELETE", "/v1/files/file_1")]
    assert meta["cost_usd"] == pytest.approx((4000 * 1.25 + 200 * 4.25) / 1_000_000)
    assert meta["usage"]["file_bytes"] == 14 and "test-key" not in json.dumps(meta)


def test_the_file_is_deleted_even_when_the_question_fails(settings, tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/v1/files":
            return httpx.Response(200, json={"id": "file_2"})
        if request.url.path == "/v1/responses":
            return httpx.Response(400, json={"error": {"message": "unsupported file"}})
        return httpx.Response(200, json={})

    async def go():
        client = client_with(settings, handler)
        try:
            await client.responses_file(str(clip), "q", {"type": "object"}, "video/mp4")
        finally:
            await client.aclose()

    with pytest.raises(Exception, match="/responses"):
        asyncio.run(go())
    assert requests[-1] == ("DELETE", "/v1/files/file_2")


# ---- routes ------------------------------------------------------------------------------------

def test_studio_routes_run_a_service_and_show_the_log(settings):
    with TestClient(create_app(settings)) as client:
        listing = {"title": "Cháo hàu ăn liền", "description": "Đặc sản Hải Phòng. Hộp 120 g.", "price_vnd": 22000}
        answer = client.post("/studio/script", json={"listing": listing, "brief": "ấm áp"})
        assert answer.status_code == 200, answer.text
        assert 4 <= len(answer.json()["beats"]) <= 8 and answer.json()["cta"]
        assert client.post("/studio/nope", json={}).status_code == 404
        assert client.post("/studio/script", json={"brief": 1}).status_code == 422
        pick = client.post("/studio/qc", json={"shot": {"id": "s01", "seconds": 5, "action": "a", "frame_prompt": "f",
                                                        "motion_prompt": "m"}, "clip_path": "x.mp4"})
        assert pick.status_code == 200 and QcVerdict.model_validate(pick.json()).verdict == "accept"
        tail = client.get("/studio/log").json()
        assert [line["key"] for line in tail] == ["script", "qc"] and all(line["mock"] for line in tail)
        assert tail[0]["role"] == "writer" and tail[0]["model"] == "muse-spark-1.3"


def test_frame_pick_and_render_job_shapes():
    pick = FramePick(shot_id="s01", candidates=["a.png", "b.png", "c.png"], picked=2)
    assert pick.path == "c.png"
    job = RenderJob(shot_id="s01", engine="mock", job_id="j", status="done", seconds=5)
    assert job.clip_path is None and job.credits is None


def test_subtitles_are_written_as_ass_events_inside_the_safe_zone(tmp_path):
    from studio.edit import ass_file
    from studio.models import Subtitle
    plan = EditPlan(clips=[EditClip(shot_id="s01", clip_path="x.mp4", in_s=0, out_s=2, at_s=0)],
                    subtitles=[Subtitle(text="Đặt ngay trên Chợ Dét", start_s=0.5, end_s=2.0)],
                    voice_path="v.wav", output_path=str(tmp_path / "o.mp4"))
    text = ass_file(plan, tmp_path / "s.ass").read_text(encoding="utf-8")
    assert "Dialogue: 0,0:00:00.50,0:00:02.00,Sub" in text
    assert "Chợ Z" in text and "Chợ Dét" not in text          # the TTS spelling never reaches the screen
    assert "MarginV" in text and ",330,1" in text                  # bottom margin keeps it clear of the app UI


def test_the_end_card_carries_the_real_packshot_and_the_ai_label(tmp_path):
    from PIL import Image

    from studio.edit import end_card
    pack = tmp_path / "pack.png"
    Image.new("RGBA", (200, 300), (200, 30, 30, 255)).save(pack)
    plan = EditPlan(clips=[EditClip(shot_id="s01", clip_path="x.mp4", in_s=0, out_s=2, at_s=0)],
                    voice_path="v.wav", packshot_path=str(pack), output_path=str(tmp_path / "o.mp4"))
    card = Image.open(end_card(plan, "Cháo hàu ăn liền", "22.000đ / hộp", "Đặt ngay trên Chợ Z", tmp_path / "card.png"))
    assert card.size == (1080, 1920)
    assert card.getpixel((540, int(1920 * 0.17) + 100)) == (200, 30, 30)       # the packshot's own pixels, centred
    assert plan.end_card == "Hình ảnh tạo bằng AI"


def test_the_mock_voice_has_one_tone_per_beat_and_a_beat_map(settings, tmp_path, monkeypatch):
    from studio import voice
    from studio.models import VoiceInput
    monkeypatch.setenv("FFMPEG", "")
    script_ = Script(product="x", beats=["Sáng nào cũng vội", "Có bát cháo nóng", "Hai mươi hai nghìn", "Đặt ngay trên Chợ Dét"],
                     cta="Đặt ngay trên Chợ Z")
    check = asyncio.run(voice.run(MockMetaClient(settings), settings, VoiceInput(script=script_, work_dir=str(tmp_path))))
    assert check.wer == 0.0 and len(check.lines) == 4 and check.seconds > 3
    assert [line.beat_index for line in check.lines] == [0, 1, 2, 3]
    assert all(b.end_s > b.start_s for b in check.lines) and check.lines[-1].end_s <= check.seconds + 0.01


def test_the_mock_voice_beat_map_comes_from_the_pauses_when_ffmpeg_is_there(settings, tmp_path):
    from studio import voice
    from studio.models import VoiceInput
    if not ffmpeg_path():
        pytest.skip("ffmpeg not on PATH or $FFMPEG")
    script_ = Script(product="x", beats=["Sáng nào cũng vội", "Có bát cháo nóng mà không phải nấu",
                                         "Hai mươi hai nghìn một hộp", "Đặt ngay trên Chợ Dét"], cta="Đặt ngay trên Chợ Z")
    check = asyncio.run(voice.run(MockMetaClient(settings), settings, VoiceInput(script=script_, work_dir=str(tmp_path))))
    gaps = [b.start_s - a.end_s for a, b in zip(check.lines, check.lines[1:])]
    assert all(0.3 <= g <= 0.6 for g in gaps), gaps                 # the 0.45 s pauses were found, not guessed


def test_wer_ignores_punctuation_case_and_asr_stutters():
    from studio.voice import wer
    assert wer("Đổ nước sôi, đậy nắp.", "đổ đổ nước sôi đậy nắp") == 0.0
    assert 0 < wer("cháo hàu ăn liền", "cháu họ ăn liền") <= 0.5


def test_render_download_saves_the_clip(tmp_path):
    """A finished engine job is saved locally: Kling keeps the URL for a day only."""
    from studio import render

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://v/g1.mp4"
        return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42clip")

    path = asyncio.run(render.download("https://v/g1.mp4", tmp_path / "s2" / "g1.mp4", httpx.MockTransport(handler)))
    assert Path(path).read_bytes().endswith(b"clip")


def test_edit_plan_shot_ids_follow_the_clip_path():
    """Spark renamed s2 to s00 in a live plan; the take's clip path decides the shot id."""
    from studio import edit
    from studio.models import EditClip, EditPlan, QcVerdict

    take = QcVerdict(shot_id="s2", clip_path="F:/x/render/s2/a.mp4", verdict="accept", usable_start_s=0.0, usable_end_s=6.0)
    plan = EditPlan.model_construct(clips=[EditClip(shot_id="s00", clip_path="F:/x/render/s2/a.mp4", in_s=0.5, out_s=5.0, at_s=0.0)])
    assert edit.match_takes(plan, [take]).clips[0].shot_id == "s2"


def test_banner_mock_renders_gif_with_listing_price(settings, tmp_path):
    """The banner service on the mock client: a GIF of the real cut-out, price written from the listing."""
    from PIL import Image as PILImage

    from studio import banner

    cut = PILImage.new("RGBA", (300, 400), (0, 0, 0, 0))
    for x in range(60, 240):
        for y in range(40, 380):
            cut.putpixel((x, y), (200, 60, 40, 255))
    cut.save(tmp_path / "cut.png")
    client = MockMetaClient(settings)
    inp = banner.BannerInput(listing={"title": "Bánh đa cua Khánh Thọ", "price_vnd": 22000, "category": "Thực phẩm"},
                             packshot_path=str(tmp_path / "cut.png"), work_dir=str(tmp_path / "out"), size=360,
                             seconds=2.0, fps=8)
    out = asyncio.run(banner.run(client, settings, inp))
    assert out.price_text == "22.000đ" and out.frames == 16
    gif = PILImage.open(out.gif_path)
    assert gif.n_frames == 16 and gif.size == (360, 360)


def test_banner_plan_rejects_model_written_prices_and_superlatives():
    from studio import banner

    plan = banner.BannerPlan(headline="Ngon nhất Hải Phòng", subline="chỉ 22 nghìn", cta="Đặt ngay", accent="#aa2200",
                             background_prompt="empty table, no text",
                             beats=[banner.Beat(effect="product_rise", start_s=0, end_s=1),
                                    banner.Beat(effect="cta_pulse", start_s=1, end_s=9)])
    problems = banner.validate(plan, 4.0)
    assert any("nhất" in p for p in problems) and any("price" in p for p in problems) and any("timing" in p for p in problems)
