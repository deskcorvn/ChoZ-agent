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
