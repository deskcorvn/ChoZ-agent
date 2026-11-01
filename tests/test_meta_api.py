"""Request shapes sent to Meta Model API and how responses are read, without a network."""
import asyncio
import base64
import json
from dataclasses import replace

import httpx
import pytest

from app import prices
from app.meta_api import MetaAPIError, MetaClient, segments_from_text, transcript_text
from tests.conftest import product_photo, wav_bytes

# The SAM 3.1 example from dev.meta.ai/docs/sam/reading-segmentation (two objects, 320×334 frame).
SAM_EXAMPLE = ("<0f>0<|box;x1=211;y1=228;x2=270;y2=254;w=320;h=334|>"
               "<|mask;x=0;y=0;data=27,60,~!!!!M!0c[0o91w?q1!pIH4pPRVp2B3'7`e.ioeAf6-k/#Xd8%dX9x(|>"
               ",1<|box;x1=155;y1=228;x2=202;y2=254;w=320;h=334|>"
               "<|mask;x=0;y=0;data=27,48,~!!!!J!0c[q=Pj_zs=*4C4(/./x#:/`S_GnD`=o3?X{emCgO$y@|>")


def run(coro):
    return asyncio.run(coro)


def client_with(settings, handler):
    return MetaClient(replace(settings, mock=False), transport=httpx.MockTransport(handler))


def test_chat_json_sends_images_as_data_urls_and_a_json_schema(settings):
    seen = {}

    def handler(request):
        seen["path"], seen["auth"] = request.url.path, request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}],
                                         "usage": {"prompt_tokens": 2000, "completion_tokens": 300}})

    async def go():
        client = client_with(settings, handler)
        try:
            return await client.chat_json("sys", "hello", [product_photo()], "Thing", {"type": "object"})
        finally:
            await client.aclose()

    result, meta = run(go())
    assert result == {"ok": True}
    assert seen["path"] == "/v1/chat/completions"
    assert seen["auth"] == "Bearer test-key"
    body = seen["body"]
    assert body["model"] == "muse-spark-1.3" and body["reasoning_effort"] == "minimal"
    assert body["response_format"] == {"type": "json_schema", "json_schema": {"name": "Thing", "schema": {"type": "object"}}}
    image = body["messages"][1]["content"][1]
    assert image["type"] == "image_url" and image["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert meta["cost_usd"] == pytest.approx((2000 * 1.25 + 300 * 4.25) / 1_000_000)


def test_cached_tokens_are_billed_at_the_cached_rate():
    usage = {"prompt_tokens": 1000, "completion_tokens": 0, "prompt_tokens_details": {"cached_tokens": 800}}
    assert prices.text_cost("muse-spark-1.3", usage) == pytest.approx((200 * 1.25 + 800 * 0.15) / 1_000_000)


def test_transcribe_sends_multipart_and_falls_back_without_language_bias(settings):
    requests = []

    def handler(request):
        text = request.content.decode("utf-8", "replace")
        requests.append(text)
        if '"languageBias"' in text:
            return httpx.Response(400, json={"error": {"message": "unknown field languageBias"}})
        return httpx.Response(200, json={"transcript": "áo thun 120 nghìn"})

    async def go():
        client = client_with(settings, handler)
        try:
            return await client.transcribe(wav_bytes(seconds=10))
        finally:
            await client.aclose()

    text, meta = run(go())
    assert text == "áo thun 120 nghìn"
    assert len(requests) == 2 and '"languageBias"' not in requests[1]
    assert 'name="request"' in requests[1] and '"audioEncoding": "WAV"' in requests[1]
    assert 'name="audio"' in requests[1]
    assert meta["usage"] == {"audio_seconds": 10.0, "language_bias": "auto"}
    assert meta["cost_usd"] == pytest.approx(10 / 3600 * 0.18)


@pytest.mark.parametrize("body,expected", [
    ({"transcript": " a "}, "a"),
    ({"text": "b"}, "b"),
    ({"turns": [{"transcript": "c"}, {"transcript": "d"}]}, "c d"),
])
def test_transcript_text_accepts_the_plausible_shapes(body, expected):
    assert transcript_text(body) == expected


def test_transcript_text_says_which_keys_it_saw():
    with pytest.raises(ValueError, match="sessionId"):
        transcript_text({"sessionId": "x"})


def test_sam_example_decodes_to_full_frame_masks():
    segments = segments_from_text(SAM_EXAMPLE, width=320, height=334)
    assert [s.object_id for s in segments] == ["0", "1"]
    first = segments[0]
    assert first.mask.shape == (334, 320)
    assert first.box == (211, 228, 271, 255)
    assert first.area == 1539                       # every decoded pixel landed inside the frame
    rows, cols = first.mask.nonzero()
    assert rows.min() >= 228 and cols.min() >= 211 and rows.max() < 255 and cols.max() < 271


def test_segment_reads_the_sse_stream(settings):
    def handler(request):
        assert request.url.path == "/v1/responses"
        body = json.loads(request.content)
        assert body["model"] == "sam-3.1" and body["stream"] is True
        assert body["input"][0]["content"][0] == {"type": "input_text", "text": "red box"}
        events = [{"type": "response.output_text.delta", "delta": SAM_EXAMPLE[:120]},
                  {"type": "response.output_text.delta", "delta": SAM_EXAMPLE[120:]},
                  {"type": "response.completed"}]
        stream = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    async def go():
        client = client_with(settings, handler)
        try:
            return await client.segment(product_photo(size=(320, 334)), "red box")
        finally:
            await client.aclose()

    segments, meta = run(go())
    assert len(segments) == 2
    assert meta["cost_usd"] == pytest.approx(0.0025)


def test_generate_image_decodes_b64(settings):
    png = b"\x89PNG fake"

    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/images/generations"
        assert body["model"] == "muse-image-1.0" and body["response_format"] == "b64_json"
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png).decode()}]})

    async def go():
        client = client_with(settings, handler)
        try:
            return await client.generate_image("an empty studio")
        finally:
            await client.aclose()

    image, meta = run(go())
    assert image == png and meta["cost_usd"] == 0.01


