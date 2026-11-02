"""The HTTP API the demo UI uses, on the mock client."""
import time

from fastapi.testclient import TestClient

from app.server import create_app
from tests.conftest import product_photo


def wait(client, run_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_create_poll_answer_publish(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").json()["mode"] == "mock"
        created = client.post("/api/runs", files=[("photos", ("a.jpg", product_photo(), "image/jpeg"))],
                              data={"note": "Áo thun cotton nam, còn 50 cái"}).json()
        run = wait(client, created["id"])
        assert run["status"] == "needs_answer" and run["question"]
        run = client.post(f"/api/runs/{run['id']}/answer", data={"text": "120 nghìn"}).json()
        assert run["status"] == "ready"
        assert client.get(f"/files/runs/{run['id']}/{run['studio_image']}").status_code == 200
        item = client.post(f"/api/runs/{run['id']}/publish", json={"title": "Áo thun cotton nam basic"}).json()
        assert item["title"] == "Áo thun cotton nam basic"
        assert client.get("/api/catalog").json()[0]["id"] == run["id"]
        assert client.get("/").status_code == 200


def test_bad_requests_get_clear_errors(settings):
    with TestClient(create_app(settings)) as client:
        no_words = client.post("/api/runs", files=[("photos", ("a.jpg", product_photo(), "image/jpeg"))])
        assert no_words.status_code == 422
        assert client.get("/api/runs/nope").status_code == 404


def test_ui_files_are_revalidated_so_updates_show_up(settings):
    with TestClient(create_app(settings)) as client:
        for path in ("/", "/web/app.js", "/web/style.css"):
            assert client.get(path).headers["cache-control"] == "no-cache", path


def test_old_run_folders_are_purged_and_recent_ones_kept(tmp_path):
    import os
    import time

    from app.server import purge_old_runs

    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir(); new.mkdir()
    past = time.time() - 10 * 86400
    os.utime(old, (past, past))
    assert purge_old_runs(tmp_path, 7) == 1 and not old.exists() and new.exists()
    assert purge_old_runs(tmp_path, 0) == 0


def test_metrics_aggregate_the_run_logs(settings):
    import asyncio

    from fastapi.testclient import TestClient

    from app.meta_api import MockMetaClient
    from app.pipeline import Agent
    from app.server import create_app
    from tests.conftest import product_photo

    agent = Agent(MockMetaClient(settings), settings.data_dir / "runs")
    run = agent.create([product_photo()], None, "Áo thun liên hệ 0912 345 678, giá 120 nghìn, còn 5 cái")
    asyncio.run(agent.process(run))
    client = TestClient(create_app(settings))
    m = client.get("/api/metrics").json()
    assert m["runs"] == 1 and m["per_day"][0]["runs"] == 1 and m["guards"] == {"privacy": 1}
    assert m["model_calls"] and "Agent metrics" in client.get("/metrics").text


def test_spend_guard_refuses_runs_over_the_daily_budget(settings, monkeypatch):
    """A live server refuses new runs once today's logged cost reaches DAILY_COST_CAP_USD (no paid call is made)."""
    import json as _json
    import time as _time

    from fastapi.testclient import TestClient

    from app.server import create_app
    from tests.conftest import product_photo

    runs = settings.data_dir / "runs" / "aaaaaaaaaaaa"
    runs.mkdir(parents=True)
    (runs / "run.json").write_text(_json.dumps({"created": _time.time(), "status": "ready", "steps": [],
                                                "totals": {"cost_usd": 6.0, "seconds": 10}}), encoding="utf-8")

    class LiveLike:          # the guard only applies to a live client; the mock never costs anything
        mock = False

        async def aclose(self):
            pass

    monkeypatch.setenv("DAILY_COST_CAP_USD", "5")
    client = TestClient(create_app(settings, client=LiveLike()))
    r = client.post("/api/runs", files={"photos": ("p.jpg", product_photo(), "image/jpeg")}, data={"note": "áo"})
    assert r.status_code == 429 and "budget" in r.json()["detail"]


def test_access_token(settings, monkeypatch):
    monkeypatch.setenv("LISTING_ACCESS_TOKEN", "s3cret-test")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").status_code == 200            # open for uptime checks
        assert client.get("/api/catalog").status_code == 401
        assert client.get("/metrics").status_code == 401
        assert client.get("/", follow_redirects=False).headers["location"] == "/login"
        assert client.get("/api/catalog", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/api/catalog", headers={"Authorization": "Bearer s3cret-test"}).status_code == 200
        assert client.post("/login", data={"access_token": "wrong"}).status_code == 401
        signed_in = client.post("/login", data={"access_token": "s3cret-test"}, follow_redirects=False)
        assert signed_in.status_code == 303 and "httponly" in signed_in.headers["set-cookie"].lower()
        assert client.get("/api/catalog").status_code == 200           # the cookie now rides along


def test_no_token_means_open(settings, monkeypatch):
    monkeypatch.delenv("LISTING_ACCESS_TOKEN", raising=False)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/catalog").status_code == 200


def test_seller_can_export_and_delete_their_data(settings):
    import io
    import zipfile

    with TestClient(create_app(settings)) as client:
        created = client.post("/api/runs", files=[("photos", ("a.jpg", product_photo(), "image/jpeg"))],
                              data={"note": "Áo thun cotton nam, giá 120 nghìn, còn 50 cái"}).json()
        run = wait(client, created["id"])
        client.post(f"/api/runs/{run['id']}/publish", json={})
        assert any(i["id"] == run["id"] for i in client.get("/api/catalog").json())
        exported = client.get(f"/api/runs/{run['id']}/export")
        names = zipfile.ZipFile(io.BytesIO(exported.content)).namelist()
        assert exported.headers["content-type"] == "application/zip"
        assert f"{run['id']}/run.json" in names and f"{run['id']}/catalog-item.json" in names
        gone = client.delete(f"/api/runs/{run['id']}").json()
        assert gone == {"deleted": run["id"], "catalog_item_removed": True, "folder_removed": True}
        assert not (settings.data_dir / "runs" / run["id"]).exists()
        assert all(i["id"] != run["id"] for i in client.get("/api/catalog").json())
        assert client.get(f"/api/runs/{run['id']}").status_code == 404
        assert client.delete("/api/runs/..%2F..%2Fetc").status_code == 404
        assert client.delete("/api/runs/0123456789ab").status_code == 404


def test_each_seller_sees_only_their_own_listings(settings, monkeypatch):
    from app import sellers

    monkeypatch.delenv("LISTING_ACCESS_TOKEN", raising=False)
    tok_a, tok_b = sellers.add(settings.data_dir, "ba-nam"), sellers.add(settings.data_dir, "co-tu")
    assert tok_a not in sellers.path_for(settings.data_dir).read_text(encoding="utf-8")      # hashes only
    a = {"Authorization": f"Bearer {tok_a}"}
    b = {"Authorization": f"Bearer {tok_b}"}
    with TestClient(create_app(settings)) as client:
        created = client.post("/api/runs", headers=a, files=[("photos", ("a.jpg", product_photo(), "image/jpeg"))],
                              data={"note": "Áo thun cotton nam, giá 120 nghìn, còn 50 cái"}).json()
        rid = created["id"]
        deadline = time.time() + 10
        while client.get(f"/api/runs/{rid}", headers=a).json()["status"] == "running" and time.time() < deadline:
            time.sleep(0.05)
        run = client.get(f"/api/runs/{rid}", headers=a).json()
        assert run["seller"] == "ba-nam"
        assert client.post(f"/api/runs/{rid}/publish", headers=a, json={}).status_code == 200
        photo = f"/files/runs/{rid}/{run['photos'][0]}"
        assert client.get(photo, headers=a).status_code == 200
        # seller B: the run, its photo, export, publish and delete all look like they do not exist
        assert client.get(f"/api/runs/{rid}", headers=b).status_code == 404
        assert client.get(photo, headers=b).status_code == 404
        assert client.get(f"/api/runs/{rid}/export", headers=b).status_code == 404
        assert client.post(f"/api/runs/{rid}/publish", headers=b, json={}).status_code == 404
        assert client.delete(f"/api/runs/{rid}", headers=b).status_code == 404
        assert client.get("/api/catalog", headers=b).json() == []
        assert [i["id"] for i in client.get("/api/catalog", headers=a).json()] == [rid]
        assert client.get("/api/catalog").status_code == 401
        assert client.delete(f"/api/runs/{rid}", headers=a).status_code == 200


def test_blocked_listing_appeal_reviewed_by_an_operator(settings, monkeypatch):
    import json as _json

    from app import audit, sellers

    monkeypatch.delenv("LISTING_ACCESS_TOKEN", raising=False)
    seller = {"Authorization": f"Bearer {sellers.add(settings.data_dir, 'ba-nam')}"}
    operator = {"Authorization": f"Bearer {sellers.add(settings.data_dir, 'an', operator=True)}"}
    with TestClient(create_app(settings)) as client:
        created = client.post("/api/runs", headers=seller, files=[("photos", ("a.jpg", product_photo(), "image/jpeg"))],
                              data={"note": "Thuốc kháng sinh amoxicillin, 80 nghìn một hộp, còn 30 hộp"}).json()
        rid = created["id"]
        deadline = time.time() + 10
        while client.get(f"/api/runs/{rid}", headers=seller).json()["status"] == "running" and time.time() < deadline:
            time.sleep(0.05)
        assert client.get(f"/api/runs/{rid}", headers=seller).json()["status"] == "blocked"     # C4 backstop
        # sellers cannot see the operator views
        assert client.get("/api/metrics", headers=seller).status_code == 403
        assert client.get("/api/appeals", headers=seller).status_code == 403
        assert client.get("/metrics", headers=operator).status_code == 200
        run = client.post(f"/api/runs/{rid}/appeal", headers=seller, json={"note": "Đây là hộp mẫu rỗng, gọi 0912345678"}).json()
        assert run["appeal"]["status"] == "pending" and "0912345678" not in run["appeal"]["note"]
        assert client.post(f"/api/runs/{rid}/appeal", headers=seller, json={}).status_code == 409
        pending = client.get("/api/appeals", headers=operator).json()
        assert [a["run_id"] for a in pending] == [rid] and pending[0]["seller"] == "ba-nam"
        assert client.post(f"/api/appeals/{rid}", headers=operator, json={"decision": "overturn"}).status_code == 422
        decided = client.post(f"/api/appeals/{rid}", headers=operator,
                              json={"decision": "uphold", "reason": "Thuốc kê đơn, không đăng được"}).json()
        assert decided["status"] == "blocked" and decided["appeal"]["status"] == "upheld" and decided["appeal"]["by"] == "an"
        assert client.get("/api/appeals", headers=operator).json() == []
    events = [_json.loads(x)["event"] for x in audit.path_for(settings.data_dir).read_text(encoding="utf-8").splitlines()]
    assert events[-2:] == ["appeal_requested", "appeal_upheld"]


def test_operator_overturn_makes_it_publishable_with_a_warning(settings, monkeypatch):
    from app import sellers

    monkeypatch.delenv("LISTING_ACCESS_TOKEN", raising=False)
    seller = {"Authorization": f"Bearer {sellers.add(settings.data_dir, 'ba-nam')}"}
    operator = {"Authorization": f"Bearer {sellers.add(settings.data_dir, 'an', operator=True)}"}
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post("/api/runs", headers=seller, files=[("photos", ("a.jpg", product_photo(), "image/jpeg"))],
                              data={"note": "Vape pod tinh dầu đồ chơi trưng bày, 80 nghìn, còn 3 cái"}).json()
        rid = created["id"]
        deadline = time.time() + 10
        while client.get(f"/api/runs/{rid}", headers=seller).json()["status"] == "running" and time.time() < deadline:
            time.sleep(0.05)
        client.post(f"/api/runs/{rid}/appeal", headers=seller, json={"note": "mô hình nhựa"})
        app.state.agent.runs.clear()                                      # as after a restart: read back from disk
        out = client.post(f"/api/appeals/{rid}", headers=operator, json={"decision": "overturn", "reason": "Mô hình nhựa"}).json()
        assert out["status"] == "ready" and out["verification"]["verdict"] == "warn" and "R1" in out["verification"]["rule_ids"]
        assert client.post(f"/api/runs/{rid}/publish", headers=seller, json={}).status_code == 409        # must read it
        assert client.post(f"/api/runs/{rid}/publish", headers=seller, json={"acknowledge": True}).status_code == 200


def test_seller_memory_recalls_and_updates_instead_of_duplicating(settings):
    with TestClient(create_app(settings)) as client:
        def listing(note):
            created = client.post("/api/runs", files=[("photos", ("a.jpg", product_photo(), "image/jpeg"))],
                                  data={"note": note}).json()
            return wait(client, created["id"])
        first = listing("Cháo hàu ăn liền Khánh Thọ loại hộp, giá 22 nghìn, còn 80 hộp")
        assert first["listing"]["catalog"]["match"] == "kt-chao-hau-hop" and "history" not in first["listing"]
        assert first["verification"]["verdict"] == "warn"     # the mock files it under "other"; the catalog says food
        client.post(f"/api/runs/{first['id']}/publish", json={"acknowledge": True})
        again = listing("Cháo hàu ăn liền Khánh Thọ loại hộp, còn 40 hộp")        # no price this time
        hist = again["listing"]["history"]
        assert hist["id"] == first["id"] and hist["price_vnd"] == 22000 and hist["how"] == "catalog"
        assert "Lần trước bạn bán 22.000đ" in again["question"]
        again = client.post(f"/api/runs/{again['id']}/answer", data={"text": "22 nghìn"}).json()
        assert again["listing"]["history"]["id"] == first["id"]                  # answering keeps memory and match
        assert again["listing"]["catalog"]["match"] == "kt-chao-hau-hop"
        item = client.post(f"/api/runs/{again['id']}/publish", json={"update_of": first["id"], "acknowledge": True}).json()
        assert item["id"] == first["id"] and item["stock"] == 40 and item["versions"] == 2
        assert item["catalog_id"] == "kt-chao-hau-hop"
        shop = client.get("/api/catalog").json()
        assert [i["id"] for i in shop] == [first["id"]]                            # one listing, updated
