"""render: first frame + motion prompt → a silent 5-8 s clip, through a `Renderer` plug-in.

The render engine is outside Meta. `KlingMCPRenderer` talks to Kling's official MCP server in the dialect
used live on 1 Nov 2026 (handshake, upload ticket, image_to_video with name/value arguments, query_tasks). `MockRenderer` makes a
Ken Burns clip from the first frame with ffmpeg, so the chain runs end to end without credits.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Protocol

import httpx

from app.config import Settings
from studio import log, work_dir
from studio.models import RenderInput, RenderJob

INPUT, OUTPUT, ROLE = RenderInput, RenderJob, "renderer"

KLING_MCP_URL = "https://kling.ai/mcp"
POLL_SECONDS = 5.0
POLL_TIMEOUT_S = 600.0


class Renderer(Protocol):
    name: str

    async def submit(self, frame_png: bytes, prompt: str, seconds: float) -> str:
        """Start a job; return its id."""

    async def poll(self, job_id: str) -> dict:
        """{"status": queued|running|done|failed, "url": ..., "path": ..., "credits": ..., "error": ...}"""


def ffmpeg_path() -> str | None:
    """ffmpeg from `$FFMPEG` (a file or a folder) or the PATH; None when there is none."""
    hint = os.environ.get("FFMPEG", "").strip()
    if hint:
        path = Path(hint)
        if path.is_dir():
            path = path / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if path.is_file():
            return str(path)
    return shutil.which("ffmpeg")


class MockRenderer:
    """Ken Burns: a slow push-in on the first frame, 5 s, 1080×1920, no credits, no network."""

    name = "mock"

    def __init__(self, out_dir: Path, ffmpeg: str | None = None):
        self.out_dir, self.ffmpeg = out_dir, ffmpeg or ffmpeg_path()
        self.jobs: dict[str, dict] = {}

    async def submit(self, frame_png: bytes, prompt: str, seconds: float) -> str:
        job_id = uuid.uuid4().hex[:12]
        frame = self.out_dir / f"{job_id}.png"
        clip = self.out_dir / f"{job_id}.mp4"
        frame.write_bytes(frame_png)
        if not self.ffmpeg:
            self.jobs[job_id] = {"status": "failed", "error": "ffmpeg not found (PATH or $FFMPEG)"}
            return job_id
        frames = int(seconds * 25)
        zoom = "zoompan=z='min(zoom+0.0008,1.2)':d=%d:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920" % frames
        command = [self.ffmpeg, "-y", "-loglevel", "error", "-loop", "1", "-i", str(frame), "-vf",
                   f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,{zoom},format=yuv420p",
                   "-t", str(seconds), "-r", "25", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", str(clip)]
        result = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True)
        if result.returncode != 0:
            self.jobs[job_id] = {"status": "failed", "error": result.stderr[-300:]}
        else:
            self.jobs[job_id] = {"status": "done", "path": str(clip), "credits": 0}
        return job_id

    async def poll(self, job_id: str) -> dict:
        return self.jobs.get(job_id, {"status": "failed", "error": "unknown job"})


class KlingMCPRenderer:
    """Kling image-to-video over its official MCP server (streamable HTTP, JSON-RPC 2.0).

    Dialect as used against the live server on 1 Nov 2026 (the tests use a mock transport):
    `initialize` and `notifications/initialized` once per client; the server's `Mcp-Session-Id` is carried
    on every later call. `file_upload` returns a ticket; the bytes go in a multipart POST to its `uploadUrl`
    (fields `ticket`, `file`) and the reply carries the public `url`. `image_to_video` takes `model`,
    `arguments` (name/value pairs, all strings) and `inputs` (`first_image`). `query_tasks` takes
    `generationId` and, once `status` is completed, lists `works` with the clip URL (kept 24 h).
    The token comes from `$KLING_MCP_TOKEN` only and is never logged. No job is submitted when
    `query_membership_and_credits` reports a balance at or below `$KLING_CREDIT_FLOOR`.
    """

    name = "kling"
    model = "kling-video-v3_0"
    done_states = ("completed", "succeed", "succeeded", "success", "finished", "done")
    failed_states = ("failed", "fail", "error", "cancelled", "canceled")

    def __init__(self, token: str | None = None, url: str = KLING_MCP_URL, credit_floor: int | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, element_id: str | None = None):
        self.token = token if token is not None else os.environ.get("KLING_MCP_TOKEN", "")
        if not self.token:
            raise RuntimeError("KLING_MCP_TOKEN is not set")
        self.url = url
        self.credit_floor = credit_floor if credit_floor is not None else int(os.environ.get("KLING_CREDIT_FLOOR", "0"))
        self.element_id = element_id or os.environ.get("KLING_ELEMENT_ID")   # a saved character Element, optional
        self._http = httpx.AsyncClient(timeout=120.0, transport=transport, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/json, text/event-stream"})
        self._next_id = 0
        self._session: str | None = None
        self._ready = False

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, body: dict) -> dict | None:
        headers = {"Mcp-Session-Id": self._session} if self._session else {}
        response = await self._http.post(self.url, json=body, headers=headers)
        if response.status_code >= 400:
            raise RuntimeError(f"kling mcp HTTP {response.status_code}: {response.text[:300]}")
        self._session = response.headers.get("Mcp-Session-Id", self._session)
        if "id" not in body:
            return None
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            for line in response.text.splitlines():
                if line.startswith("data:"):
                    message = json.loads(line[5:].strip())
                    if message.get("id") == body["id"]:
                        return message
            raise RuntimeError("kling mcp: no response in the event stream")
        return response.json()

    async def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        message = await self._post({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}})
        if message and "error" in message:
            raise RuntimeError(f"kling mcp {method}: {message['error']}")
        return (message or {}).get("result", {})

    async def _call(self, tool: str, arguments: dict) -> dict:
        """One `tools/call` after the handshake; MCP tool results carry their JSON as a text part."""
        if not self._ready:
            await self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "choz-studio", "version": "0.1"}})
            await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self._ready = True
        result = await self._rpc("tools/call", {"name": tool, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError(f"kling mcp {tool}: {result}")
        for part in result.get("content", []):
            if part.get("type") == "text":
                try:
                    return json.loads(part["text"])
                except ValueError:
                    return {"text": part["text"]}
        return result

    async def credits(self) -> float:
        return float((await self._call("query_membership_and_credits", {})).get("availableRemainCredits", 0.0))

    async def upload(self, png: bytes, filename: str = "frame.png") -> str:
        ticket = await self._call("file_upload", {"filename": filename, "contentType": "image/png", "size": len(png)})
        response = await self._http.post(ticket["uploadUrl"], data={"ticket": ticket["ticket"]},
                                         files={"file": (filename, png, "image/png")})
        if response.status_code >= 400:
            raise RuntimeError(f"kling upload HTTP {response.status_code}: {response.text[:200]}")
        body = response.json()
        url = body.get("url") or (body.get("data") or {}).get("url")
        if not url:
            raise RuntimeError(f"kling upload: no url in {body}")
        return url

    async def submit(self, frame_png: bytes, prompt: str, seconds: float) -> str:
        balance = await self.credits()
        if balance <= self.credit_floor:
            raise RuntimeError(f"kling credits {balance:.0f} at or below the floor {self.credit_floor}; no job sent")
        url = await self.upload(frame_png)
        arguments = [{"name": "prompt", "value": prompt}, {"name": "duration", "value": str(int(round(seconds)))},
                     {"name": "resolution", "value": "1080p"}, {"name": "imageCount", "value": "1"},
                     {"name": "prefer_multi_shots", "value": "false"}, {"name": "enable_audio", "value": "false"}]
        if self.element_id:
            arguments.append({"name": "elements", "value": json.dumps([{"id": str(self.element_id), "bindName": "Lan"}])})
        job = await self._call("image_to_video", {
            "model": self.model, "arguments": arguments, "inputs": [{"inputType": "URL", "name": "first_image", "url": url}],
            "rationale": "Studio: one-action silent clip from an approved first frame, cut to a pre-recorded voice-over."})
        job_id = job.get("generationId") or job.get("generation_id") or job.get("id")
        if not job_id:
            raise RuntimeError(f"kling image_to_video: no generationId in {job}")
        return str(job_id)

    async def poll(self, job_id: str) -> dict:
        task = await self._call("query_tasks", {"generationId": job_id})
        status = str(task.get("status") or task.get("taskStatus") or "running").lower()
        if status in self.done_states:
            mapped = "done"
        elif status in self.failed_states:
            mapped = "failed"
        else:
            mapped = "queued" if status in ("submitted", "pending", "queued") else "running"
        url = None
        for work in task.get("works") or task.get("results") or []:
            if isinstance(work, dict):
                url = work.get("urlWithoutWatermark") or work.get("url") or work.get("videoUrl")
                if url:
                    break
        return {"status": mapped, "url": url, "credits": task.get("creditsConsumed"), "error": task.get("message", "")}


def make_renderer(settings: Settings, out_dir: Path, engine: str | None = None) -> Renderer:
    engine = (engine or os.environ.get("STUDIO_RENDERER") or ("mock" if settings.mock else "kling")).lower()
    if engine == "mock":
        return MockRenderer(out_dir)
    if engine == "kling":
        return KlingMCPRenderer()
    raise ValueError(f"unknown render engine {engine!r}; mock or kling")


async def run(client, settings: Settings, inp: RenderInput, renderer: Renderer | None = None) -> RenderJob:
    folder = work_dir(settings, inp.work_dir, "render", inp.shot.id)
    renderer = renderer or make_renderer(settings, folder, inp.engine)
    describe = f"shot {inp.shot.id}, {inp.shot.seconds:.0f} s, engine {renderer.name}"
    async with log.step("render", f"Render {inp.shot.id} (plug-in: {renderer.name})", ROLE, describe,
                        mock=renderer.name == "mock") as step:
        step.model, step.endpoint = f"{renderer.name} i2v", KLING_MCP_URL if renderer.name == "kling" else "ffmpeg"
        job_id = await renderer.submit(Path(inp.frame_path).read_bytes(), inp.shot.motion_prompt, inp.shot.seconds)
        deadline = asyncio.get_running_loop().time() + POLL_TIMEOUT_S
        while True:
            state = await renderer.poll(job_id)
            if state["status"] in ("done", "failed") or asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(POLL_SECONDS)
        clip_path = state.get("path")
        if state["status"] == "done" and not clip_path and state.get("url"):
            clip_path = str(folder / f"{job_id}.mp4")     # TODO(live): download state["url"] to clip_path
        job = RenderJob(shot_id=inp.shot.id, engine=renderer.name, job_id=job_id, status=state["status"],
                        seconds=inp.shot.seconds, clip_path=clip_path, url=state.get("url"),
                        credits=state.get("credits"), error=str(state.get("error") or ""))
        step.status = "done" if job.status == "done" else "failed"
        step.error = job.error
        step.decision = f"{job.status}, credits {job.credits if job.credits is not None else '?'}"
        step.output["job"] = job.model_dump()
        return job