def test_http_errors_name_the_endpoint_and_never_the_key(settings):
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    async def go():
        client = client_with(settings, handler)
        try:
            await client.generate_image("x")
        finally:
            await client.aclose()

    with pytest.raises(MetaAPIError) as exc:
        run(go())
    assert exc.value.status == 401 and "/images/generations" in str(exc.value)
    assert "test-key" not in str(exc.value)


def test_a_dropped_connection_is_retried_once_then_classified(settings):
    """One retry with jitter on a network error; a second failure is a MetaAPIError of kind 'network'."""
    import httpx as _httpx

    from app.meta_api import MetaAPIError, MetaClient

    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            raise _httpx.ConnectError("boom")
        return _httpx.Response(429, text="slow down")

    client = MetaClient(settings, transport=_httpx.MockTransport(handler))
    try:
        asyncio.run(client._post("/chat/completions", json={}))
    except MetaAPIError as exc:
        assert exc.kind == "quota" and exc.status == 429
    assert len(calls) == 2


def test_error_kinds():
    from app.meta_api import error_kind

    assert [error_kind(s) for s in (0, 401, 429, 503, 400)] == ["network", "auth", "quota", "server", "request"]


def test_spark_down_falls_back_to_the_pinned_model(settings, monkeypatch):
    import asyncio
    import json

    import httpx

    from app.meta_api import MetaClient

    async def no_wait(*_):
        return None
    monkeypatch.setattr("app.meta_api.asyncio.sleep", no_wait)
    seen = []

    def handler(request):
        model = json.loads(request.content)["model"]
        seen.append(model)
        if model == "muse-spark-1.3":
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, json={"choices": [{"message": {"content": "{\"ok\": \"yes\"}"}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 2}})

    client = MetaClient(settings, transport=httpx.MockTransport(handler))
    out, meta = asyncio.run(client.chat_json("s", "t", [], "Ok", {"type": "object"}))
    assert out == {"ok": "yes"} and meta["model"] == "muse-spark-1.2" and meta["fallback_reason"] == "server"
    assert seen == ["muse-spark-1.3", "muse-spark-1.3", "muse-spark-1.2"]          # retried once, then fell back

    monkeypatch.setenv("LISTING_FALLBACK_TEXT_MODEL", "")
    import pytest

    with pytest.raises(Exception, match="503"):
        asyncio.run(MetaClient(settings, transport=httpx.MockTransport(handler)).chat_json("s", "t", [], "Ok", {}))
