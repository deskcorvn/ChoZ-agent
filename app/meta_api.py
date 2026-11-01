"""Meta Model API calls used by the agent, plus a mock with the same interface.

Request shapes follow dev.meta.ai (read 31/10/2026):
- Muse Spark: POST /chat/completions, images as base64 `data:` URLs, `response_format` json_schema.
- Muse Voice Transcribe: POST /asr/transcribe, multipart parts `request` (JSON) and `audio`.
- SAM 3.1: POST /responses with stream=true; boxes and masks arrive as special-token text, decoded
  with Meta's `meta_sam_parser`.
- Muse Image: POST /images/generations, `b64_json`.
- Studio (preview, not yet verified live): POST /images/edits with reference images; POST /files +
  POST /responses with `input_file` for a video, then DELETE /files/{id}.

Every call returns `(result, meta)`; `meta` is what the agent log shows: endpoint, model, usage, cost.
The key is only ever sent in the Authorization header and never logged.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import random
import re
import wave
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np
from meta_sam_parser import CompletedOutcome, decode_mask_to_raster, image_segmentation_format
from PIL import Image, ImageDraw, ImageFilter

from app import prices
from app.config import Settings


class MetaAPIError(RuntimeError):
    def __init__(self, endpoint: str, status: int, body: str):
        self.endpoint, self.status, self.body = endpoint, status, body
        self.kind = error_kind(status)
        super().__init__(f"[{self.kind}] {endpoint} → HTTP {status}: {body[:300]}")


def error_kind(status: int) -> str:
    """One word the log, the seller message and the eval report can group by."""
    if status == 0:
        return "network"
    if status in (401, 403):
        return "auth"
    if status in (402, 429):
        return "quota"
    if status >= 500:
        return "server"
    return "request"


@dataclass
class Segment:
    object_id: str
    box: tuple[int, int, int, int]   # left, top, right (exclusive), bottom (exclusive), source pixels
    mask: np.ndarray                 # bool, full frame (height, width)
    confidence: float | None

    @property
    def area(self) -> int:
        return int(self.mask.sum())


def data_url(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")


def wav_seconds(wav: bytes) -> float:
    try:
        with wave.open(io.BytesIO(wav)) as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError):
        return 0.0


def transcript_text(body: dict) -> str:
    """The transcript from a /asr/transcribe JSON body.

    The reference documents the realtime events (`speechComplete.transcript`) but not the buffered
    JSON body, so this accepts the plausible shapes and the raw body goes to the log for checking.
    """
    for key in ("transcript", "text"):
        if isinstance(body.get(key), str):
            return body[key].strip()
    for key in ("turns", "results", "segments", "transcripts"):
        items = body.get(key)
        if isinstance(items, list):
            parts = [str(i.get("transcript") or i.get("text") or "").strip() for i in items if isinstance(i, dict)]
            if any(parts):
                return " ".join(p for p in parts if p)
    raise ValueError(f"no transcript in response keys {sorted(body)}")


def segments_from_text(output_text: str, width: int, height: int) -> list[Segment]:
    """Decode SAM 3.1 output for one image into full-frame boolean masks, keyed by object id."""
    parser = image_segmentation_format().create_parser()
    parser.push(output_text if output_text.endswith("\n") or not output_text else output_text + "\n")
    result = parser.finish(CompletedOutcome()).result
    boxes, segments = {}, []
    for record in result.records:
        if record.kind == "box":
            boxes[record.object_id] = record
    for record in result.records:
        if record.kind != "mask":
            continue
        raster = np.frombuffer(decode_mask_to_raster(record.mask), dtype=np.uint8)
        raster = raster.reshape(record.mask.height, record.mask.width).astype(bool)
        left, top = int(record.bounds.left), int(record.bounds.top)
        right, bottom = int(record.bounds.right), int(record.bounds.bottom)
        if (right - left, bottom - top) != (record.mask.width, record.mask.height):
            scaled = Image.fromarray(raster.astype(np.uint8) * 255).resize((right - left, bottom - top))
            raster = np.asarray(scaled) > 127
        full = np.zeros((height, width), dtype=bool)
        r0, c0 = max(top, 0), max(left, 0)
        r1, c1 = min(bottom, height), min(right, width)
        full[r0:r1, c0:c1] = raster[r0 - top:r1 - top, c0 - left:c1 - left]
        box = boxes.get(record.object_id)
        segments.append(Segment(record.object_id, (left, top, right, bottom), full,
                                record.confidence if record.confidence is not None
                                else (box.confidence if box else None)))
    return segments


class MetaClient:
    mock = False

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings
        self._http = httpx.AsyncClient(base_url=settings.base_url, timeout=settings.timeout_s,
                                       transport=transport,
                                       headers={"Authorization": f"Bearer {settings.api_key}"})

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, path: str, **kwargs) -> dict:
        for attempt in range(2):
            try:
                response = await self._http.post(path, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == 0:          # one retry on a dropped connection or a timeout
                    await asyncio.sleep(1.0 + random.random())
                    continue
                raise MetaAPIError(path, 0, f"{type(exc).__name__}: {exc}") from exc
            if response.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                await asyncio.sleep(1.0 + random.random())      # jitter: parallel calls do not retry in lockstep
                continue
            if response.status_code >= 400:
                raise MetaAPIError(path, response.status_code, response.text)
            return response.json()
        raise AssertionError("unreachable")

    async def chat_json(self, system: str, text: str, images: list[bytes], name: str, schema: dict) -> tuple[dict, dict]:
        """Spark 1.3; if it is still down after the retry (network / quota / server), the pinned fallback model
        (LISTING_FALLBACK_TEXT_MODEL, default muse-spark-1.2, "" to disable) answers and the result says so."""
        try:
            return await self._chat_json(self.s.text_model, system, text, images, name, schema)
        except MetaAPIError as exc:
            fallback = os.environ.get("LISTING_FALLBACK_TEXT_MODEL", "muse-spark-1.2")
            if exc.kind not in ("network", "quota", "server") or not fallback or fallback == self.s.text_model:
                raise
            out, meta = await self._chat_json(fallback, system, text, images, name, schema)
            return out, {**meta, "fallback_from": self.s.text_model, "fallback_reason": exc.kind}

    async def _chat_json(self, model: str, system: str, text: str, images: list[bytes], name: str,
                         schema: dict) -> tuple[dict, dict]:
        content = [{"type": "text", "text": text}]
        content += [{"type": "image_url", "image_url": {"url": data_url(img)}} for img in images]
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": schema}},
        }
        if self.s.reasoning_effort:
            payload["reasoning_effort"] = self.s.reasoning_effort
        body = await self._post("/chat/completions", json=payload)
        message = body["choices"][0]["message"]["content"]
        if isinstance(message, list):
            message = "".join(part.get("text", "") for part in message if isinstance(part, dict))
        usage = body.get("usage") or {}
        meta = {"endpoint": "POST /v1/chat/completions", "model": model,
                "usage": {**usage, "reasoning_effort": self.s.reasoning_effort or "default"},
                "cost_usd": prices.text_cost(model, usage)}
        return json.loads(message), meta

    async def transcribe(self, wav: bytes) -> tuple[str, dict]:
        async def call(bias: str) -> dict:
            request = {"model": self.s.asr_model, "audioEncoding": "WAV", "mode": "PUSH_TO_TALK"}
            if bias:
                request["languageBias"] = [bias]
            files = {"request": (None, json.dumps(request), "application/json"),
                     "audio": ("speech.wav", wav, "audio/wav")}
            return await self._post("/asr/transcribe", files=files, headers={"Accept": "application/json"})

        bias = self.s.language_bias
        try:
            body = await call(bias)
        except MetaAPIError as exc:
            # languageBias is documented for the realtime handshake; if the file endpoint rejects it, go without.
            if exc.status != 400 or not bias:
                raise
            bias = ""
            body = await call(bias)
        seconds = wav_seconds(wav)
        meta = {"endpoint": "POST /v1/asr/transcribe", "model": self.s.asr_model,
                "usage": {"audio_seconds": round(seconds, 2), "language_bias": bias or "auto"},
                "cost_usd": prices.asr_cost(seconds),
                "raw": json.dumps(body, ensure_ascii=False)[:600]}
        return transcript_text(body), meta

    async def segment(self, jpeg: bytes, phrase: str) -> tuple[list[Segment], dict]:
        payload = {
            "model": self.s.sam_model,
            "input": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": phrase},
                {"type": "input_image", "image_url": data_url(jpeg)}]}],
            "stream": True,
        }
        text = ""
        async with self._http.stream("POST", "/responses", json=payload,
                                     headers={"Accept": "text/event-stream"}) as response:
            if response.status_code >= 400:
                raise MetaAPIError("/responses", response.status_code, (await response.aread()).decode("utf-8", "replace"))
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[len("data: "):]
                if data == "[DONE]":
                    break
                event = json.loads(data)
                kind = event.get("type")
                if kind == "response.output_text.delta":
                    text += event.get("delta") or ""
                elif kind in ("response.failed", "error"):
                    raise MetaAPIError("/responses", 200, json.dumps(event)[:500])
        width, height = Image.open(io.BytesIO(jpeg)).size
        meta = {"endpoint": "POST /v1/responses (stream)", "model": self.s.sam_model,
                "usage": {"images": 1}, "cost_usd": prices.SAM_PER_IMAGE}
        return segments_from_text(text, width, height), meta

    async def generate_image(self, prompt: str, size: str = "1024x1024") -> tuple[bytes, dict]:
        payload = {"model": self.s.image_model, "prompt": prompt, "size": size,
                   "output_format": "png", "response_format": "b64_json"}
        body = await self._post("/images/generations", json=payload)
        image = base64.b64decode(body["data"][0]["b64_json"])
        meta = {"endpoint": "POST /v1/images/generations", "model": self.s.image_model,
                "usage": {"images": 1}, "cost_usd": prices.IMAGE_PER_IMAGE}
        return image, meta

    async def images_edit(self, prompt: str, images: list[bytes], size: str = "1080x1920") -> tuple[bytes, dict]:
        """Muse Image with reference images (studio first frames): POST /images/edits, one PNG back.

        Body: model, n=1, size, output_format png, prompt, images as `{"image_url": data_url}`. Shape read
        from dev.meta.ai on 1/11/2026; not yet exercised against the live API.
        """
        payload = {"model": self.s.image_model, "n": 1, "size": size, "output_format": "png", "prompt": prompt,
                   "images": [{"image_url": data_url(img)} for img in images]}
        body = await self._post("/images/edits", json=payload)
        image = base64.b64decode(body["data"][0]["b64_json"])
        meta = {"endpoint": "POST /v1/images/edits", "model": self.s.image_model,
                "usage": {"images": 1, "references": len(images)}, "cost_usd": prices.IMAGE_PER_IMAGE}
        return image, meta

    async def responses_file(self, path: str, prompt: str, schema: dict, mime: str) -> tuple[dict, dict]:
        """Muse Spark over a file (studio QC and judge on a video): upload, ask with a JSON schema, delete.

        POST /files (purpose user_data) → POST /responses with `input_file` + `text.format` json_schema →
        DELETE /files/{id}, even when the question fails. Cost from the response's `usage`; the upload
        itself is not priced. Not yet exercised against the live API.
        """
        name = Path(path).name
        with open(path, "rb") as handle:
            uploaded = await self._post("/files", files={"purpose": (None, "user_data"),
                                                         "file": (name, handle, mime)})
        file_id = uploaded["id"]
        try:
            payload = {
                "model": self.s.text_model,
                "input": [{"role": "user", "content": [{"type": "input_file", "file_id": file_id},
                                                       {"type": "input_text", "text": prompt}]}],
                "text": {"format": {"type": "json_schema", "name": schema.get("title", "Answer"), "schema": schema}},
            }
            if self.s.reasoning_effort:
                payload["reasoning"] = {"effort": self.s.reasoning_effort}
            body = await self._post("/responses", json=payload)
        finally:
            await self._http.delete(f"/files/{file_id}")       # best effort: a leftover file is not an error
        text = body.get("output_text")
        if not text:
            parts = [c.get("text", "") for item in body.get("output", []) if item.get("type") == "message"
                     for c in item.get("content", []) if c.get("type") == "output_text"]
            text = "".join(parts)
        usage = body.get("usage") or {}
        if "input_tokens" in usage:                              # Responses API names; price them as Chat's
            usage = {**usage, "prompt_tokens": usage.get("input_tokens"), "completion_tokens": usage.get("output_tokens")}
        meta = {"endpoint": "POST /v1/files + /v1/responses + DELETE /v1/files/{id}", "model": self.s.text_model,
                "usage": {**usage, "file": name, "file_bytes": Path(path).stat().st_size},
                "cost_usd": prices.text_cost(self.s.text_model, usage)}
        return json.loads(text), meta


# --------------------------------------------------------------------------------------------
# Mock: same interface, deterministic, no network. Used when MODEL_API_KEY is not set, and in tests.

MOCK_UTTERANCE = "Áo thun cotton nam, size M, L, XL, ba màu trắng đen xám, còn 50 cái."
_PRICE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(k|nghìn|ngàn|ng|triệu|tr)\b|(\d{4,})\s*(?:đ|đồng|vnd)", re.I)
_STOCK = re.compile(r"(?:còn|có|số lượng)\s*(\d+)", re.I)


def mock_quote(text: str, pattern: str) -> str:
    m = re.search(pattern, text, re.I)
    return m.group(0) if m else ""


def mock_price(text: str) -> int | None:
    m = _PRICE.search(text)
    if not m:
        return None
    if m.group(3):
        return int(m.group(3))
    value = float(m.group(1).replace(",", "."))
    return int(value * (1_000_000 if m.group(2).lower() in ("triệu", "tr") else 1000))


def mock_stock(text: str) -> int | None:
    m = _STOCK.search(text)
    return int(m.group(1)) if m else None


class MockMetaClient:
    mock = True

    def __init__(self, settings: Settings):
        self.s = settings

    async def aclose(self) -> None:
        pass

    def _meta(self, endpoint: str, model: str, usage: dict) -> dict:
        return {"endpoint": endpoint, "model": model, "usage": usage, "cost_usd": 0.0}

    async def chat_json(self, system: str, text: str, images: list[bytes], name: str, schema: dict) -> tuple[dict, dict]:
        await asyncio.sleep(0.05)
        meta = self._meta("POST /v1/chat/completions", self.s.text_model, {"images": len(images)})
        if name == "Listing":
            note = (text.split("<<<", 1)[-1].split(">>>", 1)[0] if "<<<" in text
                    else text.split("Seller said:", 1)[-1]).strip()
            price, stock = mock_price(note), mock_stock(note)
            first = re.split(r"[,.]", note)[0].strip() or "Sản phẩm"
            listing = {
                "title": first[:1].upper() + first[1:],
                "description": f"{first[:1].upper() + first[1:]}. Hàng có sẵn, giao nhanh trong ngày.",
                "category_id": "fashion_men" if re.search(r"\báo\b|quần", note, re.I) else "other",
                "attributes": [], "variants": [], "price_vnd": price, "stock": stock, "unit": "cái",
                "missing": [k for k, v in (("price_vnd", price), ("stock", stock)) if v is None],
                "product_phrase_en": "product",
                "background_prompt_en": "A light grey seamless studio backdrop with a white tabletop.",
                "evidence": {"price_quote": mock_quote(note, r"(?:giá\s*)?\d+\s*(?:nghìn|ngàn|k)\b") if price else "",
                             "stock_quote": mock_quote(note, r"còn\s*\d+\s*\w+") if stock is not None else ""},
            }
            return listing, meta
        if name == "Verification":
            return {"verdict": "pass", "prohibited": False, "prohibited_reason": "",
                    "checks": [{"claim": "item type", "status": "consistent", "note": "mock"}],
                    "summary_vi": "Mô tả khớp với ảnh (mock).", "instructions_found": "", "rule_ids": []}, meta
        if name == "SaleFacts":
            answer = text.split("Answer:", 1)[-1]
            return {"price_vnd": mock_price(answer), "stock": mock_stock(answer)}, meta
        raise ValueError(f"mock has no answer for schema {name}")

    async def transcribe(self, wav: bytes) -> tuple[str, dict]:
        await asyncio.sleep(0.05)
        seconds = wav_seconds(wav)
        return MOCK_UTTERANCE, self._meta("POST /v1/asr/transcribe", self.s.asr_model,
                                          {"audio_seconds": round(seconds, 2)})

    async def segment(self, jpeg: bytes, phrase: str) -> tuple[list[Segment], dict]:
        """Foreground = pixels far from the median border colour. Good enough to exercise the pipeline."""
        await asyncio.sleep(0.05)
        rgb = np.asarray(Image.open(io.BytesIO(jpeg)).convert("RGB")).astype(np.int16)
        h, w, _ = rgb.shape
        border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
        distance = np.abs(rgb - np.median(border, axis=0)).sum(axis=2)
        mask = distance > 60
        meta = self._meta("POST /v1/responses (stream)", self.s.sam_model, {"images": 1})
        if not mask.any():
            return [], meta
        rows, cols = np.where(mask)
        box = (int(cols.min()), int(rows.min()), int(cols.max()) + 1, int(rows.max()) + 1)
        return [Segment("0", box, mask, 0.9)], meta

    async def generate_image(self, prompt: str, size: str = "1024x1024") -> tuple[bytes, dict]:
        await asyncio.sleep(0.05)
        width, height = (int(v) for v in size.split("x"))
        image = Image.new("RGB", (width, height), (236, 238, 241))
        draw = ImageDraw.Draw(image)
        for y in range(height // 2, height):
            shade = 236 - int((y - height // 2) / (height / 2) * 30)
            draw.line([(0, y), (width, y)], fill=(shade, shade, shade + 3))
        buffer = io.BytesIO()
        image.filter(ImageFilter.GaussianBlur(2)).save(buffer, "PNG")
        return buffer.getvalue(), self._meta("POST /v1/images/generations", self.s.image_model, {"images": 1})

    async def images_edit(self, prompt: str, images: list[bytes], size: str = "1080x1920") -> tuple[bytes, dict]:
        """The first reference, scaled to fit, on a plain backdrop; a flat backdrop when there is none."""
        await asyncio.sleep(0.05)
        width, height = (int(v) for v in size.split("x"))
        canvas = Image.new("RGB", (width, height), (230, 232, 236))
        if images:
            reference = Image.open(io.BytesIO(images[0])).convert("RGB")
            reference.thumbnail((width // 2, height // 2))
            canvas.paste(reference, ((width - reference.width) // 2, (height - reference.height) // 2))
        buffer = io.BytesIO()
        canvas.save(buffer, "PNG", optimize=True)
        return buffer.getvalue(), self._meta("POST /v1/images/edits", self.s.image_model,
                                             {"images": 1, "references": len(images)})

    async def responses_file(self, path: str, prompt: str, schema: dict, mime: str) -> tuple[dict, dict]:
        """No answer in the mock: the studio services build their own sample verdicts."""
        await asyncio.sleep(0.05)
        size = Path(path).stat().st_size if Path(path).is_file() else 0
        return {}, self._meta("POST /v1/files + /v1/responses + DELETE /v1/files/{id}", self.s.text_model,
                              {"file": Path(path).name, "file_bytes": size, "mime": mime})


def make_client(settings: Settings) -> MetaClient | MockMetaClient:
    return MockMetaClient(settings) if settings.mock else MetaClient(settings)
