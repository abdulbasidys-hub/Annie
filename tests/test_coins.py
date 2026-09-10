"""Why a coin moved — the research pass.

The system could always answer *what* happened: peaks, timings, which
characteristics are over-represented. It could never answer why, and why is
the only half that transfers to launching something yourself.

Full coverage of every tier-crosser was chosen deliberately over a cheaper
sample, so the tests that matter most here are the ones about not spending
twice: research is keyed by mint and never repeated, the queue drains biggest
first so a backlog loses only the small ones, and a search that finds nothing
still produces an honest record rather than a retry forever.
"""

from __future__ import annotations

import json

import pytest

from app.memory import coins, ledger


def _mint(i: int) -> str:
    return f"Mint{i:040d}"


def _qualify(i: int, peak: float, *, symbol: str = "CAT", name: str = "Quantum Cat"):
    mint = _mint(i)
    ledger.record_launch(mint=mint, creator=f"W{i}", symbol=symbol, name=name)
    ledger.record_price(mint=mint, market_cap=peak, liquidity=90_000, tier=100_000)
    from app.memory import db

    db.execute("UPDATE sightings SET peak_market_cap = ? WHERE mint = ?", (peak, mint))
    return mint


VERDICT = {
    "why_it_moved": "A courtroom sketch clip did twelve million views the same morning.",
    "catalyst": "viral_video",
    "catalyst_detail": "the cat-lawyer courtroom clip",
    "why_now": "The clip peaked overnight and the ticker was unclaimed.",
    "category": "animal - cat",
    "confidence": "high",
    "repeatable": True,
}


class FakeSearch:
    def __init__(self, results=None, fail=False):
        self.results = results or []
        self.fail = fail
        self.queries: list[str] = []

    async def search(self, query, max_results=5, recency_days=None, **kwargs):
        self.queries.append(query)
        if self.fail:
            raise RuntimeError("tavily unreachable")
        return self.results


class FakeCompletions:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = json.dumps(self.payload)
        return type("R", (), {
            "choices": [type("C", (), {"message": type("M", (), {"content": content})()})()],
            "usage": type("U", (), {"prompt_tokens": 400, "completion_tokens": 200})(),
        })()


class FakeRegistry:
    def __init__(self, payload=VERDICT, search=None):
        self.completions = FakeCompletions(payload)
        self.web_research = search or FakeSearch()
        client = type("Cl", (), {
            "chat": type("Ch", (), {"completions": self.completions})()
        })()
        self.reasoning = type("R", (), {"raw_client": lambda _s: _client(client)})()


async def _client(c):
    return c


@pytest.fixture
def settings():
    from app.config import get_settings

    return get_settings()


class TestTheQueue:
    def test_only_qualifiers_are_researched(self, isolated_memory):
        """Thirty thousand launches a day never trade. Researching them would
        be the data dump this system exists to avoid."""
        _qualify(1, 400_000)
        ledger.record_launch(mint=_mint(2), creator="W2", symbol="DUD")

        queue = coins.pending()

        assert [s.mint for s in queue] == [_mint(1)]

    def test_the_biggest_are_researched_first(self, isolated_memory):
        """So a backlog after an outage degrades into "the small ones wait",
        never "the interesting ones were missed"."""
        _qualify(1, 150_000)
        _qualify(2, 9_000_000)
        _qualify(3, 800_000)

        assert [s.mint for s in coins.pending()] == [_mint(2), _mint(3), _mint(1)]

    def test_an_already_researched_coin_leaves_the_queue(self, isolated_memory):
        """The property that makes full coverage affordable: steady-state
        cost is new qualifiers, not qualifiers."""
        mint = _qualify(1, 400_000)
        coins.save(coins.CoinResearch(mint=mint, researched_at="2026-09-10T00:00:00"))

        assert coins.pending() == []

    def test_the_queue_is_bounded(self, isolated_memory):
        for i in range(10):
            _qualify(i, 200_000 + i)

        assert len(coins.pending(limit=4)) == 4


