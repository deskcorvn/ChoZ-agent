"""HTTP API and the demo UI (a Zalo Mini App look-alike plus the agent log panel)."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import config
from app.catalog import Catalog
from app.config import ROOT, Settings
from app.meta_api import make_client
from app.pipeline import Agent

MAX_PHOTO_BYTES = 15 * 1024 * 1024
MAX_AUDIO_BYTES = 10 * 1024 * 1024


def create_app(settings: Settings | None = None, client=None) -> FastAPI:
    settings = settings or config.load()
    client = client or make_client(settings)
    runs_dir = settings.data_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
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

    @app.post("/api/runs")
    async def create_run(photos: list[UploadFile] = File(...), audio: UploadFile | None = File(None),
                         note: str = Form("")):
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

    app.mount("/files/runs", StaticFiles(directory=runs_dir), name="run-files")
    catalog.images.mkdir(parents=True, exist_ok=True)
    app.mount("/files/catalog", StaticFiles(directory=catalog.images), name="catalog-files")
    app.mount("/web", StaticFiles(directory=ROOT / "web"), name="web")

    @app.get("/")
    def index():
        return FileResponse(ROOT / "web" / "index.html")

    return app
