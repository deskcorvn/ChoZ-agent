"""The listing agent: photos + a spoken note → transcript → listing → (verify ‖ segment ‖ background) → studio photo.

Each step is logged with the model it used, the endpoint, duration, usage and cost, so the demo's log
panel shows exactly which Meta model did what. At most one follow-up question is asked, and only for
what a listing cannot go live without (price, stock).
"""
from __future__ import annotations

import asyncio
import io
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image
from pydantic import ValidationError

from app import compose, prompts
from app.schema import LISTING_SCHEMA, SALE_FACTS_SCHEMA, VERIFICATION_SCHEMA, Listing, SaleFacts, Verification


@dataclass
class Step:
    key: str
    title: str
    model: str = ""
    endpoint: str = ""
    status: str = "running"          # running | done | failed | skipped
    started: float = field(default_factory=time.time)
    ms: int = 0
    cost_usd: float = 0.0
    mock: bool = False
    input: str = ""
    output: object = None
    error: str = ""


@dataclass
class Run:
    id: str
    mode: str                        # live | mock
    status: str = "running"          # running | needs_answer | ready | blocked | failed | published
    photos: list[str] = field(default_factory=list)
    note: str = ""
    transcript: str = ""
    listing: dict | None = None
    verification: dict | None = None
    studio_image: str | None = None
    question: str | None = None
    asked: bool = False
    steps: list[Step] = field(default_factory=list)
    error: str = ""
    created: float = field(default_factory=time.time)
    agent_seconds: float = 0.0       # time the agent worked; time spent waiting for the seller is not counted
    busy_since: float | None = None

    def totals(self) -> dict:
        done = [s for s in self.steps if s.status == "done"]
        busy = time.time() - self.busy_since if self.busy_since else 0.0
        return {"cost_usd": round(sum(s.cost_usd for s in self.steps), 6),
                "model_calls": sum(1 for s in done if s.model),
                "seconds": round(self.agent_seconds + busy, 1)}

    def start_work(self) -> None:
        self.busy_since = time.time()

    def stop_work(self) -> None:
        if self.busy_since:
            self.agent_seconds += time.time() - self.busy_since
            self.busy_since = None

    def public(self) -> dict:
        data = asdict(self)
        data["totals"] = self.totals()
        return data


