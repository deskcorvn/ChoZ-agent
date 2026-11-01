"""The agent end to end on the mock client: steps, the one follow-up question, blocking, failures."""
import asyncio
import time

import pytest

from app.catalog import Catalog
from app.meta_api import MockMetaClient
from app.pipeline import Agent
from tests.conftest import product_photo, wav_bytes


def agent_for(settings, client=None):
    return Agent(client or MockMetaClient(settings), settings.data_dir / "runs")


def process(agent, run):
    return asyncio.run(agent.process(run))


def test_a_complete_note_goes_straight_to_ready(settings):
    agent = agent_for(settings)
    run = agent.create([product_photo()], None, "Áo thun cotton nam, giá 120 nghìn, còn 50 cái")
    process(agent, run)
    assert run.status == "ready", run.error
    assert run.listing["price_vnd"] == 120000 and run.listing["stock"] == 50
    keys = [s.key for s in run.steps]
    assert keys[0] == "prepare" and keys[-1] == "compose"
    assert sorted(keys[1:-1]) == ["background", "listing", "segment", "verify"]
    assert keys.index("background") < keys.index("verify")      # the slow painter starts early
    assert all(s.status == "done" for s in run.steps)
    assert all(s.mock for s in run.steps if s.model)
    assert (settings.data_dir / "runs" / run.id / "studio.jpg").is_file()


def test_voice_is_transcribed_first(settings):
    agent = agent_for(settings)
    run = agent.create([product_photo()], wav_bytes(), "")
    process(agent, run)
    step = next(s for s in run.steps if s.key == "transcribe")
    assert run.transcript and step.output["usage"]["audio_seconds"] == 1.5


def test_missing_price_asks_exactly_one_question(settings):
    agent = agent_for(settings)
    run = agent.create([product_photo()], None, "Áo thun cotton nam, còn 50 cái")
    process(agent, run)
    assert run.status == "needs_answer"
    assert run.question == "Bạn bán sản phẩm này giá bao nhiêu?"
    asyncio.run(agent.answer(run, None, "120 nghìn"))
    assert run.status == "ready" and run.listing["price_vnd"] == 120000
    assert run.listing["missing"] == []
    with pytest.raises(ValueError):                      # no second question
        asyncio.run(agent.answer(run, None, "130 nghìn"))


def test_time_spent_waiting_for_the_seller_is_not_agent_time(settings):
    agent = agent_for(settings)
    run = agent.create([product_photo()], None, "Áo thun cotton nam, còn 50 cái")
    process(agent, run)
    before = run.totals()["seconds"]
    time.sleep(0.6)
    asyncio.run(agent.answer(run, None, "120 nghìn"))
    assert run.totals()["seconds"] - before < 0.4


def test_an_answer_that_settles_nothing_still_ends_the_questions(settings):
    agent = agent_for(settings)
    run = agent.create([product_photo()], None, "Áo thun cotton nam")
    process(agent, run)
    assert run.question == "Bạn bán giá bao nhiêu và còn bao nhiêu sản phẩm?"
    asyncio.run(agent.answer(run, None, "để tôi xem lại"))
    assert run.status == "ready" and run.listing["missing"] == ["price_vnd", "stock"]


class BlockingClient(MockMetaClient):
    async def chat_json(self, system, text, images, name, schema):
        if name == "Verification":
            await asyncio.sleep(0)          # a real call always yields; a coroutine that never does is not one
            return ({"verdict": "block", "prohibited": True, "prohibited_reason": "replica weapon",
                     "checks": [], "summary_vi": "Không được đăng."}, self._meta("x", "m", {}))
        return await super().chat_json(system, text, images, name, schema)


def test_a_blocked_item_cannot_be_published(settings):
    agent = agent_for(settings, BlockingClient(settings))
    run = agent.create([product_photo()], None, "Súng đồ chơi, giá 300 nghìn, còn 5 cái")
    process(agent, run)
    assert run.status == "blocked"
    with pytest.raises(ValueError, match="blocked"):
        Catalog(settings.data_dir).publish(run, settings.data_dir / "runs" / run.id, {})


