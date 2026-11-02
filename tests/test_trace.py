"""Runs as OpenTelemetry traces (GenAI semantic conventions), without content."""
import time

from fastapi.testclient import TestClient

from app.server import create_app
from tests.conftest import product_photo


def attrs(span):
    return {a["key"]: next(iter(a["value"].values())) for a in span["attributes"]}


def test_a_run_is_an_otlp_trace_with_genai_attributes(settings):
    with TestClient(create_app(settings)) as client:
        created = client.post("/api/runs", files=[("photos", ("a.jpg", product_photo(), "image/jpeg"))],
                              data={"note": "Cháo hàu ăn liền Khánh Thọ loại hộp, giá 22 nghìn, còn 80 hộp, gọi 0912 345 678"}).json()
        deadline = time.time() + 10
        while client.get(f"/api/runs/{created['id']}").json()["status"] == "running" and time.time() < deadline:
            time.sleep(0.05)
        body = client.get(f"/api/runs/{created['id']}/trace").json()
    spans = body["resourceSpans"][0]["scopeSpans"][0]["spans"]
    root, children = spans[0], spans[1:]
    assert len(root["traceId"]) == 32 and len(root["spanId"]) == 16 and "parentSpanId" not in root
    assert root["name"] == "invoke_workflow choz-listing-agent" and attrs(root)["gen_ai.operation.name"] == "invoke_workflow"
    assert all(c["parentSpanId"] == root["spanId"] and c["traceId"] == root["traceId"] for c in children)
    names = [c["name"] for c in children]
    assert "chat muse-spark-1.3" in names and "retrieval catalog" in names and "execute_tool privacy" in names
    chat = next(c for c in children if c["name"] == "chat muse-spark-1.3")
    assert attrs(chat)["gen_ai.provider.name"] == "meta" and attrs(chat)["gen_ai.request.model"] == "muse-spark-1.3"
    assert all(int(c["endTimeUnixNano"]) >= int(c["startTimeUnixNano"]) for c in spans)
    values = str([attrs(s) for s in spans])           # attribute values only: ids and times are random digits
    assert "0912" not in values and "Cháo hàu ăn liền Khánh Thọ loại hộp, giá" not in values


def test_every_run_is_stamped_with_what_produced_it():
    from app import prompts, versions
    from app.pipeline import Run
    v = Run(id="abc", mode="mock").public()["versions"]
    assert set(v) == {"prompt_listing", "prompt_verify", "prompt_answer", "policy", "catalog"}
    assert all(len(x) == 10 for x in v.values())
    versions.current.cache_clear()
    old = prompts.LISTING
    try:
        prompts.LISTING = old + " "
        assert versions.current()["prompt_listing"] != v["prompt_listing"]      # any edit changes the stamp
    finally:
        prompts.LISTING = old
        versions.current.cache_clear()
