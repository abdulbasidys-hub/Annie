"""Launch ideas: the daily set, the format, and what it refuses to do.

The daily set is the one thing here that generates without being asked, so the
guard that matters most is the one that stops it: three speculative ideas
produced from an empty ledger would arrive looking exactly like grounded ones,
which is worse than sending nothing.
"""

from __future__ import annotations

import json

import pytest

from app.memory import bootstrap, ideas, index, ledger, service

IDEA = {
    "name": "Cat Lawyer",
    "ticker": "LAWCAT",
    "description": "objection your honour my bags are down bad",
    "image_prompt": "A tabby cat in an ill-fitting grey suit standing behind a "
                    "courtroom bench, one paw raised mid-objection, flat plain "
                    "background, single clear subject, strong silhouette.",
    "image_style": "flat vector, thick outlines, four-colour muted palette",
    "image_avoid": "no gold coins, no rocket, no laser eyes, not photoreal",
    "first_tweet": "your honour i object to my bags being down 94%",
    "tweet_angle": "It is a quotable one-liner that works without knowing the token.",
    "angle": "A cat in a courtroom filing motions on behalf of bag-holders.",
    "why_now": "Cat-adjacent is in its third week and the specific variants clear "
               "at roughly 3x the generic ones.",
    "evidence": "Animal (token) 11 of 14 modified names cleared this week.",
    "grounding": "observed",
    "risk": "Week three is usually where a theme saturates.",
}

PAYLOAD = {
    "read_of_the_market": "Cat-adjacent still running; AI agents saturated.",
    "ideas": [IDEA, {**IDEA, "name": "Unemployed Capybara", "ticker": "NOJOB",
                     "grounding": "inferred"}],
    "avoid": ["AI (token)", "Politics (token)"],
    "grounded_in": {"movers": 10, "signals": 6, "memories": ["playbook/what-worked.md"]},
}


class FakeCompletions:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = json.dumps(self.payload)
        return type(
            "R", (), {
                "choices": [type("C", (), {"message": type("M", (), {"content": content})()})()],
                "usage": type("U", (), {"prompt_tokens": 2100, "completion_tokens": 800})(),
            },
        )()


class FakeRegistry:
    def __init__(self, payload):
        completions = FakeCompletions(payload)
        client = type("Cl", (), {"chat": type("Ch", (), {"completions": completions})()})()
        self.completions = completions

        class Reasoner:
            async def raw_client(self_inner):
                return client

        self.reasoning = Reasoner()


@pytest.fixture
def seeded(isolated_memory):
    bootstrap._seed_files()
    index.reindex_all()
    for i in range(25):
        ledger.record_launch(
            mint=f"Mint{i:040d}", creator=f"W{i % 3}", launchpad="pumpfun",
            symbol="CAT", name="Quantum Cat",
        )
        ledger.record_price(
            mint=f"Mint{i:040d}", market_cap=320_000, liquidity=60_000, tier=250_000
        )


class TestTheDailySet:
    async def test_it_generates_from_what_moved(self, seeded):
        from app.config import get_settings

        registry = FakeRegistry(PAYLOAD)
        result = await ideas.generate_daily(registry, get_settings(), count=3)

        assert result["generated"] == 2
        assert result["grounded_in_movers"] == 20
        assert result["path"].startswith("playbook/ideas-")

    async def test_an_empty_ledger_produces_nothing_rather_than_hunches(self, isolated_memory):
        """Three speculative ideas from no data would arrive looking exactly
        like grounded ones. Sending none is the honest output."""
        from app.config import get_settings

        bootstrap._seed_files()
        registry = FakeRegistry(PAYLOAD)
        result = await ideas.generate_daily(registry, get_settings(), count=3)

        assert result["skipped"] == "nothing moved in the last 24h"
        assert registry.completions.calls == [], "it spent a model call on nothing"
        assert ideas.latest(origin="daily") is None

    async def test_it_is_recorded_both_ways(self, seeded):
        """Structured for the page, prose for the notebook. Reconstructing
        either from the other would be lossy."""
        from app.config import get_settings

        await ideas.generate_daily(FakeRegistry(PAYLOAD), get_settings())

        stored = ideas.latest(origin="daily")
        assert stored["ideas"][0]["ticker"] == "LAWCAT"
        assert stored["origin"] == "daily"

        written = service.read(stored["memory_path"])
        assert written is not None
        assert "Cat Lawyer" in written.body
        assert "objection your honour" in written.body, "the description copy was not kept"

    async def test_a_requested_set_stays_distinguishable(self, seeded):
        from app.config import get_settings

        await ideas.generate_daily(FakeRegistry(PAYLOAD), get_settings())
        await ideas.record(PAYLOAD, origin="requested", brief="something in AI")

        assert ideas.latest(origin="daily")["origin"] == "daily"
        assert ideas.latest(origin="requested")["brief"] == "something in AI"
        # Newest overall is the requested one.
        assert ideas.latest()["origin"] == "requested"

    async def test_history_returns_newest_first(self, seeded):
        for n in range(3):
            await ideas.record({**PAYLOAD, "n": n}, origin="requested")

        history = ideas.history(limit=10)
        assert len(history) == 3
        assert history[0]["generated_at"] >= history[-1]["generated_at"]