class Agent:
    def __init__(self, client, runs_dir: Path):
        self.client = client
        self.runs_dir = runs_dir
        self.runs: dict[str, Run] = {}

    # ---- bookkeeping ------------------------------------------------------------------------

    def _dir(self, run: Run) -> Path:
        path = self.runs_dir / run.id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _save(self, run: Run) -> None:
        (self._dir(run) / "run.json").write_text(json.dumps(run.public(), ensure_ascii=False, indent=2, default=str),
                                                 encoding="utf-8")

    async def _step(self, run: Run, key: str, title: str, coro, describe_input: str = ""):
        step = Step(key=key, title=title, input=describe_input, mock=self.client.mock)
        run.steps.append(step)
        self._save(run)
        t0 = time.perf_counter()
        try:
            result, meta = await coro
        except Exception as exc:  # the log must show the failure, whatever it is
            step.status, step.error = "failed", f"{type(exc).__name__}: {exc}"[:500]
            step.ms = round((time.perf_counter() - t0) * 1000)
            self._save(run)
            raise
        step.ms = round((time.perf_counter() - t0) * 1000)
        step.status = "done"
        step.model, step.endpoint = meta.get("model", ""), meta.get("endpoint", "")
        step.cost_usd = round(float(meta.get("cost_usd") or 0.0), 6)
        step.output = {"usage": meta.get("usage")}
        if meta.get("raw"):
            step.output["raw"] = meta["raw"]
        self._save(run)
        return result, step

    def _local(self, run: Run, key: str, title: str, describe_input: str, output, status: str = "done") -> Step:
        step = Step(key=key, title=title, status=status, input=describe_input, output=output)
        run.steps.append(step)
        self._save(run)
        return step

    # ---- the agent --------------------------------------------------------------------------

    def create(self, photos: list[bytes], audio: bytes | None, note: str) -> Run:
        if not 1 <= len(photos) <= 3:
            raise ValueError("send 1 to 3 photos")
        if not audio and not note.strip():
            raise ValueError("say or type something about the product")
        run = Run(id=uuid.uuid4().hex[:12], mode="mock" if self.client.mock else "live", note=note.strip())
        run.start_work()
        self.runs[run.id] = run
        folder = self._dir(run)
        t0 = time.perf_counter()
        sizes = []
        for i, raw in enumerate(photos):
            jpeg = compose.normalise_photo(raw)
            (folder / f"photo-{i + 1}.jpg").write_bytes(jpeg)
            run.photos.append(f"photo-{i + 1}.jpg")
            sizes.append(f"{len(raw) // 1024} KB → {len(jpeg) // 1024} KB")
        if audio:
            (folder / "speech.wav").write_bytes(audio)
        step = self._local(run, "prepare", "Prepare photos (local)", f"{len(photos)} photo(s)", {"sizes": sizes})
        step.ms = round((time.perf_counter() - t0) * 1000)
        return run

    def _photos(self, run: Run) -> list[bytes]:
        folder = self._dir(run)
        return [(folder / name).read_bytes() for name in run.photos]

    async def process(self, run: Run) -> Run:
        try:
            await self._process(run)
        except Exception as exc:
            run.status, run.error = "failed", f"{type(exc).__name__}: {exc}"[:500]
        run.stop_work()
        self._save(run)
        return run

    async def _process(self, run: Run) -> None:
        # Muse Image is the slowest call (10-26 s measured) and its neutral room needs nothing from the
        # other steps, so it starts first and runs alongside all of them.
        painting = self.client.generate_image(prompts.STUDIO_BACKGROUND.format(scene=prompts.NEUTRAL_SCENE))
        background = asyncio.create_task(self._step(
            run, "background", "Paint empty studio (no product, no text)", painting, "neutral studio, started first"))
        try:
            await self._listing_and_photo(run, background)
        finally:
            if not background.done():          # an earlier step failed: stop painting a room nobody will see
                background.cancel()
                await asyncio.gather(background, return_exceptions=True)
                painting.close()               # no-op if it ran; otherwise it was never awaited
                for step in run.steps:
                    if step.key == "background" and step.status == "running":
                        step.status, step.error = "skipped", "cancelled: an earlier step failed"

    async def _listing_and_photo(self, run: Run, background: asyncio.Task) -> None:
        folder = self._dir(run)
        photos = self._photos(run)

        # 1. Voice → text
        wav_path = folder / "speech.wav"
        if wav_path.exists():
            run.transcript, step = await self._step(run, "transcribe", "Voice → text",
                                                    self.client.transcribe(wav_path.read_bytes()),
                                                    f"{wav_path.stat().st_size // 1024} KB WAV")
            step.output["transcript"] = run.transcript
        seller_words = " ".join(part for part in (run.transcript, run.note) if part)

        # 2. Photos + words → listing
        raw, step = await self._step(
            run, "listing", "Photos + words → listing (JSON schema)",
            self.client.chat_json(prompts.LISTING, f"Seller said: {seller_words}", photos, "Listing", LISTING_SCHEMA),
            f"{len(photos)} photo(s) + {len(seller_words)} chars")
        try:
            listing = Listing.model_validate(raw)
        except ValidationError as exc:
            step.status, step.error = "failed", f"listing failed validation: {exc.errors()[:3]}"
            raise
        listing.missing = listing.missing_for_sale()
        run.listing = listing.model_dump()
        step.output["listing"] = run.listing

        # 3. In parallel (with the background already painting): verify against the photos and the
        #    seller's words, and cut the product out.
        listing_for_check = {k: v for k, v in run.listing.items() if k not in ("product_phrase_en", "background_prompt_en")}
        verify = self._step(
            run, "verify", "Check listing against photos",
            self.client.chat_json(prompts.VERIFY,
                                  f"Seller said: {seller_words}\n\nListing to check:\n"
                                  + json.dumps(listing_for_check, ensure_ascii=False),
                                  photos, "Verification", VERIFICATION_SCHEMA),
            "listing + seller's words + photos")
        segment = self._step(run, "segment", f"Cut out “{listing.product_phrase_en}”",
                             self.client.segment(photos[0], listing.product_phrase_en),
                             f"photo-1 + “{listing.product_phrase_en}”")
        verified, segmented, painted = await asyncio.gather(verify, segment, background, return_exceptions=True)

        if isinstance(verified, BaseException):
            raise verified
        verification = Verification.model_validate(verified[0])
        run.verification = verification.model_dump()
        verified[1].output["verification"] = run.verification

        # 4. Compose: real product pixels on the painted room. Without a cutout, show the photo as taken.
        if isinstance(segmented, BaseException) or not segmented[0]:
            note = "no product found" if not isinstance(segmented, BaseException) else str(segmented)[:200]
            self._local(run, "compose", "Compose studio photo (local)", "", {"reason": note}, status="skipped")
        else:
            segments, seg_step = segmented
            best = max(segments, key=lambda s: ((s.confidence or 0.0), s.area))
            seg_step.output.update({"matches": len(segments), "picked": best.object_id, "box": best.box,
                                    "mask_pixels": best.area})
            photo = Image.open(io.BytesIO(photos[0])).convert("RGB")
            if isinstance(painted, BaseException):
                room, source = Image.new("RGB", (compose.STUDIO_SIDE,) * 2, (245, 245, 245)), "plain (image step failed)"
            else:
                room, source = Image.open(io.BytesIO(painted[0])), "Muse Image"
                (folder / "background.png").write_bytes(painted[0])
            t0 = time.perf_counter()
            piece = compose.cutout(photo, best.mask)
            piece.save(folder / "cutout.png")
            final = compose.studio(room, piece)
            (folder / "studio.jpg").write_bytes(compose.jpeg(final))
            run.studio_image = "studio.jpg"
            step = self._local(run, "compose", "Compose studio photo (local)", f"cutout + {source} background",
                               {"size": final.size, "product_pixels": "from the seller's photo, scaled only"})
            step.ms = round((time.perf_counter() - t0) * 1000)

        self._decide(run, listing, verification)

    async def restyle(self, run: Run) -> Run:
        """On request: a second Muse Image room, this time described by Spark for this product."""
        folder = self._dir(run)
        if run.status != "ready" or not (folder / "cutout.png").is_file():
            raise ValueError(f"run is {run.status} without a cut-out product; nothing to restyle")
        listing = Listing.model_validate(run.listing)
        run.start_work()
        try:
            image, _ = await self._step(
                run, "restyle", "Paint a studio for this product",
                self.client.generate_image(prompts.STUDIO_BACKGROUND.format(scene=listing.background_prompt_en)),
                listing.background_prompt_en)
            n = sum(1 for s in run.steps if s.key == "restyle")
            (folder / f"background-{n}.png").write_bytes(image)
            final = compose.studio(Image.open(io.BytesIO(image)), Image.open(folder / "cutout.png"))
            (folder / f"studio-{n}.jpg").write_bytes(compose.jpeg(final))
            run.studio_image = f"studio-{n}.jpg"
            run.error = ""
        except Exception as exc:  # the old photo stays; the failed step is in the log
            run.error = f"{type(exc).__name__}: {exc}"[:500]
        finally:
            run.stop_work()
            self._save(run)
        return run

    def _decide(self, run: Run, listing: Listing, verification: Verification) -> None:
        if verification.verdict == "block":
            run.status = "blocked"
        elif listing.missing and not run.asked:
            run.status, run.question, run.asked = "needs_answer", prompts.follow_up_question(listing.missing), True
        else:
            run.status = "ready"

    async def answer(self, run: Run, audio: bytes | None, text: str) -> Run:
        if run.status != "needs_answer":
            raise ValueError(f"run is {run.status}, not waiting for an answer")
        run.start_work()
        try:
            words = text.strip()
            if audio:
                heard, step = await self._step(run, "transcribe-answer", "Voice → text (answer)",
                                               self.client.transcribe(audio), f"{len(audio) // 1024} KB WAV")
                step.output["transcript"] = heard
                words = f"{heard} {words}".strip()
            facts_raw, step = await self._step(
                run, "answer", "Answer → price / stock",
                self.client.chat_json(prompts.SALE_FACTS, f"Question: {run.question}\nAnswer: {words}", [],
                                      "SaleFacts", SALE_FACTS_SCHEMA),
                words)
            facts = SaleFacts.model_validate(facts_raw)
            step.output["facts"] = facts.model_dump()
            listing = Listing.model_validate(run.listing)
            if facts.price_vnd is not None:
                listing.price_vnd = facts.price_vnd
            if facts.stock is not None:
                listing.stock = facts.stock
            listing.missing = listing.missing_for_sale()
            run.listing = listing.model_dump()
            run.status = "ready"     # one question only; anything still missing is typed in on the preview
        except Exception as exc:
            run.status, run.error = "failed", f"{type(exc).__name__}: {exc}"[:500]
        run.stop_work()
        self._save(run)
        return run
