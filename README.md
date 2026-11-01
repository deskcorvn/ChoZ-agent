# Chợ Z listing agent — one photo, one sentence, one listing

A small-seller assistant for **Chợ Z**, Gifty Tech's marketplace inside Zalo. The seller snaps 1–3 photos
and says, in Vietnamese, something like *"áo thun cotton nam, size M L XL, 120 nghìn, còn 50 cái"*.
Four Meta models turn that into a complete listing — title, description, category, attributes, price,
stock and a clean studio photo — in seconds instead of 10–15 minutes of typing.

> **What is new.** Chợ Z existed before this hackathon. **Everything in this repository is new work for the
> Meta Global AI Developer Hackathon** (build window Oct 26 – Nov 4, 2026; first commit Oct 31, 2026).
> It does not call Chợ Z: "publish" writes to a local sample catalog (`data/catalog.json`).

## How it works

```
photos + voice
   │
   ├────────────────────────── Muse Image: paint an EMPTY studio        (POST /v1/images/generations)
   │                            (slowest call, so it starts first and runs alongside everything)
   ├─ Muse Voice Transcribe ── Vietnamese speech → text                (POST /v1/asr/transcribe)
   │
   ├─ Muse Spark 1.3 ────────── photos + text → listing JSON            (chat completions, json_schema)
   │                             price/stock only from the seller's words; printed label beats speech
   │
   ├─┬─ Muse Spark 1.3 ──────── verify every claim AND the seller's words against the photos
   │ │                           ("you said 150 g, the label says 120 g" → warn; prohibited → block)
   │ └─ SAM 3.1 ─────────────── cut the real product out of photo 1     (POST /v1/responses, stream)
   │
   ├─ compose (local) ───────── real product pixels on the painted room, shadow along its edge
   │
   ├─ missing price or stock? → ask exactly one question → preview → publish
   └─ "Đổi nền" on the preview → Muse Image paints a room Spark described for this product
```

Measured on the live API (Oct 31, 2026, five OCOP food products in `samples/products`, `python -m app.batch`
run three times, 15 listings): price 15/15, stock 12/12, category 15/15 correct; 17.8 s per listing on
average (slowest 27.0 s), $0.025 each; the one product whose spoken weight (150 g) disagrees with its label
(120 g) was flagged on every run; no run failed. A prescription-drug test item was blocked and could not be
published.

Design choices that matter:

- **The product in the studio photo is the seller's real product.** SAM 3.1 cuts it out; Muse Image only
  paints the background; the product's pixels are scaled, never redrawn. No wrong colour, logo or shape.
- **Structured output everywhere.** Listing, verification and the follow-up answer all use a fixed JSON
  schema, and each answer is validated again (pydantic) before the agent uses it.
- **A verification step.** Spark checks each claim in the draft against the photos
  (consistent / not visible / contradicted) and blocks prohibited goods; a blocked item cannot be published.
- **One question, at most.** Only price and stock are required to go live; if either is missing the agent
  asks once, by voice or text. Anything still missing is typed in on the preview.
- **Everything is logged.** The right-hand panel shows every step: model, endpoint, duration, token or
  image usage and cost. The footer shows agent time (time waiting for the seller is not counted) and total cost.
- **Standard mode only.** Contributor-mode model IDs are cheaper but let Meta train on the request; sellers'
  photos and voices are not sent that way.

## Run it

Needs Python 3.12+. One line after cloning:

```bash
# macOS / Linux
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && cp .env.example .env.local && .venv/bin/python -m app
```

```powershell
# Windows
py -3.12 -m venv .venv; .venv\Scripts\pip install -r requirements.txt; copy .env.example .env.local; .venv\Scripts\python -m app
```

Open http://127.0.0.1:8000. Put your Meta Model API key in `.env.local` (`MODEL_API_KEY=...`) and restart.

**Without a key the app runs in mock mode** so the UI and flow can be tried. Mock mode is never silent: the
log panel header says `MOCK`, and every mocked model call is tagged `(mock)` with a cost of $0.

Use a browser on `localhost` (or HTTPS) so the microphone works. On a phone, the photo button opens the camera.

## Measure it on real products

```bash
python -m app.batch samples/products
```

Runs every product folder through the agent and writes `data/reports/batch-*.csv`: status, agent seconds,
cost, transcript, fields, verdict, and — if the folder has an `expected.json` — whether price, stock and
category were right. See `samples/README.md`.

## Studio: from listing to a 30-second product video (preview)

