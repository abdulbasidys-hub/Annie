"""The HTTP surface: memory browsing, search, the ledger, signals, cost.

Uses FastAPI's TestClient against the real app with auth dependency-overridden,
so these exercise the actual routes and their real handlers — not a mock of
them. What they lock in is mostly shape: the frontend and the `memory_pull`
script both depend on these payloads, and a silently renamed field is the kind
of break that only shows up in a browser.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(isolated_memory):
    from app.auth import require_auth
    from app.main import app

    app.dependency_overrides[require_auth] = lambda: {"sub": "test"}
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def seeded(isolated_memory):
    """A small but realistic memory + ledger."""
    import asyncio

    from app.memory import bootstrap, index, ledger, service

    bootstrap._seed_files()

    mint = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
    wallet = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
    ledger.record_launch(
        mint=mint, creator=wallet, launchpad="pumpfun", symbol="MOON", name="Moon Cat"
    )
    ledger.record_price(mint=mint, market_cap=420_000, liquidity=60_000, tier=250_000)

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        service.write(
            "tokens/moon.md",
            body=f"Moon Cat ran to $420k.\n\n- CA: `{mint}`\n- Creator: `{wallet}`",
            title="Moon Cat",
            keys=[mint, wallet, "moon"],
            tags=["token", "cat"],
            mirror=False,
        )
    )
    index.reindex_all()
    return {"mint": mint, "wallet": wallet}


class TestMemoryBrowsing:
    def test_the_tree_lists_every_section(self, client, seeded):
        body = client.get("/api/memory").json()
        names = [s["name"] for s in body["sections"]]
        assert "core" in names and "creators" in names and "playbook" in names
        assert body["total_files"] > 0
        assert "durability" in body

    def test_a_file_comes_back_whole(self, client, seeded):
        body = client.get("/api/memory/file", params={"path": "core/market-model.md"}).json()
        assert body["path"] == "core/market-model.md"
        assert "body" in body

    def test_raw_returns_markdown_not_json(self, client, seeded):
        response = client.get(
            "/api/memory/file", params={"path": "core/market-model.md", "raw": True}
        )
        assert response.headers["content-type"].startswith("text/markdown")
        assert response.text.startswith("---")

    def test_a_traversal_path_is_refused(self, client, seeded):
        assert client.get("/api/memory/file", params={"path": "../../.env"}).status_code == 422

    def test_a_missing_file_is_404_not_500(self, client, seeded):
        assert client.get(
            "/api/memory/file", params={"path": "notes/nope.md"}
        ).status_code == 404

    def test_write_then_read_roundtrips(self, client, seeded):
        created = client.post(
            "/api/memory/file",
            json={"path": "notes/by-hand.md", "body": "Written by the operator.",
                  "title": "By hand", "keys": ["handmade"]},
        )
        assert created.status_code == 200
        found = client.get("/api/memory/search", params={"q": "handmade"}).json()
        assert "notes/by-hand.md" in [h["path"] for h in found["hits"]]

    def test_delete_removes_it_from_search_too(self, client, seeded):
        client.post(
            "/api/memory/file",
            json={"path": "notes/temp.md", "body": "Temporary thought.", "keys": ["tempkey"]},
        )
        assert client.delete("/api/memory/file", params={"path": "notes/temp.md"}).status_code == 204
        assert client.get("/api/memory/search", params={"q": "tempkey"}).json()["hits"] == []


class TestSearchSurface:
    def test_a_contract_address_resolves_by_key(self, client, seeded):
        body = client.get("/api/memory/search", params={"q": seeded["mint"]}).json()
        assert body["matched_by"] == "key"
        assert "tokens/moon.md" in [h["path"] for h in body["hits"]]

    def test_a_creator_wallet_resolves_by_key(self, client, seeded):
        body = client.get("/api/memory/search", params={"q": seeded["wallet"]}).json()
        assert body["hits"], "a wallet written into memory was not findable by it"

    def test_a_fuzzy_question_falls_back_to_text(self, client, seeded):
        body = client.get("/api/memory/search", params={"q": "moon cat ran"}).json()
        assert body["hits"]


class TestDigestStaysSmall:
    def test_the_cycle_prompt_is_bounded(self, client, seeded):
        """The claim that a cycle is cheap, asserted rather than asserted-in-prose.

        4,000 tokens is roughly a cent per cycle. If a change pushes the
        digest past this, the cost model of the whole rewrite has changed and
        that should fail loudly here rather than show up on a bill.
        """
        body = client.get("/api/memory/digest", params={"window_hours": 24}).json()
        assert body["approx_input_tokens"] < 4000, (
            f"cycle prompt grew to ~{body['approx_input_tokens']} tokens"
        )
        assert "facts" in body and "prompt" in body

    def test_a_quiet_window_would_not_call_the_model(self, client, isolated_memory):
        body = client.get("/api/memory/digest", params={"window_hours": 1}).json()
        assert body["would_call_model"] is False


class TestLedgerSurface:
    def test_stats_reports_both_activity_and_cost(self, client, seeded):
        body = client.get("/api/ledger/stats").json()
        assert body["ledger"]["qualified_total"] == 1
        assert body["firestore"]["write_budget"] > 0
        assert "stream" in body

    def test_creator_detail_carries_movements_and_dossier_slot(self, client, seeded):
        body = client.get(f"/api/ledger/creators/{seeded['wallet']}").json()
        assert body["creator"]["wallet"] == seeded["wallet"]
        assert body["movements"], "creator movements were not recorded"

    def test_token_detail_joins_ledger_and_memory(self, client, seeded):
        body = client.get(f"/api/ledger/token/{seeded['mint']}").json()
        assert body["sighting"]["peak_market_cap"] == 420_000
        assert body["memory"]["path"] == "tokens/moon.md"

    def test_an_unknown_mint_is_404(self, client, seeded):
        assert client.get("/api/ledger/token/NotARealMintAtAll").status_code == 404


class TestCatalogueReadsTheLedger:
    def test_tokens_lists_what_moved(self, client, seeded):
        body = client.get("/api/tokens", params={"hours": 24}).json()
        assert [t["mint"] for t in body["items"]] == [seeded["mint"]]
        assert body["items"][0]["themes"], "themes should be derived on read"

    def test_creators_lists_the_wallet(self, client, seeded):
        body = client.get("/api/creators").json()
        assert seeded["wallet"] in [c["wallet"] for c in body["items"]]


class TestCostVisibility:
    def test_the_cost_report_names_where_spend_is(self, client, seeded):
        body = client.get("/api/system/cost").json()
        assert body["firestore"]["spark_plan_daily_caps"]["writes"] == 20000
        assert body["firestore"]["writes"] <= body["firestore"]["write_budget"]
        assert "durability" in body
        assert isinstance(body["jobs"], list)


class TestSignalShapeForTheFrontend:
    """The Trends pages and the dashboard render a specific shape.

    Notably the nested `recent`/`baseline` objects: `<Sample>` is the only
    sanctioned way to display a rate on the frontend and it needs the
    denominator alongside the value, which is what stops "100% of winners
    were cat-themed" being shown without the "…out of 3" that makes it
    meaningless. Flattening these would silently re-open that.
    """

    @pytest.fixture
    def with_signals(self, isolated_memory):
        from app.memory import ledger, signals

        for i in range(25):
            ledger.record_launch(
                mint=f"Mint{i:040d}", creator=f"W{i % 4}", launchpad="pumpfun",
                symbol="CAT", name="Quantum Cat",
            )
            ledger.record_price(
                mint=f"Mint{i:040d}", market_cap=300_000, liquidity=50_000, tier=250_000
            )
        signals.recompute()

    def test_a_listed_signal_carries_its_denominator(self, client, with_signals):
        body = client.get("/api/trends", params={"include_low_confidence": True}).json()
        assert body["items"], "no signals were listed"
        first = body["items"][0]
        assert "recent" in first and "total" in first["recent"]
        assert first["recent"]["total"] is not None
        assert "baseline" in first

    def test_signals_and_trends_return_the_same_shape(self, client, with_signals):
        trends = client.get("/api/trends", params={"include_low_confidence": True}).json()
        signals_route = client.get(
            "/api/signals", params={"include_thin": True}
        ).json()
        assert trends["items"], "the /api/trends alias returned nothing"
        assert signals_route["items"], "the /api/signals route returned nothing"

    def test_detail_includes_the_series_and_related_memory(self, client, with_signals):
        listed = client.get("/api/trends", params={"include_low_confidence": True}).json()
        slug = listed["items"][0]["slug"]
        detail = client.get(f"/api/trends/{slug}").json()
        assert detail["slug"] == slug
        assert "recent_series" in detail
        assert "related_memories" in detail

    def test_the_front_page_is_her_read_not_a_count(self, client, with_signals):
        """/api/today replaced /api/dashboard when the product stopped being a
        database. The shape is the argument: her latest read and her core
        files lead; scale is one field at the bottom.

        The repo is stubbed because the only Firestore this endpoint touches
        is a single query for the sidebar's research badge — everything that
        makes the page is local.
        """
        from app.db.repo import get_repo
        from app.main import app

        class StubRepo:
            async def list_research_tasks(self, **kwargs):
                return [], None

        app.dependency_overrides[get_repo] = lambda: StubRepo()
        try:
            body = client.get("/api/today", params={"window_hours": 24}).json()
        finally:
            app.dependency_overrides.pop(get_repo, None)

        # What she thinks comes first-class...
        assert "latest_read" in body
        assert "whats_working" in body
        assert "market_model" in body
        assert body["whats_working"]["path"] == "core/whats-working.md"

        # ...and the evidence backs it, capped. A front page shows what
        # moved, not everything held — the cap is the design, not a limit
        # that happened to be hit.
        assert 0 < len(body["movers"]) <= 12
        assert body["signals"], "no signals surfaced on the front page"
        assert body["watching"]["creators"] is not None

        # Scale is present but demoted to one small object, not the headline.
        assert body["scale"]["qualified_24h"] == 25
        assert body["scale"]["seen_24h"] == 25

        # And the shell's two fields ride along, so it needs no second call.
        assert "research_pending" in body
        assert "data_freshness" in body

    def test_the_front_page_says_why_when_there_is_nothing(self, client, isolated_memory):
        """An empty deployment must get a cause, not a page of zeros."""
        from app.db.repo import get_repo
        from app.main import app

        class StubRepo:
            async def list_research_tasks(self, **kwargs):
                return [], None

        app.dependency_overrides[get_repo] = lambda: StubRepo()
        try:
            body = client.get("/api/today").json()
        finally:
            app.dependency_overrides.pop(get_repo, None)

        assert body["pipeline"]["state"] == "never_started"
        assert body["pipeline"]["headline"]
        assert body["pipeline"]["what_to_check"]
        assert body["latest_read"]["headline"] is None
