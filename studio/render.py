"""render: first frame + motion prompt → a silent 5-8 s clip, through a `Renderer` plug-in.

The render engine is outside Meta. `KlingMCPRenderer` talks to Kling's MCP server (skeleton: the tool
names and the JSON-RPC shape, nothing verified against the live server yet). `MockRenderer` makes a
Ken Burns clip from the first frame with ffmpeg, so the chain runs end to end without credits.
"""
from __future__ import annotations

import asyncio
import base64
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
    """Kling image-to-video over its MCP server (streamable HTTP, JSON-RPC 2.0).

    Skeleton only. Tools used: `file_upload` (the first frame), `image_to_video` (frame + motion prompt +
    duration), `query_tasks` (status, result URL). The token comes from `$KLING_MCP_TOKEN` and nothing
    else; it is sent in the Authorization header and never logged. `CREDIT_FLOOR`: no job is submitted
    when the account's credits are at or below `$KLING_CREDIT_FLOOR`.
    TODO(live): initialize handshake, Mcp-Session-Id, exact argument names of the three tools.
    """

    name = "kling"

    def __init__(self, token: str | None = None, url: str = KLING_MCP_URL, credit_floor: int | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.token = token if token is not None else os.environ.get("KLING_MCP_TOKEN", "")
        if not self.token:
            raise RuntimeError("KLING_MCP_TOKEN is not set")
        self.url = url
        self.credit_floor = credit_floor if credit_floor is not None else int(os.environ.get("KLING_CREDIT_FLOOR", "0"))
        self._http = httpx.AsyncClient(timeout=60.0, transport=transport, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/json, text/event-stream"})
        self._next_id = 0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, tool: str, arguments: dict) -> dict:
        """One `tools/call`. Streamable HTTP may answer with JSON or with an SSE body; both are read."""
        self._next_id += 1
        body = {"jsonrpc": "2.0", "id": self._next_id, "method": "tools/call",
                "params": {"name": tool, "arguments": arguments}}
        response = await self._http.post(self.url, json=body)
        if response.status_code >= 400:
            raise RuntimeError(f"kling mcp {tool} → HTTP {response.status_code}: {response.text[:300]}")
        text = response.text
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            data = [line[6:] for line in text.splitlines() if line.startswith("data: ")]
            text = data[-1] if data else "{}"
        message = json.loads(text)
        if "error" in message:
            raise RuntimeError(f"kling mcp {tool}: {message['error']}")
        result = message.get("result", {})
        for part in result.get("content", []):          # MCP tool results carry JSON as text parts
            if part.get("type") == "text":
                try:
                    return json.loads(part["text"])
                except ValueError:
                    return {"text": part["text"]}
        return result

    async def submit(self, frame_png: bytes, prompt: str, seconds: float) -> str:
        uploaded = await self._call("file_upload", {"file_name": "frame.png", "file_type": "image/png",
                                                    "file_data": base64.b64encode(frame_png).decode("ascii")})
        job = await self._call("image_to_video", {"image": uploaded.get("url") or uploaded.get("file_id"),
                                                  "prompt": prompt, "duration": str(int(round(seconds))),
                                                  "aspect_ratio": "9:16", "model": "kling-v3"})
        return str(job.get("task_id") or job.get("id"))

    async def poll(self, job_id: str) -> dict:
        task = await self._call("query_tasks", {"task_id": job_id})
        status = str(task.get("task_status") or task.get("status") or "running").lower()
        mapped = {"submitted": "queued", "processing": "running", "succeed": "done", "succeeded": "done",
                  "failed": "failed"}.get(status, "running")
        videos = ((task.get("task_result") or {}).get("videos")) or []
        return {"status": mapped, "url": videos[0].get("url") if videos else None,
                "credits": task.get("credits"), "error": task.get("task_status_msg", "")}


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