The same listing, one step further: a vertical 30-second spot a seller can post. The studio is nine small
services in `studio/`, each a module with `run(client, settings, input) -> output` (pydantic in and out),
one log line per model call in `studio/log.jsonl` (role, model, endpoint, ms, cost, decision), a CLI
(`python -m studio <service> --in file.json [--out file.json] [--mock]`) and a route (`POST /studio/<service>`,
`GET /studio/log`). `python -m studio demo samples/products/05-chao-hau-hop --mock` runs the whole chain
on one sample product.

```
script → shotlist → refs → voice → frames → render → qc → edit → judge
                                      └──────── ≤ 2 rounds per shot ────┘   (qc's director note rewrites the frame prompt)
```

| Service | In → out | Meta model (main path) |
|---|---|---|
| `script` | listing + brief → 4–8 voice-over beats (≤ 12 words), supers, CTA, claims with their source | Muse Spark 1.3, json_schema |
| `shotlist` | script + continuity bible → 5–8 s shots, one action each, locked camera, frame + motion prompt | Muse Spark 1.3, json_schema |
| `refs` | shot list → character in 3 angles, empty location, plain props (the product is the seller's photo, never repainted) | Muse Image (generations / edits) |
| `voice` | script → one voice-over take (TTS plug-in) → read back, WER per beat, beat map | Muse Voice Transcribe |
| `frames` | shot + refs → 3 painted 9:16 first frames → one picked, bad ones rejected before any render credit is spent | Muse Image edits + Muse Spark vision |
| `render` | first frame + motion prompt → silent 5–8 s clip | — (plug-in, see below) |
| `qc` | clip + acceptance criteria → per-criterion evidence, beats, usable window, accept / retry / reject | Muse Spark 1.3 with the clip as a file input |
| `edit` | beat map + accepted takes + real packshot → edit plan (validated in Python) → 1080×1920 mp4, subtitles, AI label, −14 LUFS | Muse Spark 1.3, json_schema |
| `judge` | finished mp4 → 14 technical checks, 6 criteria, timestamps, score | Muse Spark 1.3 with the file as input |

**The render engine is a plug-in outside Meta.** `studio/render.py` defines a small `Renderer` interface
(`submit(frame, prompt, seconds) → job_id`, `poll(job_id) → status | url`); `KlingMCPRenderer` targets Kling
image-to-video over its MCP server (token from `KLING_MCP_TOKEN` only; no job is sent below `KLING_CREDIT_FLOOR`),
and `MockRenderer` makes a Ken Burns clip from the first frame with ffmpeg (from `PATH` or `$FFMPEG`) so the
chain runs without credits. An external second-opinion reviewer on frames and clips is optional (`second_opinion`)
and off by default.

**Status: skeleton.** Every service runs in mock mode with sample data and is covered by `tests/test_studio.py`;
the prompts are marked `TODO(prompt)` and a live call without one is refused (HTTP 501) rather than billed.
The two new client calls (`/images/edits`, `/files` + `/responses` with a video) are shaped from the docs and
not yet exercised live. Measurements (shots accepted after round 1 / round 2, credits per clip, $ and seconds
per spot, frames rejected before credits) will be read from `studio/log.jsonl` and added here once the chain
has run on the live API.

## Cost

List prices (Standard mode, dev.meta.ai, Oct 31, 2026): Muse Spark 1.3 $1.25 / $4.25 per 1M input / output
tokens ($0.15 cached input), Muse Image $0.01 per image, SAM 3.1 $0.0025 per image, Muse Voice Transcribe
$0.18 per audio hour. The log shows the real cost of every run from the API's own usage numbers.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The tests run without a key or network: request shapes are checked against a mock transport (including
SAM 3.1's documented output, decoded with Meta's `meta_sam_parser`), and the agent runs end to end on the mock.

## Not yet verified against the live API

- The exact JSON body of `/v1/asr/transcribe`: the parser accepts the likely shapes and the raw body is
  shown in the log of the transcribe step.
- Whether the file endpoint accepts `languageBias` (documented for the realtime endpoint). If it answers
  400, the call is retried without it.
- Recognition quality for Vietnamese prices and sizes, and SAM 3.1 on cluttered phone photos — this is
  what `app.batch` on the sample products measures.
- Studio (preview): the body of `POST /v1/images/edits` with reference images, and whether Muse Spark
  accepts a video through `POST /v1/files` + `POST /v1/responses` (`input_file`). Request shapes are
  tested against a mock transport only.

## Layout

```
app/          agent (pipeline.py), Meta Model API client (meta_api.py), schemas, prompts, compose, server
studio/       listing → 30-second video, nine services + Renderer plug-in (preview, mock only so far)
web/          demo UI: a Zalo Mini App look-alike + agent log panel (plain HTML/CSS/JS)
samples/      categories and sample products
tests/        pytest, no network
```