class TestResearchingOne:
    async def test_it_records_the_reason_not_just_the_numbers(
        self, isolated_memory, settings
    ):
        mint = _qualify(1, 400_000)
        registry = FakeRegistry()

        await coins.run_research(registry, settings)

        record = coins.get(mint)
        assert record is not None
        assert "twelve million views" in record.why_it_moved
        assert record.catalyst == "viral_video"
        assert record.category == "animal - cat"
        assert record.repeatable is True

    async def test_it_searches_for_the_token(self, isolated_memory, settings):
        _qualify(1, 400_000, symbol="LAWCAT", name="Cat Lawyer")
        search = FakeSearch()
        registry = FakeRegistry(search=search)

        await coins.run_research(registry, settings)

        assert search.queries, "it never looked anything up"
        assert any("Cat Lawyer" in q or "LAWCAT" in q for q in search.queries)

    async def test_a_failed_search_still_produces_a_record(
        self, isolated_memory, settings
    ):
        """Tavily being down must not leave the coin in the queue forever,
        re-attempted on every cycle at full model cost."""
        mint = _qualify(1, 400_000)
        registry = FakeRegistry(search=FakeSearch(fail=True))

        await coins.run_research(registry, settings)

        assert coins.get(mint) is not None
        assert coins.pending() == []

    async def test_a_nameless_token_is_not_searched_for(
        self, isolated_memory, settings
    ):
        """Searching for "" returns the whole internet. Enrichment names the
        winners; until it has, there is nothing to look up."""
        from app.memory import db

        mint = _qualify(1, 400_000)
        db.execute("UPDATE sightings SET name = NULL, symbol = NULL WHERE mint = ?", (mint,))
        search = FakeSearch()

        await coins.run_research(FakeRegistry(search=search), settings)

        assert search.queries == []

    async def test_the_call_is_bounded(self, isolated_memory, settings):
        """Ninety of these a day. A loose token ceiling here is the
        difference between affordable and not."""
        _qualify(1, 400_000)
        registry = FakeRegistry()

        await coins.run_research(registry, settings)

        call = registry.completions.calls[0]
        assert call["max_completion_tokens"] <= 1000
        assert call["reasoning_effort"] == "none"

    async def test_it_speaks_in_her_voice(self, isolated_memory, settings):
        _qualify(1, 400_000)
        registry = FakeRegistry()

        await coins.run_research(registry, settings)

        system = registry.completions.calls[0]["messages"][0]["content"]
        assert "# Voice" in system


class TestSpendingItOnce:
    async def test_a_second_pass_costs_nothing(self, isolated_memory, settings):
        _qualify(1, 400_000)
        registry = FakeRegistry()

        await coins.run_research(registry, settings)
        before = len(registry.completions.calls)
        await coins.run_research(registry, settings)

        assert len(registry.completions.calls) == before, "it researched the same coin twice"

    async def test_a_new_qualifier_is_picked_up(self, isolated_memory, settings):
        _qualify(1, 400_000)
        registry = FakeRegistry()
        await coins.run_research(registry, settings)

        _qualify(2, 500_000)
        result = await coins.run_research(registry, settings)

        assert result["researched"] == 1


class TestTheCategoriesThatWereMissing:
    async def test_categories_come_from_what_the_coin_was(
        self, isolated_memory, settings
    ):
        """Not from matching a name against a seeded vocabulary, which cannot
        see a theme nobody thought to seed."""
        for i in range(3):
            _qualify(i, 300_000 + i)
        await coins.run_research(FakeRegistry(), settings)

        found = coins.categories()

        assert found[0]["category"] == "animal - cat"
        assert found[0]["coins"] == 3
        assert found[0]["repeatable"] == 3

    async def test_a_coin_with_no_category_is_not_counted(
        self, isolated_memory, settings
    ):
        _qualify(1, 300_000)
        await coins.run_research(FakeRegistry({**VERDICT, "category": ""}), settings)

        assert coins.categories() == []


class TestItReachesTheRestOfTheSystem:
    async def test_the_memory_file_carries_the_reason(
        self, isolated_memory, settings
    ):
        """A contract address pasted into chat should resolve to why it ran,
        not to a row anyone could already read off the ledger."""
        from app.memory import service

        mint = _qualify(1, 400_000)
        await coins.run_research(FakeRegistry(), settings)

        memory = service.read(service.token_path(mint))
        assert memory is not None
        assert "Why it moved" in memory.body
        assert "twelve million views" in memory.body
        assert "viral video" in memory.body

    async def test_the_digest_carries_it_into_her_thinking(
        self, isolated_memory, settings
    ):
        """Otherwise the cycle can only notice that things moved, which is
        how a notebook fills with counts instead of causes."""
        from app.memory import digest

        _qualify(1, 400_000)
        await coins.run_research(FakeRegistry(), settings)

        rendered = digest.build(window_hours=24).render()

        assert "viral video" in rendered
