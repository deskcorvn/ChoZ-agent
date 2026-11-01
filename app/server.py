"""HTTP API and the demo UI (a Zalo Mini App look-alike plus the agent log panel)."""
from __future__ import annotations

import asyncio
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

from app import audit, config, guard, metrics, policy, sellers
from app.catalog import Catalog
from app.config import ROOT, Settings
from app.meta_api import make_client
from app.pipeline import Agent, Run
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
    # Sellers and their token hashes (data/sellers.json, plus LISTING_ACCESS_TOKEN as "shop"). Empty = open demo.
    table = sellers.load(settings.data_dir)
    role_of = sellers.roles(settings.data_dir)

    def require_operator() -> None:
        if table and role_of.get(sellers.current.get()) != "operator":
            raise HTTPException(403, "operators only")
    open_paths = ("/login", "/api/health")

    def owner_of(run_id: str) -> str | None:
        run = agent.runs.get(run_id)
        if run is not None:
            return run.seller
        saved = runs_dir / run_id / "run.json"
        return json.loads(saved.read_text(encoding="utf-8")).get("seller", "") if saved.is_file() else None

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        if not table or request.url.path in open_paths:
            return await call_next(request)
        bearer = request.headers.get("authorization", "")
        seller = sellers.who(table, bearer[7:] if bearer.lower().startswith("bearer ") else None) or sellers.who(
            table, request.cookies.get("choz_token"))
        if seller is None:
            if request.method == "GET" and request.url.path == "/":
                return RedirectResponse("/login", status_code=303)
            return JSONResponse({"detail": "access token required"}, status_code=401)
        m = re.match(r"/files/runs/([0-9a-f]{12})/", request.url.path)
        if m and owner_of(m.group(1)) != seller:           # another seller's photos: as if they did not exist
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        token = sellers.current.set(seller)
        try:
            return await call_next(request)
        finally:
            sellers.current.reset(token)

    def mine(run_id: str) -> bool:
        return not table or owner_of(run_id) == sellers.current.get()

    @app.get("/login", response_class=HTMLResponse)
    def login_page():
        return LOGIN_PAGE

    @app.post("/login")
    def login(access_token: str = Form(...)):
        if not table or sellers.who(table, access_token) is None:
            return HTMLResponse(LOGIN_PAGE.replace("<!--err-->", "<p>Sai mã truy cập.</p>"), status_code=401)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie("choz_token", access_token, httponly=True, samesite="strict", max_age=12 * 3600)
        return response

    def get_run(run_id: str):
        run = agent.runs.get(run_id)
        if run is None or not mine(run_id):
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
            run = agent.create([i for i in images if i], await read_limited(audio, MAX_AUDIO_BYTES), note,
                               seller=sellers.current.get())
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
        if (run_id not in agent.runs and not folder.is_dir()) or not mine(run_id):
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
        audit.append(audit.path_for(settings.data_dir), "deleted_by_seller", run_id, seller=sellers.current.get(),
                     catalog_item_removed=result["catalog_item_removed"])
        return result

    @app.get("/api/catalog")
    def list_catalog():
        items = catalog.items()
        return [i for i in items if i.get("seller", "") == sellers.current.get()] if table else items

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
        require_operator()
        return metrics.collect(runs_dir, days)

    @app.get("/metrics")
    def metrics_page():
        require_operator()
        return HTMLResponse(metrics.PAGE)

    # ---- appeals: a person reviews what the agent blocked (policy R1) -----------------------------------

    def load_any(run_id: str):
        """Any seller's run, for the operator: from memory, or read back from run.json after a restart."""
        if not re.fullmatch(r"[0-9a-f]{12}", run_id):
            raise HTTPException(404, "unknown run")
        run = agent.runs.get(run_id)
        if run is None and (runs_dir / run_id / "run.json").is_file():
            run = Run.from_dict(json.loads((runs_dir / run_id / "run.json").read_text(encoding="utf-8")))
            agent.runs[run_id] = run
        if run is None:
            raise HTTPException(404, "unknown run")
        return run

    @app.post("/api/runs/{run_id}/appeal")
    def appeal(run_id: str, body: dict | None = None):
        run = get_run(run_id)
        if run.status != "blocked":
            raise HTTPException(409, "only a blocked listing can be appealed")
        if run.appeal and run.appeal.get("status") == "pending":
            raise HTTPException(409, "already waiting for review")
        note = str((body or {}).get("note", ""))[:500]
        run.appeal = {"status": "pending", "note": guard.mask_text(note)[0], "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        agent._save(run)
        audit.append(audit.path_for(settings.data_dir), "appeal_requested", run.id, seller=run.seller,
                     rules=sorted((run.verification or {}).get("rule_ids") or []))
        return run.public()

    @app.get("/api/appeals")
    def list_appeals():
        require_operator()
        pending = []
        for f in sorted(runs_dir.glob("*/run.json")):
            data = json.loads(f.read_text(encoding="utf-8"))
            if (data.get("appeal") or {}).get("status") == "pending":
                v = data.get("verification") or {}
                pending.append({"run_id": data["id"], "seller": data.get("seller", ""), "title": (data.get("listing") or {}).get("title"),
                                "photo": f"/files/runs/{data['id']}/{(data.get('photos') or [''])[0]}",
                                "reason": v.get("prohibited_reason") or v.get("summary_vi"), "rules": v.get("rule_ids"),
                                "seller_note": data["appeal"].get("note"), "at": data["appeal"].get("at")})
        return pending

    @app.post("/api/appeals/{run_id}")
    def decide_appeal(run_id: str, body: dict):
        require_operator()
        run = load_any(run_id)
        decision, reason = body.get("decision"), str(body.get("reason", "")).strip()[:300]
        if not run.appeal or run.appeal.get("status") != "pending":
            raise HTTPException(409, "no pending appeal for this listing")
        if decision not in ("uphold", "overturn") or not reason:
            raise HTTPException(422, "decision must be uphold or overturn, with a reason")
        run.appeal.update(status={"uphold": "upheld", "overturn": "overturned"}[decision], reason=reason,
                          by=sellers.current.get() or "operator", decided_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        if decision == "overturn":
            v = run.verification or {}
            v.update(verdict="warn", prohibited_reason="",
                     summary_vi="Người duyệt đã xem lại và cho phép đăng: " + reason)
            v["rule_ids"] = list(dict.fromkeys((v.get("rule_ids") or []) + ["R1"]))
            v["rules"], _ = policy.cited(v["rule_ids"])
            run.verification, run.status = v, "ready"
        agent._save(run)
        audit.append(audit.path_for(settings.data_dir), "appeal_" + run.appeal["status"], run.id, seller=run.seller,
                     by=run.appeal["by"])
        return run.public()

    @app.get("/review")
    def review_page():
        require_operator()
        return HTMLResponse(REVIEW_PAGE)

    return app


LOGIN_PAGE = """<!doctype html><html lang="vi"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Chợ Z · đăng nhập</title><body style="font-family:system-ui;max-width:360px;margin:15vh auto;padding:0 16px">
<h1 style="font-size:20px">Chợ Z listing agent</h1><!--err-->
<form method="post" action="/login"><label>Mã truy cập<br><input type="password" name="access_token" autofocus
style="width:100%;padding:8px;margin:6px 0 12px"></label><button style="padding:8px 16px">Vào</button></form></body></html>
"""


REVIEW_PAGE = """<!doctype html><html lang="vi"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Chợ Z · xem lại tin bị chặn</title>
<body style="font-family:system-ui;max-width:760px;margin:24px auto;padding:0 16px;color:#1f2937">
<h1 style="font-size:20px">Tin bị chặn chờ xem lại</h1><p id="empty" hidden>Không có yêu cầu nào.</p><div id="list"></div>
<script>
async function load() {
  const items = await (await fetch('/api/appeals')).json();
  document.getElementById('empty').hidden = items.length > 0;
  const list = document.getElementById('list'); list.replaceChildren();
  for (const a of items) {
    const card = document.createElement('div');
    card.style.cssText = 'display:flex;gap:16px;border:1px solid #e5e7eb;border-radius:12px;padding:12px;margin:12px 0';
    const img = document.createElement('img'); img.src = a.photo; img.style.cssText = 'width:140px;height:140px;object-fit:cover;border-radius:8px';
    const body = document.createElement('div'); body.style.flex = '1';
    const lines = [['Tin', a.title], ['Người bán', a.seller || '-'], ['Lý do chặn', a.reason], ['Quy định', (a.rules || []).join(', ')],
                   ['Người bán viết', a.seller_note || '-'], ['Lúc', a.at]];
    for (const [k, v] of lines) { const p = document.createElement('div'); p.textContent = k + ': ' + (v || ''); body.append(p); }
    const why = document.createElement('input'); why.placeholder = 'Lý do quyết định (bắt buộc)'; why.style.cssText = 'width:100%;margin:8px 0;padding:6px';
    body.append(why);
    for (const [label, decision] of [['Giữ chặn', 'uphold'], ['Cho đăng (kèm cảnh báo)', 'overturn']]) {
      const b = document.createElement('button'); b.textContent = label; b.style.marginRight = '8px';
      b.onclick = async () => {
        const r = await fetch('/api/appeals/' + a.run_id, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                          body: JSON.stringify({decision, reason: why.value})});
        if (!r.ok) { alert((await r.json()).detail); return; } load();
      };
      body.append(b);
    }
    card.append(img, body); list.append(card);
  }
}
load();
</script></body></html>
"""

