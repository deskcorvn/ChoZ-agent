"""The simulated day (sim/): schema, mock run, validation, fees and the headcount table. No network, no keys."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from sim import workload
from sim.run_day import run
from sim.schema import Emitter
from sim.validate import check
from sim.world import FEE_PCT, SHARE, fee_split, generate


@pytest.fixture(scope="module")
def mock_day(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("mock")
    t0 = time.perf_counter()
    asyncio.run(run("mock", 7, out))
    assert time.perf_counter() - t0 < 10
    return out


def events(out: Path) -> list[dict]:
    return workload.read_events(out / "events.jsonl")


def test_mock_day_passes_validation(mock_day):
    errors, stats = check(mock_day / "events.jsonl")
    assert errors == []
    assert 120 <= stats["events"] <= 250
    assert stats["scenes"] == 7 and stats["orders"] == 30 and stats["blocked"] == 1


def test_every_event_is_mock_and_costs_nothing(mock_day):
    evs = events(mock_day)
    assert {e["mode"] for e in evs} == {"mock"}
    assert sum((e.get("usage") or {}).get("cost_usd", 0) for e in evs) == 0


def test_last_metric_matches_summary(mock_day):
    last = events(mock_day)[-1]
    summary = json.loads((mock_day / "summary.json").read_text(encoding="utf-8"))
    assert last["type"] == "metric"
    for key in ("orders", "gmv_vnd", "agent_actions", "human_approvals", "human_minutes", "blocked"):
        assert last["metrics"][key] == summary[key]


def test_missing_approval_is_caught(mock_day, tmp_path):
    lines = (mock_day / "events.jsonl").read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if json.loads(line)["type"] != "approve"]
    bad = tmp_path / "events.jsonl"
    bad.write_text("\n".join(kept) + "\n", encoding="utf-8")
    errors, _ = check(bad)
    assert any("chưa có approve" in e for e in errors)


def test_same_seed_same_world():
    assert generate(7).to_json() == generate(7).to_json()
    assert generate(7).to_json() != generate(8).to_json()


def test_fee_split_is_exact():
    split = fee_split(100_000)
    assert split["fee_vnd"] == 100_000 * FEE_PCT // 100 == 10_000
    assert (split["tram"], split["kho_giao"], split["gioi_thieu"], split["cho_z"]) == (5_000, 2_000, 1_000, 2_000)
    assert sum(SHARE.values()) == 10


def test_settlement_fee_recomputed_by_hand(mock_day):
    settle = next(e for e in reversed(events(mock_day)) if e["type"] == "tool" and e["tool"]["name"] == "settlement")
    for row in settle["detail"]["output"]["rows"][:3]:
        assert row["fee_vnd"] == row["amount_vnd"] // 10
        assert row["tram"] == row["amount_vnd"] * 5 // 100 and row["cho_z"] == row["amount_vnd"] * 2 // 100


def test_headcount_follows_the_norms(mock_day, tmp_path):
    base = workload.compute(events(mock_day))
    norms = tmp_path / "norms.json"
    norms.write_text(json.dumps([{"key": "nhan_dat_gio", "minutes": 4}]), encoding="utf-8")
    changed = workload.write(mock_day / "events.jsonl", tmp_path / "h.json", norms)
    assert changed["tram_1"]["manual_minutes"] == base["tram_1"]["manual_minutes"] + 30 * 2
    assert changed["tram_1"]["with_agents_minutes"] == base["tram_1"]["with_agents_minutes"]


def test_physical_work_is_never_saved(mock_day):
    data = workload.compute(events(mock_day))
    for row in data["per_tram_day"]["rows"]:
        if row["kind"] == "tay_chan":
            assert row["manual_min"] == row["with_agent_min"]
        assert row["with_agent_min"] <= row["manual_min"]
    for block in ("tram_1", "tram_50"):
        b = data[block]
        assert b["with_agents_people"] <= b["manual_people"]
        assert b["office"]["with_agents_people"] <= b["office"]["manual_people"]


def test_emitter_refuses_bad_events(tmp_path):
    em = Emitter(tmp_path / "e.jsonl", "mock", 26300)
    with pytest.raises(ValueError):
        em.emit(0, "say", **{"from": "ag_chia_don", "to": "hu_tram", "text": "x" * 91})
    with pytest.raises(ValueError):
        em.emit(0, "say", **{"from": "ag_khong_co", "to": "hu_tram", "text": "1 đơn"})
    with pytest.raises(ValueError):
        em.emit(0, "move", **{"from": "ag_chia_don", "loc": "loc_tram"})
    em.emit(10, "scene", scene="dat_gio")
    with pytest.raises(ValueError):
        em.emit(5, "scene", scene="dang_ban")
    em.close()


def test_replay_over_websocket(mock_day):
    pytest.importorskip("websockets")
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve as ws_serve

    from sim import serve

    async def scenario() -> int:
        hub = serve.Hub()
        async with ws_serve(hub.handler, "localhost", 0) as server:
            port = server.sockets[0].getsockname()[1]
            got = []
            async with connect(f"ws://localhost:{port}") as ws:
                task = asyncio.create_task(serve.replay(hub, mock_day / "events.jsonl", 1e9, True))

                async def read():
                    while len(got) < len(events(mock_day)):
                        got.append(json.loads(await ws.recv()))
                await asyncio.wait_for(read(), 10)
                await task
            assert [e["id"] for e in got] == [e["id"] for e in events(mock_day)]
            return len(got)

    assert asyncio.run(scenario()) == len(events(mock_day))


def test_final_view_is_whole_people_and_station_minutes(mock_day):
    v = workload.compute(events(mock_day))["view"]
    st, c = v["station"], v["central_50"]
    assert st["with_agents_minutes_day"] < st["manual_minutes_day"]          # same person, less paperwork
    assert all(r["people"] is None or isinstance(r["people"], int) for r in c["manual"] + c["with_agents"])
    same_m = {r["role"]: r["cost_vnd_month"] for r in c["manual"] if r["kind"] == "tay_chan"}
    same_a = {r["role"]: r["cost_vnd_month"] for r in c["with_agents"] if r["kind"] == "tay_chan"}
    assert same_m == same_a                                                  # same flow, same physical work
    assert c["office_people_with_agents"] < c["office_people_manual"]
    assert c["managers"] == {"manual": 4, "with_agents": 2}
    assert c["saving_vnd_month"] == (c["manual_cost_vnd_month"] - c["with_agents_staff_cost_vnd_month"]
                                     - c["agents_cost_vnd_month"])
    assert c["fee_pool"]["vnd_month"] == 180_000_000
    assert all(a["source"] for a in v["assumptions"])


def test_staffing_follows_its_rates():
    from sim import staffing
    base = staffing.team(50, 11, 0)
    old = staffing.CONTACTS_PER_100
    try:
        staffing.CONTACTS_PER_100 = 20
        more = staffing.team(50, 11, 0)
    finally:
        staffing.CONTACTS_PER_100 = old
    cs = lambda t: next(r["people"] for r in t["manual"] if r["role"] == "chăm sóc khách hàng")  # noqa: E731
    assert cs(more) > cs(base)
    assert more["with_agents_staff_cost_vnd_month"] == base["with_agents_staff_cost_vnd_month"]   # agents absorb it


def test_people_summary_is_whole_people_and_plain_words(mock_day):
    from sim import staffing
    ps = workload.compute(events(mock_day))["view"]["central_50"]["people_summary"]
    for side in ("manual", "with_agents"):
        assert all(isinstance(g[side], int) for g in ps["groups"])
        assert sum(g[side] for g in ps["groups"]) == ps[f"{side}_people"]
    for k, vnd in (("manual_cost_trieu", "manual_cost_vnd_month"), ("with_agents_cost_trieu", "with_agents_cost_vnd_month")):
        assert ps[k] == round(ps[vnd] / 1e6)                              # money to the nearest 1 triệu
    text = " ".join([ps["headline"], ps["cost_line"]] + [g["label"] for g in ps["groups"]])
    assert "tay chân" not in text and "→" not in text
    assert ("dưới mức" in ps["cost_line"]) == ps["within_fee_pool"]
    assert staffing.about_million(177_421_000) == 177 and staffing.about_million(444_556_000) == 445