class BadListingClient(MockMetaClient):
    async def chat_json(self, system, text, images, name, schema):
        if name == "Listing":
            return {"title": "x"}, self._meta("x", "m", {})
        return await super().chat_json(system, text, images, name, schema)


def test_a_listing_that_fails_validation_fails_the_run_and_the_step(settings):
    agent = agent_for(settings, BadListingClient(settings))
    run = agent.create([product_photo()], None, "áo")
    process(agent, run)
    assert run.status == "failed"
    failed = [s.key for s in run.steps if s.status == "failed"]
    assert failed == ["listing"]


class NoMatchClient(MockMetaClient):
    async def segment(self, jpeg, phrase):
        return [], self._meta("x", "sam-3.1", {})


def test_no_segmentation_match_skips_the_studio_photo_but_not_the_listing(settings):
    agent = agent_for(settings, NoMatchClient(settings))
    run = agent.create([product_photo()], None, "Áo thun, giá 120k, còn 5 cái")
    process(agent, run)
    assert run.status == "ready" and run.studio_image is None
    assert run.steps[-1].key == "compose" and run.steps[-1].status == "skipped"


def test_publish_needs_price_and_stock_and_writes_the_catalog(settings):
    agent = agent_for(settings)
    run = agent.create([product_photo()], None, "Áo thun cotton nam")
    process(agent, run)
    asyncio.run(agent.answer(run, None, "chưa biết"))
    catalog = Catalog(settings.data_dir)
    with pytest.raises(ValueError, match="still missing"):
        catalog.publish(run, settings.data_dir / "runs" / run.id, {})
    item = catalog.publish(run, settings.data_dir / "runs" / run.id, {"price_vnd": 99000, "stock": 3})
    assert item["price_vnd"] == 99000 and run.status == "published"
    assert catalog.items()[0]["id"] == run.id


def test_photo_count_is_one_to_three(settings):
    agent = agent_for(settings)
    with pytest.raises(ValueError):
        agent.create([], None, "áo")
    with pytest.raises(ValueError):
        agent.create([product_photo()] * 4, None, "áo")


def test_category_is_chosen_by_id_and_shown_by_name(settings):
    agent = agent_for(settings)
    run = agent.create([product_photo()], None, "Áo thun cotton nam, giá 120 nghìn, còn 50 cái")
    process(agent, run)
    assert run.listing["category_id"] == "fashion_men" and run.listing["category"] == "Thời trang nam"


def test_an_unknown_category_id_fails_validation():
    from pydantic import ValidationError

    from app.schema import Listing
    with pytest.raises(ValidationError, match="unknown category"):
        Listing.model_validate({"title": "Áo thun", "description": "x", "category_id": "Thời trang nam",
                                "product_phrase_en": "shirt", "background_prompt_en": "studio"})


def test_restyle_paints_a_second_room_and_keeps_the_product(settings):
    agent = agent_for(settings)
    run = agent.create([product_photo()], None, "Áo thun cotton nam, giá 120 nghìn, còn 50 cái")
    process(agent, run)
    asyncio.run(agent.restyle(run))
    assert run.studio_image == "studio-1.jpg" and run.status == "ready" and not run.error
    assert (settings.data_dir / "runs" / run.id / "studio-1.jpg").is_file()
    assert [s.key for s in run.steps].count("restyle") == 1


def test_restyle_needs_a_cut_out_product(settings):
    agent = agent_for(settings, NoMatchClient(settings))
    run = agent.create([product_photo()], None, "Áo thun, giá 120k, còn 5 cái")
    process(agent, run)
    with pytest.raises(ValueError, match="nothing to restyle"):
        asyncio.run(agent.restyle(run))


class SlowBlockingClient(BlockingClient):
    """Verification blocks quickly while the image steps would take long: the agent must not wait for them."""

    async def segment(self, jpeg, phrase):
        await asyncio.sleep(5)
        return await super().segment(jpeg, phrase)

    async def generate_image(self, prompt, size="1024x1024"):
        await asyncio.sleep(5)
        return await super().generate_image(prompt, size)