class TestTheFormat:
    async def test_an_idea_carries_everything_a_launch_form_asks_for(self, seeded):
        from app.config import get_settings

        registry = FakeRegistry(PAYLOAD)
        result = await ideas.generate(registry, get_settings(), count=2)

        idea = result["ideas"][0]
        for field in ("name", "ticker", "description"):
            assert idea.get(field), f"{field} missing — the form cannot be filled from this"
        for field in ("image_prompt", "image_style", "image_avoid"):
            assert idea.get(field), f"{field} missing — the art cannot be briefed from this"
        for field in ("first_tweet", "tweet_angle"):
            assert idea.get(field), f"{field} missing — there is nothing to post"
        for field in ("why_now", "evidence", "grounding", "risk"):
            assert idea.get(field), f"{field} missing — the reasoning is not checkable"

    async def test_the_schema_requires_the_launch_fields(self, seeded):
        """A model that omits the description would produce an idea nobody can
        act on, so the schema refuses it rather than leaving it blank."""
        required = ideas.IDEA_SCHEMA["properties"]["ideas"]["items"]["required"]
        assert {
            "name", "ticker", "description",
            "image_prompt", "image_style", "image_avoid",
            "first_tweet", "tweet_angle",
        } <= set(required)

    async def test_grounding_is_constrained_to_three_honest_values(self, seeded):
        enum = ideas.IDEA_SCHEMA["properties"]["ideas"]["items"]["properties"]["grounding"]["enum"]
        assert enum == ["observed", "inferred", "speculative"]

    def test_delivery_format_leads_with_the_usable_fields(self):
        text = ideas.format_for_delivery(PAYLOAD, limit=3)

        assert "Cat Lawyer" in text
        assert "$LAWCAT" in text
        assert "objection your honour" in text, "the description is what gets pasted"
        assert "Image:" in text
        assert "your honour i object" in text, "the launch post is the point"
        assert "observed" in text
        assert "Avoiding:" in text

    def test_delivery_of_an_empty_set_is_empty_not_a_header(self):
        assert ideas.format_for_delivery({"ideas": []}) == ""


class TestApiSurface:
    @pytest.fixture
    def client(self, isolated_memory):
        from app.auth import require_auth
        from app.main import app
        from fastapi.testclient import TestClient

        app.dependency_overrides[require_auth] = lambda: {"sub": "test"}
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()

    async def test_latest_is_empty_before_anything_is_generated(self, client, isolated_memory):
        body = client.get("/api/ideas/latest").json()
        assert body["found"] is False
        assert body["ideas"] is None

    async def test_latest_returns_the_daily_set(self, client, seeded):
        await ideas.record(PAYLOAD, origin="daily")

        body = client.get("/api/ideas/latest", params={"origin": "daily"}).json()
        assert body["found"] is True
        assert body["ideas"]["ideas"][0]["ticker"] == "LAWCAT"
        assert body["ideas"]["ideas"][0]["description"]

    async def test_history_is_free_and_local(self, client, seeded):
        from app.memory import db

        await ideas.record(PAYLOAD, origin="daily")
        before = db.counters_today().get("firestore_reads", 0)

        body = client.get("/api/ideas/history", params={"limit": 5}).json()

        assert body["total"] == 1
        assert db.counters_today().get("firestore_reads", 0) == before
