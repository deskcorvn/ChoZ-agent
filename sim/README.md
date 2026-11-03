# sim/ — Một ngày của Trạm Z

One simulated day of one Chợ Z station (50 households, 30 baskets, 5 suppliers, 1 warehouse, 1 van), worked by six
agents and approved by two people, written to `events.jsonl` for the stage page `/demo` (repo choz-landingpage).

| Agent | Runs on | Does |
|---|---|---|
| `ag_dang_ban` | Meta, `app/pipeline.py` of this repo (not rewritten) | photo + voice → listing, cut-out, studio photo |
| `ag_kiem_chung` | Meta (verify step of the pipeline) + Claude (lots at the warehouse) | checks claims against photos, blocks prohibited items, checks label/expiry/temperature |
| `ag_chia_don` | Claude, AgentScope ReActAgent | baskets → picklist, proposes substitutes when short |
| `ag_giao_nhan` | Claude | tells the warehouse what to collect, van schedule, laundry on the way back |
| `ag_cham_khach` | Claude | answers a customer, proposes an exchange |
| `ag_doi_soat` | Claude | settles completed orders: 10 % fee split 5/2/1/2, amounts per supplier |

People (`hu_tram`, `hu_kho`) approve everything with money or a customer in it; every approval is an
`approve_request` + `approve` pair. Arithmetic (fees, picklists, totals) is plain Python in `tools.py`; agents call
those tools and word the result.

## Run in 10 minutes

```powershell
py -3.12 -m venv .venv-sim
.venv-sim\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt -r sim/requirements.txt
$env:PYTHONIOENCODING = "utf-8"

.venv-sim\Scripts\python.exe -m sim.run_day --mode mock --seed 7 --out runs/mock     # < 10 s, no keys, no network
.venv-sim\Scripts\python.exe -m sim.validate runs/mock/events.jsonl                    # "0 lỗi"
.venv-sim\Scripts\python.exe -m sim.workload runs/mock/events.jsonl                    # rewrites headcount.json
.venv-sim\Scripts\python.exe -m pytest -q tests/test_sim.py
```

Live (real Claude and Meta calls, about 0.5 USD a day):

```powershell
# .env.local (git-ignored) holds MODEL_API_KEY (Meta) and ANTHROPIC_API_KEY
.venv-sim\Scripts\python.exe -m sim.run_day --mode live --seed 7 --out runs/live_final
.venv-sim\Scripts\python.exe -m sim.run_day --mode live --only dat_gio --out F:/_tmp/try   # one scene
```

Settings (environment or `.env.local`): `CLAUDE_MODEL` (default `claude-sonnet-5-5`), `MAX_RUN_USD` (default 40: the
run stops in order when reached), `USD_VND` (default 26300, the brief's rate of 03/11/2026), `LISTING_*` as in the app.

Stage feed: `python -m sim.serve --replay runs/live_final/events.jsonl --speed 3120` (26 h in 30 s, port 8765), or
`--live` to run the day and push events as they happen. The stage opens `/demo?ws=ws://localhost:8765`.

## Output (`runs/<name>/`)

| File | What |
|---|---|
| `events.jsonl` | the day, schema in `schema.py` (section 3 of the brief), sorted by `t` |
| `summary.json` | totals, cost per provider and model (calls, tokens, images, audio seconds, USD), latency p50/p95, price sources |
| `headcount.json` | people by hand vs people + agents, 1 station and 50 stations, every norm with its source (`workload.py`) |
| `log.jsonl` | every model call as returned: usage, stop reason, content |
| `world.json` | the generated world (same seed → same world) |

`t` is simulated seconds from 00:00 of the day; the evening before is negative (20:00 = −14400), so 05:12 = 18720 as
in the brief. The last line is always a metric whose numbers equal `summary.json`; `validate.py` recomputes them from
the lines, and the fee of every completed order from `world.json`.

## Honesty rules in code

- Mock events say `mode: "mock"` and carry zero usage; `Emitter` refuses a mock event with a cost.
- Claude usage is read from each Messages API response (input, output, cache read, cache write) and priced with
  `prices.py` (source and date inside); Meta usage and cost are the pipeline's own step log (`app/prices.py`).
- Each model call's usage is attached to the event it produced, so `cost_total_usd` is the sum of the event lines;
  `summary.json` also sums the raw calls and reports whether the two agree.
- Substitutions cannot be applied before a person approves (`tools.apply_substitution` refuses).
- Physical work (picking, receiving, handing over) counts the same minutes on both sides of the headcount table.

## Notes

- AgentScope 1.0.21 imports a function that mcp 2.x renamed; `agents/claude.py` aliases it (MCP is not used).
- Claude 5.5 models refuse forced tool use; AgentScope forces its structured-output tool, so the call is sent with
  `tool_choice: auto` and the prompt says to finish with `generate_response`.
- Samples: Khánh Thọ products from `samples/products` (published with permission); the prohibited item in
  `sim/samples/hang-cam` is drawn by code; the supplier's voice is synthetic (VieNeu-TTS, preset "Đoan Trang").
- Open questions and assumptions: `sim/YEU-CAU.md`.
