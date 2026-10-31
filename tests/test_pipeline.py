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
