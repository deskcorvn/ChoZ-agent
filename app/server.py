"""HTTP API and the demo UI (a Zalo Mini App look-alike plus the agent log panel)."""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app import config, metrics
from app.catalog import Catalog
from app.config import ROOT, Settings
from app.meta_api import make_client
from app.pipeline import Agent
from studio import SERVICES, PromptMissing, run_service
from studio import log as studio_log

MAX_PHOTO_BYTES = 15 * 1024 * 1024
MAX_AUDIO_BYTES = 10 * 1024 * 1024


def purge_old_runs(runs_dir: Path, days: float) -> int:
    """Retention: run folders (photos, transcripts, logs) older than `days` are deleted at startup.
    Published catalog items keep their own copy of the studio photo. `days <= 0` keeps everything."""
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for folder in runs_dir.iterdir():
        if folder.is_dir() and folder.stat().st_mtime < cutoff:
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
    return removed


class RevalidatedFiles(StaticFiles):
    """The UI's own files: always ask the server (cheap with ETag), so an update is never hidden by
    the browser cache. Found 31/10/2026: a reload kept running the previous app.js."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(settings: Settings | None = None, client=None) -> FastAPI:
    settings = settings or config.load()
    client = client or make_client(settings)
    runs_dir = settings.data_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    purge_old_runs(runs_dir, float(os.environ.get("LISTING_RETENTION_DAYS", "7")))
    agent = Agent(client, runs_dir)
    catalog = Catalog(settings.data_dir)
    tasks: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await client.aclose()

    app = FastAPI(title="Chợ Z listing agent", lifespan=lifespan)
    app.state.agent, app.state.catalog, app.state.tasks = agent, catalog, tasks

    def get_run(run_id: str):
        run = agent.runs.get(run_id)
        if run is None:
            raise HTTPException(404, "unknown run")
        return run

    async def read_limited(upload: UploadFile | None, limit: int) -> bytes | None:
        if upload is None:
            return None
        data = await upload.read()
        if len(data) > limit:
            raise HTTPException(413, f"{upload.filename} is larger than {limit // (1024 * 1024)} MB")
        return data or None

    @app.get("/api/health")
    def health():
        return {"mode": "mock" if client.mock else "live",
                "models": {"text": settings.text_model, "image": settings.image_model,
                           "segmentation": settings.sam_model, "speech": settings.asr_model}}

    recent: list[float] = []      # start times of recent runs, for the rate limit

    def spend_guard() -> None:
        """Refuse new runs over the daily budget or the per-minute rate, before any paid call is made."""
        if client.mock:
            return
        cap = float(os.environ.get("DAILY_COST_CAP_USD", "5"))
        today = time.strftime("%Y-%m-%d")
        spent = next((d["cost_usd"] for d in metrics.collect(runs_dir, 1)["per_day"] if d["day"] == today), 0.0)
        if cap > 0 and spent >= cap:
            raise HTTPException(429, f"over today's budget: ${spent:.2f} of ${cap:.2f} (DAILY_COST_CAP_USD)")
        now = time.time()
        recent[:] = [t for t in recent if now - t < 60]
        per_minute = int(os.environ.get("RUNS_PER_MINUTE", "12"))
        if per_minute > 0 and len(recent) >= per_minute:
            raise HTTPException(429, f"too many listings this minute (RUNS_PER_MINUTE={per_minute}); try again shortly")
        recent.append(now)

    @app.post("/api/runs")
    async def create_run(photos: list[UploadFile] = File(...), audio: UploadFile | None = File(None),
                         note: str = Form("")):
        spend_guard()
        images = [await read_limited(p, MAX_PHOTO_BYTES) for p in photos]
        try:
            run = agent.create([i for i in images if i], await read_limited(audio, MAX_AUDIO_BYTES), note)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        task = asyncio.create_task(agent.process(run))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return run.public()

    @app.get("/api/runs/{run_id}")
    def read_run(run_id: str):
        return get_run(run_id).public()

    @app.post("/api/runs/{run_id}/answer")
    async def answer(run_id: str, audio: UploadFile | None = File(None), text: str = Form("")):
        run = get_run(run_id)
        try:
            await agent.answer(run, await read_limited(audio, MAX_AUDIO_BYTES), text)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return run.public()

    @app.post("/api/runs/{run_id}/restyle")
    async def restyle(run_id: str):
        run = get_run(run_id)
        try:
            await agent.restyle(run)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return run.public()

    @app.post("/api/runs/{run_id}/publish")
    def publish(run_id: str, edits: dict | None = None):
        run = get_run(run_id)
        try:
            item = catalog.publish(run, runs_dir / run.id, edits or {})
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return item

    @app.get("/api/catalog")
    def list_catalog():
        return catalog.items()

    # ---- studio (preview): one route per service, same client, JSON in and out -------------------

    @app.post("/studio/{service}")
    async def studio_service(service: str, payload: dict):
        if service not in SERVICES:
            raise HTTPException(404, f"unknown studio service; one of {', '.join(SERVICES)}")
        try:
            return await run_service(service, client, settings, payload)
        except ValidationError as exc:
            raise HTTPException(422, exc.errors()[:3]) from exc
        except PromptMissing as exc:
            raise HTTPException(501, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/studio/log")
    def studio_log_tail():
        return studio_log.tail(200)

    app.mount("/files/runs", StaticFiles(directory=runs_dir), name="run-files")
    catalog.images.mkdir(parents=True, exist_ok=True)
    app.mount("/files/catalog", StaticFiles(directory=catalog.images), name="catalog-files")
    app.mount("/web", RevalidatedFiles(directory=ROOT / "web"), name="web")

    @app.get("/")
    def index():
        return FileResponse(ROOT / "web" / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/metrics")
    def api_metrics(days: int = 7):
        return metrics.collect(runs_dir, days)

    @app.get("/metrics")
    def metrics_page():
        return HTMLResponse(metrics.PAGE)

    return app
