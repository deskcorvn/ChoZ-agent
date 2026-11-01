"""HTTP API and the demo UI (a Zalo Mini App look-alike plus the agent log panel)."""
from __future__ import annotations

import asyncio
import hmac
import io
import json
import os
import re
import shutil
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app import audit, config, metrics
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

    # Optional access token: unset = open (localhost demo); set LISTING_ACCESS_TOKEN before exposing the server.
    # Scripts send "Authorization: Bearer <token>"; the browser signs in once at /login and gets an HttpOnly cookie.
    token = os.environ.get("LISTING_ACCESS_TOKEN", "")
    open_paths = ("/login", "/api/health")

    def token_ok(given: str | None) -> bool:
        return bool(given) and hmac.compare_digest(given.encode(), token.encode())

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        if not token or request.url.path in open_paths:
            return await call_next(request)
        bearer = request.headers.get("authorization", "")
        if token_ok(bearer[7:] if bearer.lower().startswith("bearer ") else None) or token_ok(request.cookies.get("choz_token")):
            return await call_next(request)
        if request.method == "GET" and request.url.path == "/":
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"detail": "access token required"}, status_code=401)

    @app.get("/login", response_class=HTMLResponse)
    def login_page():
        return LOGIN_PAGE

    @app.post("/login")
    def login(access_token: str = Form(...)):
        if not token or not token_ok(access_token):
            return HTMLResponse(LOGIN_PAGE.replace("<!--err-->", "<p>Sai mã truy cập.</p>"), status_code=401)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie("choz_token", access_token, httponly=True, samesite="strict", max_age=12 * 3600)
        return response

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

    def run_folder(run_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{12}", run_id):
            raise HTTPException(404, "unknown run")
        folder = runs_dir / run_id
        if run_id not in agent.runs and not folder.is_dir():
            raise HTTPException(404, "unknown run")
        return folder

    @app.get("/api/runs/{run_id}/export")
    def export_run(run_id: str):
        """Everything kept about one listing (photos, studio image, run log), as a zip for the seller."""
        folder = run_folder(run_id)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(folder.glob("*")) if folder.is_dir() else []:
                zf.write(f, f"{run_id}/{f.name}")
            item = next((i for i in catalog.items() if i["id"] == run_id), None)
            if item:
                zf.writestr(f"{run_id}/catalog-item.json", json.dumps(item, ensure_ascii=False, indent=2))
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="choz-{run_id}.zip"'})

    @app.delete("/api/runs/{run_id}")
    def delete_run(run_id: str):
        """The seller deletes a listing's data: run folder (photos, studio image, log) and the catalog entry."""
        folder = run_folder(run_id)
        run = agent.runs.get(run_id)
        if run is not None and run.status == "running":
            raise HTTPException(409, "the agent is still working on this listing")
        shutil.rmtree(folder, ignore_errors=True)
        agent.runs.pop(run_id, None)
        result = {"deleted": run_id, "catalog_item_removed": catalog.remove(run_id), "folder_removed": not folder.exists()}
        audit.append(audit.path_for(settings.data_dir), "deleted_by_seller", run_id,
                     catalog_item_removed=result["catalog_item_removed"])
        return result

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


LOGIN_PAGE = """<!doctype html><html lang="vi"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Chợ Z · đăng nhập</title><body style="font-family:system-ui;max-width:360px;margin:15vh auto;padding:0 16px">
<h1 style="font-size:20px">Chợ Z listing agent</h1><!--err-->
<form method="post" action="/login"><label>Mã truy cập<br><input type="password" name="access_token" autofocus
style="width:100%;padding:8px;margin:6px 0 12px"></label><button style="padding:8px 16px">Vào</button></form></body></html>
"""
