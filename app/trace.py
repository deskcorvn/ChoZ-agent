"""Each run as an OpenTelemetry trace, following the GenAI semantic conventions, in OTLP/JSON.

The step log already holds what a trace needs (start, duration, model, tokens, cost, status); this module renders it
so any OTLP backend (Jaeger, Grafana Tempo, Langfuse, Honeycomb) can show a listing as one trace:

- root span `invoke_workflow choz-listing-agent` (INTERNAL) for the run;
- one span per Meta model call: `chat muse-spark-1.3` (`gen_ai.operation.name` = chat), `generate_content
  muse-image-1.0` for Muse Image; Transcribe and SAM use custom operation names (`transcribe`, `segment`), as the
  conventions allow when no well-known value applies. Attributes: `gen_ai.provider.name` = meta,
  `gen_ai.request.model`, `gen_ai.usage.input_tokens` / `output_tokens` / `cache_read.input_tokens` when the API
  reports them, `error.type` on failure (network / auth / quota / server / request / budget);
- `retrieval` for the catalog search (`gen_ai.data_source.id`), `execute_tool {name}` for local guards and checks.
Custom attributes carry the cost (`choz.cost_usd`) and the seller-facing decision (`choz.verdict`, `choz.rules`).
Content (prompts, photos, words) is not exported: the trace has the same privacy as the audit log.

    GET /api/runs/{id}/trace                         (the run's owner or an operator)
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318  → every finished run is POSTed to {endpoint}/v1/traces
"""
from __future__ import annotations

import hashlib
import os

import httpx

SERVICE = "choz-listing-agent"
OPERATIONS = {"muse-spark": "chat", "muse-image": "generate_content", "muse-voice-transcribe": "transcribe",
              "sam": "segment"}
LOCAL_TOOLS = {"prepare", "compose", "privacy", "injection", "origin", "backstop", "fallback", "match", "memory"}


def _id(seed: str, n: int) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:n]


def _attr(key: str, value) -> dict:
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    elif isinstance(value, (list, tuple)):
        v = {"arrayValue": {"values": [{"stringValue": str(x)} for x in value]}}
    else:
        v = {"stringValue": str(value)}
    return {"key": key, "value": v}


def _operation(model: str) -> str:
    for prefix, op in OPERATIONS.items():
        if model.startswith(prefix):
            return op
    return "chat"


def spans(run: dict) -> list[dict]:
    trace_id = _id(run["id"], 32)
    root_id = _id(run["id"] + ":root", 16)
    steps = run.get("steps") or []
    t0 = min((s.get("started") or run.get("created") or 0) for s in steps) if steps else run.get("created", 0)
    t1 = max(((s.get("started") or t0) + (s.get("ms") or 0) / 1000) for s in steps) if steps else t0
    v = run.get("verification") or {}
    root_attrs = [_attr("gen_ai.operation.name", "invoke_workflow"), _attr("gen_ai.agent.name", SERVICE),
                  _attr("gen_ai.conversation.id", run["id"]), _attr("choz.status", run.get("status", "")),
                  _attr("choz.cost_usd", float((run.get("totals") or {}).get("cost_usd") or 0)),
                  _attr("choz.model_calls", int((run.get("totals") or {}).get("model_calls") or 0))]
    if v:
        root_attrs += [_attr("choz.verdict", v.get("verdict", "")), _attr("choz.rules", sorted(set(v.get("rule_ids") or [])))]
    out = [{"traceId": trace_id, "spanId": root_id, "name": f"invoke_workflow {SERVICE}", "kind": 1,
            "startTimeUnixNano": str(int(t0 * 1e9)), "endTimeUnixNano": str(int(t1 * 1e9)),
            "attributes": root_attrs, "status": {"code": 2 if run.get("status") == "failed" else 1}}]
    for k, s in enumerate(steps):
        start = s.get("started") or t0
        end = start + (s.get("ms") or 0) / 1000
        model = s.get("model") or ""
        usage = (s.get("output") or {}).get("usage") if isinstance(s.get("output"), dict) else None
        if model and not model.startswith("cache"):
            op = _operation(model)
            name, kind = f"{op} {model}", 3                                   # CLIENT: a call to the Meta Model API
            attrs = [_attr("gen_ai.operation.name", op), _attr("gen_ai.provider.name", "meta"),
                     _attr("gen_ai.request.model", model), _attr("server.address", "api.meta.ai"),
                     _attr("choz.cost_usd", float(s.get("cost_usd") or 0))]
            if isinstance(usage, dict):
                if usage.get("prompt_tokens") is not None:
                    attrs.append(_attr("gen_ai.usage.input_tokens", int(usage["prompt_tokens"])))
                if usage.get("completion_tokens") is not None:
                    attrs.append(_attr("gen_ai.usage.output_tokens", int(usage["completion_tokens"])))
                cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                if cached:
                    attrs.append(_attr("gen_ai.usage.cache_read.input_tokens", int(cached)))
        elif s.get("key") == "retrieve":
            name, kind = "retrieval catalog", 1
            cands = (s.get("output") or {}).get("candidates") or []
            attrs = [_attr("gen_ai.operation.name", "retrieval"), _attr("gen_ai.data_source.id", "catalog"),
                     _attr("choz.candidates", [c.get("id") for c in cands])]
        else:
            tool = s.get("key") or "step"
            name, kind = f"execute_tool {tool}", 1
            attrs = [_attr("gen_ai.operation.name", "execute_tool"), _attr("gen_ai.tool.name", tool),
                     _attr("gen_ai.tool.type", "function"),
                     *([_attr("choz.cached_from", model)] if model.startswith("cache") else [])]
        attrs.append(_attr("choz.step", s.get("title", "")))
        failed = s.get("status") == "failed"
        if failed:
            err = s.get("error") or ""
            kind_ = err.split("[", 1)[1].split("]", 1)[0] if "[" in err and "]" in err else err.split(":", 1)[0]
            attrs.append(_attr("error.type", kind_ or "error"))
        out.append({"traceId": trace_id, "spanId": _id(f"{run['id']}:{k}", 16), "parentSpanId": root_id, "name": name,
                    "kind": kind, "startTimeUnixNano": str(int(start * 1e9)), "endTimeUnixNano": str(int(end * 1e9)),
                    "attributes": attrs, "status": {"code": 2 if failed else 1}})
    return out


def otlp(run: dict) -> dict:
    """An OTLP/JSON ExportTraceServiceRequest for one run."""
    return {"resourceSpans": [{
        "resource": {"attributes": [_attr("service.name", SERVICE)]},
        "scopeSpans": [{"scope": {"name": "app.trace", "version": "1"}, "spans": spans(run)}]}]}


def export(run: dict) -> None:
    """POST the run's trace to $OTEL_EXPORTER_OTLP_ENDPOINT when it is set; never fails the run."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").rstrip("/")
    if not endpoint:
        return
    try:
        httpx.post(f"{endpoint}/v1/traces", json=otlp(run), timeout=5)
    except Exception:  # an observability outage must not break listings
        pass
