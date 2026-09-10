"""Our own launches — the one thing here tracked because we said so.

Everything else earns attention by clearing a bar, and that filter is exactly
wrong for a coin we launched: ours matters at $4,000 and it matters at zero,
because what we need from it is a post-mortem rather than a verdict. So the
tests that matter are the exemptions — it must survive the prune that drops
everything flat and unloved, and it must be re-priced ahead of the queue that
is explicitly designed to starve tokens that are not doing anything.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.memory import launches, ledger

MINT = "OurMint" + "1" * 37
OTHER = "Other" + "2" * 39


class FakeSearch:
    def __init__(self, results=None):
        self.results = results or []
        self.queries: list[str] = []

    async def search(self, query, max_results=5, recency_days=None, **kwargs):
        self.queries.append(query)
        return self.results


CHECKIN = {
    "observation": "Flat since launch. The theme did not catch and nobody outside is talking about it.",
    "traction": "none",
    "what_is_working": "",
    "what_to_change": "Launch inside the first two hours of a trend, not the second day.",
}


class FakeCompletions:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return type("R", (), {
            "choices": [type("C", (), {"message": type("M", (), {
                "content": json.dumps(self.payload)})()})()],
            "usage": type("U", (), {"prompt_tokens": 300, "completion_tokens": 150})(),
        })()


class FakeRegistry:
    def __init__(self, payload=CHECKIN, search=None):
        self.completions = FakeCompletions(payload)
        self.web_research = search or FakeSearch()

        async def raw_client(_self=None):
            return type("Cl", (), {
                "chat": type("Ch", (), {"completions": self.completions})()
            })()

        self.reasoning = type("R", (), {"raw_client": raw_client})()


@pytest.fixture
def settings():
    from app.config import get_settings

    return get_settings()


class TestRegistering:
    async def test_a_mint_we_have_never_seen_is_still_accepted(self, isolated_memory):
        """A launch from another wallet, or one registered after an outage,
        will not be in the ledger yet."""
        launch = await launches.register(MINT, name="Cat Lawyer", ticker="LAWCAT")

        assert launch.mint == MINT
        assert launches.is_ours(MINT)
        assert ledger.get_sighting(MINT) is not None

    async def test_registering_twice_updates_rather_than_duplicates(self, isolated_memory):
        await launches.register(MINT, ticker="LAWCAT")
        await launches.register(MINT, ticker="LAWCAT", note="second thoughts")

        assert len(launches.listing()) == 1
        assert launches.get(MINT).note == "second thoughts"

    async def test_the_original_launch_date_survives_a_re_register(self, isolated_memory):
        first = await launches.register(MINT, ticker="LAWCAT")
        again = await launches.register(MINT, ticker="LAWCAT", note="later")

        assert again.launched_at == first.launched_at

    async def test_it_opens_a_file_immediately(self, isolated_memory):
        """Before any check-in. The operator should be able to look at it the
        moment they have handed over the CA."""
        from app.memory import service

        await launches.register(MINT, name="Cat Lawyer", ticker="LAWCAT", note="third try")

        memory = service.read(launches.launch_path(MINT))
        assert memory is not None
        assert MINT in memory.body
        assert "third try" in memory.body

    async def test_it_is_findable_by_contract_address(self, isolated_memory):
        from app.memory import index

        await launches.register(MINT, ticker="LAWCAT")

        assert index.by_key(MINT)


class TestTheExemptions:
    async def test_a_flat_launch_of_ours_survives_the_prune(self, isolated_memory):
        """The rule that drops everything unloved after 48h is exactly wrong
        for ours — a launch that went nowhere is the one we most need to keep,
        because that is where the lesson is."""
        from app.memory import db

        old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="seconds")
        await launches.register(MINT, ticker="LAWCAT")
        ledger.record_launch(mint=OTHER, creator="W")
        db.execute("UPDATE sightings SET last_seen = ?", (old,))

        result = ledger.prune(ttl_hours=48)

        assert ledger.get_sighting(MINT) is not None, "our own launch was pruned"
        assert ledger.get_sighting(OTHER) is None
        assert result["sightings_dropped"] == 1

    async def test_ours_is_never_marked_faded(self, isolated_memory):
        from app.memory import db

        old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="seconds")
        await launches.register(MINT, ticker="LAWCAT")
        db.execute("UPDATE sightings SET last_seen = ? WHERE mint = ?", (old, MINT))

        ledger.prune(ttl_hours=48)

        assert ledger.get_sighting(MINT).status != ledger.STATUS_FADED

    async def test_the_prune_still_works_with_no_launches_registered(self, isolated_memory):
        """`AND mint NOT IN ()` is a syntax error in SQLite, not a no-op, so
        the empty case has to be the absence of a clause."""
        from app.memory import db

        old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="seconds")
        ledger.record_launch(mint=OTHER, creator="W")
        db.execute("UPDATE sightings SET last_seen = ?", (old,))

        assert ledger.prune(ttl_hours=48)["sightings_dropped"] == 1

    async def test_ours_is_re_priced_before_everything_else(self, isolated_memory):
        """`due_for_check` is designed to starve tokens that are not doing
        anything, which describes most of our launches most of the time."""
        for i in range(30):
            ledger.record_launch(mint=f"Mint{i:040d}", creator=f"W{i}")
            ledger.record_price(mint=f"Mint{i:040d}", market_cap=80_000, liquidity=20_000)
        await launches.register(MINT, ticker="LAWCAT")

        queue = ledger.due_for_check(5)

        assert queue[0].mint == MINT


class TestTheCheckIns:
    async def test_a_review_appends_to_the_file(self, isolated_memory, settings):
        from app.memory import service

        await launches.register(MINT, name="Cat Lawyer", ticker="LAWCAT")
        await launches.run_reviews(FakeRegistry(), settings)

        body = service.read(launches.launch_path(MINT)).body
        assert "check-in 1" in body
        assert "nobody outside is talking about it" in body
        assert "first two hours of a trend" in body

    async def test_check_ins_accumulate_rather_than_replace(self, isolated_memory, settings):
        """The record is the point. A file that only ever holds the latest
        verdict cannot show how a launch actually went."""
        from app.memory import service

        await launches.register(MINT, ticker="LAWCAT")
        await launches.run_reviews(FakeRegistry(), settings)
        await launches.run_reviews(FakeRegistry(), settings)

        body = service.read(launches.launch_path(MINT)).body
        assert "check-in 1" in body and "check-in 2" in body
        assert launches.get(MINT).checkins == 2

    async def test_it_looks_for_outside_chatter(self, isolated_memory, settings):
        search = FakeSearch()
        await launches.register(MINT, name="Cat Lawyer", ticker="LAWCAT")

        await launches.run_reviews(FakeRegistry(search=search), settings)

        assert search.queries, "it never checked whether anyone was talking about it"
        assert "LAWCAT" in search.queries[0] or "Cat Lawyer" in search.queries[0]

    async def test_the_idea_it_came_from_is_put_in_front_of_her(
        self, isolated_memory, settings
    ):
        """Checking the prediction against the outcome is the single most
        useful thing in the file."""
        from app.memory import ideas

        payload = {
            "read_of_the_market": "cats",
            "ideas": [{
                "name": "Cat Lawyer", "ticker": "LAWCAT", "description": "d",
                "angle": "a cat in a courtroom", "why_now": "third week of cats",
                "evidence": "e", "grounding": "observed", "risk": "saturation",
            }],
            "avoid": [],
        }
        await ideas.record(payload, origin="requested")
        idea_id = ideas.latest()["id"]

        await launches.register(MINT, ticker="LAWCAT", idea_id=idea_id)
        registry = FakeRegistry()
        await launches.run_reviews(registry, settings)

        sent = registry.completions.calls[0]["messages"][1]["content"]
        assert "a cat in a courtroom" in sent
        assert "saturation" in sent

    async def test_a_closed_out_launch_stops_being_reviewed(
        self, isolated_memory, settings
    ):
        await launches.register(MINT, ticker="LAWCAT")
        launches.set_status(MINT, "dead")

        registry = FakeRegistry()
        result = await launches.run_reviews(registry, settings)

        assert result["reviewed"] == 0
        assert registry.completions.calls == []

    async def test_it_speaks_in_her_voice(self, isolated_memory, settings):
        await launches.register(MINT, ticker="LAWCAT")
        registry = FakeRegistry()

        await launches.run_reviews(registry, settings)

        assert "# Voice" in registry.completions.calls[0]["messages"][0]["content"]