def test_a_block_cancels_the_image_steps_instead_of_waiting_for_them(settings):
    agent = agent_for(settings, SlowBlockingClient(settings))
    run = agent.create([product_photo()], None, "Thuốc kháng sinh, giá 80 nghìn, còn 30 hộp")
    started = time.monotonic()
    process(agent, run)
    assert run.status == "blocked"
    assert time.monotonic() - started < 3                   # did not sit out the 5 s image calls
    by_key = {s.key: s.status for s in run.steps}
    assert by_key["segment"] == "skipped" and by_key["background"] == "skipped" and by_key["compose"] == "skipped"
    assert run.studio_image is None


def test_empty_product_phrase_falls_back_instead_of_failing_the_run():
    """Live, 1 Nov 2026: Spark left product_phrase_en empty for a prescription antibiotic; the run must still
    reach the verifier (which blocks it) rather than fail validation."""
    from app.schema import Listing

    listing = Listing.model_validate({"title": "Thuốc kháng sinh", "description": "x", "category_id": "fashion_men",
                                      "product_phrase_en": "", "background_prompt_en": "a table"})
    assert listing.product_phrase_en == "product"


def test_a_full_room_pool_is_reused_instead_of_painting(settings):
    """The neutral room is the same for every product: with a full pool no Muse Image call is made for it."""
    from PIL import Image as PILImage

    agent = agent_for(settings)
    pool = agent.runs_dir.parent / "cache" / "neutral-rooms"
    pool.mkdir(parents=True, exist_ok=True)
    for i in range(4):
        PILImage.new("RGB", (64, 64), (230, 230, 230 - i)).save(pool / f"room-{i}.png")
    run = agent.create([product_photo()], None, "Áo thun cotton nam, giá 120 nghìn, còn 50 cái")
    process(agent, run)
    step = next(s for s in run.steps if s.key == "background")
    assert step.model.startswith("cache") and step.cost_usd == 0 and run.studio_image


def test_the_seller_voice_is_deleted_once_it_is_text(settings):
    agent = agent_for(settings)
    run = agent.create([product_photo()], wav_bytes(), "")
    process(agent, run)
    assert not (agent.runs_dir / run.id / "speech.wav").exists() and run.transcript


def test_an_implausible_cut_out_is_not_composed(settings, monkeypatch):
    """SAM returned 200 but the mask covers almost the whole frame: the photo as taken is used, with the reason."""
    import io

    import numpy as np
    from PIL import Image

    from app.meta_api import Segment

    agent = agent_for(settings)

    async def segment(jpeg, phrase):
        img = Image.open(io.BytesIO(jpeg))
        mask = np.ones((img.height, img.width), bool)
        return [Segment("0", (0, 0, img.width, img.height), mask, 0.9)], {"model": "sam-3.1", "endpoint": "x", "cost_usd": 0}

    monkeypatch.setattr(agent.client, "segment", segment)
    run = agent.create([product_photo()], None, "Áo thun cotton nam, giá 120 nghìn, còn 50 cái")
    process(agent, run)
    step = next(s for s in run.steps if s.key == "compose")
    assert step.status == "skipped" and "covers 100.0 %" in step.output["reason"] and not run.studio_image


def test_prices_and_stock_carry_the_sellers_quote_checked_verbatim(settings):
    agent = agent_for(settings)
    run = agent.create([product_photo()], None, "Áo thun cotton nam, giá 120 nghìn, còn 50 cái")
    process(agent, run)
    p = run.listing["provenance"]
    assert p["price_vnd"]["verified"] and "120 nghìn" in p["price_vnd"]["quote"]
    assert p["stock"]["verified"] and p["stock"]["quote"].startswith("còn 50")


def test_a_quote_that_is_not_in_the_sellers_words_is_marked_unverified():
    from app.pipeline import provenance
    from app.schema import Listing

    listing = Listing.model_validate({"title": "Áo thun", "description": "x", "category_id": "fashion_men",
                                      "price_vnd": 120000, "stock": 5, "product_phrase_en": "shirt",
                                      "background_prompt_en": "a table",
                                      "evidence": {"price_quote": "giá 99 nghìn", "stock_quote": "còn 5 cái"}})
    p = provenance(listing, "áo thun giá 120 nghìn, còn 5 cái")
    assert p["price_vnd"]["verified"] is False and p["stock"]["verified"] is True


class DeafClient(MockMetaClient):
    """Transcribe is down (quota); everything else works."""

    async def transcribe(self, wav):
        from app.meta_api import MetaAPIError
        raise MetaAPIError("POST /v1/asr/transcribe", 429, "quota")


def test_voice_failure_degrades_to_typed_words(settings, tmp_path):
    agent = Agent(DeafClient(settings), tmp_path / "runs")
    run = agent.create([product_photo()], wav_bytes(), "Áo thun cotton nam, giá 120 nghìn, còn 5 cái")
    asyncio.run(agent.process(run))
    assert run.status == "ready" and run.listing["price_vnd"] == 120000
    fallback = next(s for s in run.steps if s.key == "fallback")
    assert fallback.status == "warn" and fallback.output["kind"] == "quota"
    assert not (tmp_path / "runs" / run.id / "speech.wav").exists()


def test_voice_failure_without_words_keeps_the_question_open(settings, tmp_path):
    agent = Agent(DeafClient(settings), tmp_path / "runs")
    run = agent.create([product_photo()], None, "Áo thun cotton nam, còn 5 cái")
    asyncio.run(agent.process(run))
    assert run.status == "needs_answer"
    asyncio.run(agent.answer(run, wav_bytes(), ""))
    assert run.status == "needs_answer" and run.steps[-1].key == "fallback"
    asyncio.run(agent.answer(run, None, "120 nghìn"))
    assert run.status == "ready" and run.listing["price_vnd"] == 120000


class PricedClient(MockMetaClient):
    """Mock that charges like the live API, so the per-run ceiling can be hit."""

    def _meta(self, endpoint, model, usage):
        return {**super()._meta(endpoint, model, usage), "cost_usd": 0.01}


def test_a_listing_cannot_spend_past_its_budget(settings, tmp_path, monkeypatch):
    from app import metrics

    monkeypatch.setenv("LISTING_RUN_BUDGET_USD", "0.06")
    agent = Agent(PricedClient(settings), tmp_path / "runs")
    run = agent.create([product_photo()], None, "Áo thun cotton nam, giá 120 nghìn, còn 5 cái")
    asyncio.run(agent.process(run))
    assert run.status == "ready"
    for _ in range(6):                     # a seller (or a stuck client) pressing "restyle" again and again
        asyncio.run(agent.restyle(run))
    paid = [s for s in run.steps if s.status == "done" and s.cost_usd]
    assert run.totals()["cost_usd"] <= 0.06 + 1e-9 and "[budget]" in run.error
    assert sum(s.key == "restyle" for s in paid) < 6
    assert metrics.collect(tmp_path / "runs", 1)["failures"].get("budget", 0) >= 1


def test_an_empty_title_does_not_stop_the_check():
    from app.schema import Listing
    listing = Listing.model_validate({"title": "", "description": "", "category_id": "food_specialty",
                                      "product_phrase_en": "", "background_prompt_en": ""})
    assert listing.title == "Sản phẩm chưa có tên" and listing.product_phrase_en == "product"


def test_a_cached_room_is_not_counted_as_a_model_call(settings, tmp_path):
    from app.pipeline import Run, Step
    run = Run(id="abc", mode="live")
    run.steps = [Step(key="transcribe", title="", model="muse-voice-transcribe-1.0", status="done"),
                 Step(key="background", title="", model="cache (muse-image-1.0)", status="done")]
    assert run.totals()["model_calls"] == 1
