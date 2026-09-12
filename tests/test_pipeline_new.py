"""The rewritten pipeline: stream ingest, the watch loop, signals, reports.

These exercise the paths that used to be the expensive ones. The assertions
worth reading are the ones about *counts of operations*, not just outputs —
ingest touching Firestore zero times, and the watch loop batching its lookups
rather than issuing one per mint — because those are the properties that made
the old design unaffordable and they will not stay true by accident.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.memory import db, ledger, signals
from app.pipeline import stream
from app.providers.types import Provenance, TokenLaunch


def _launch(i: int, creator: str = "Wa11etAAA", name: str | None = None) -> TokenLaunch:
    return TokenLaunch(
        mint=f"Mint{i:040d}",
        provenance=Provenance(
            provider="helius",
            operation="webhook_token_mint",
            observed_at=datetime.now(timezone.utc),
            raw_reference=f"sig{i}",
        ),
        creator_wallet=creator,
        launchpad_slug="pumpfun",
        launched_at=datetime.now(timezone.utc),
        signature=f"sig{i}",
        name=name or f"Quantum Cat {i}",
        symbol=f"QCAT{i}",
    )


class TestStreamIngest:
    def test_a_delivery_costs_no_firestore_operations(self, isolated_memory):
        """The whole reason the webhook was rewritten.

        This path used to do one Firestore read plus one write per event, at
        ~16,000 events a day, against a plan allowing 20,000 writes total.
        """
        stream.ingest_many([_launch(i) for i in range(250)])

        counters = db.counters_today()
        assert counters.get("firestore_writes", 0) == 0
        assert counters.get("firestore_reads", 0) == 0
        assert ledger.stats()["sightings_total"] == 250

    def test_one_bad_event_does_not_lose_the_batch(self, isolated_memory, monkeypatch):
        """A real production failure: a Firestore quota error on one event
        500'd the whole delivery and dropped every other event in it."""
        real = ledger.record_launch
        calls = {"n": 0}

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("simulated storage failure")
            return real(**kwargs)

        monkeypatch.setattr(ledger, "record_launch", flaky)
        result = stream.ingest_many([_launch(i) for i in range(10)])

        assert result.failed == 1
        assert result.new == 9
        assert ledger.stats()["sightings_total"] == 9

    def test_a_redelivered_event_is_counted_as_a_repeat(self, isolated_memory):
        batch = [_launch(i) for i in range(5)]
        stream.ingest_many(batch)
        second = stream.ingest_many(batch)

        assert second.new == 0
        assert second.repeats == 5
        assert ledger.get_creator("Wa11etAAA")["launches"] == 5


class TestWatchLoop:
    async def test_lookups_are_batched_not_per_mint(self, isolated_memory, monkeypatch):
        """900 mints must be ~30 requests, not 900.

        The adapter's own batch endpoint takes 30 comma-separated mints;
        losing that would multiply this loop's API cost thirtyfold without
        changing any output, so it is asserted rather than assumed.
        """
        from app.config import get_settings
        from app.pipeline import watch

        stream.ingest_many([_launch(i) for i in range(90)])

        batches: list[list[str]] = []

        class FakeMarket:
            async def get_quotes(self, mints):
                batches.append(list(mints))
                return {}

        class FakeRegistry:
            market_primary = FakeMarket()

        run = await watch.run_watch(FakeRegistry(), get_settings(), batch_size=90)

        assert len(batches) == 1, "the watch loop issued more than one call for one page"
        assert len(batches[0]) == 90
        assert run.checked == 90
        assert run.unpriced == 90

    async def test_unpriced_mints_are_stamped_so_the_next_pass_moves_on(
        self, isolated_memory, monkeypatch
    ):
        from app.config import get_settings
        from app.pipeline import watch

        stream.ingest_many([_launch(i) for i in range(5)])

        class FakeMarket:
            async def get_quotes(self, mints):
                return {}

        class FakeRegistry:
            market_primary = FakeMarket()

        await watch.run_watch(FakeRegistry(), get_settings(), batch_size=5)
        assert all(s.checks == 1 for s in ledger.due_for_check(10))

    async def test_a_thin_pool_spike_does_not_qualify_a_token(self, isolated_memory):
        """A market cap computed from a pool nobody could exit is not a
        measurement of value. Thin-pool spikes routinely imply eight-figure
        caps on a few hundred dollars of liquidity, and a memory built on one
        is worse than no memory."""
        from app.config import get_settings
        from app.pipeline import watch
        from app.providers.types import MarketQuote, Provenance as P

        stream.ingest_many([_launch(1)])
        mint = f"Mint{1:040d}"

        class FakeMarket:
            async def get_quotes(self, mints):
                return {
                    mint: MarketQuote(
                        mint=mint,
                        provenance=P(provider="dexscreener", operation="pair",
                                     observed_at=datetime.now(timezone.utc)),
                        market_cap=Decimal("9000000"),
                        liquidity_usd=Decimal("400"),  # far below MIN_LIQUIDITY_USD
                    )
                }

        class FakeRegistry:
            market_primary = FakeMarket()

        run = await watch.run_watch(FakeRegistry(), get_settings(), batch_size=5)

        assert run.newly_qualified == []
        assert ledger.get_sighting(mint).qualified_at is None
        # The observation is still recorded — it is a measurement, just not a
        # qualifying one.
        assert ledger.get_sighting(mint).peak_market_cap == 9_000_000

    async def test_a_real_run_qualifies_and_writes_a_memory(self, isolated_memory):
        from app.config import get_settings
        from app.memory import service
        from app.pipeline import watch
        from app.providers.types import MarketQuote, Provenance as P

        stream.ingest_many([_launch(1)])
        mint = f"Mint{1:040d}"

        class FakeMarket:
            async def get_quotes(self, mints):
                return {
                    mint: MarketQuote(
                        mint=mint,
                        provenance=P(provider="dexscreener", operation="pair",
                                     observed_at=datetime.now(timezone.utc)),
                        market_cap=Decimal("450000"),
                        liquidity_usd=Decimal("80000"),
                    )
                }

        class FakeRegistry:
            market_primary = FakeMarket()

        run = await watch.run_watch(FakeRegistry(), get_settings(), batch_size=5)

        assert run.newly_qualified == [mint]
        memory = service.read(service.token_path(mint))
        assert memory is not None, "a qualified token did not get a memory file"
        assert mint in memory.body, "the memory file does not carry the contract address"
        assert "Wa11etAAA" in memory.body, "the memory file does not carry the creator wallet"

        # …and it is findable by either handle, which is the operator's
        # actual requirement.
        from app.memory import index

        assert index.by_key(mint)
        assert index.by_key("Wa11etAAA")



class TestAnOldTokenIsNotANewLaunch:
    """The second layer, and the one that does not depend on Helius labels.

    A token's pair creation date rides the price response we already fetch,
    so this costs nothing. It is what catches an established token that
    reaches the ledger by any route — a relabelled event, a new launchpad,
    the polling backfill — and stops it clearing a tier on a market cap it
    reached three years ago.
    """

    @staticmethod
    def _market(mint: str, *, age_days: float | None):
        from datetime import timedelta

        from app.providers.types import MarketQuote, Provenance as P

        created = (
            None if age_days is None
            else datetime.now(timezone.utc) - timedelta(days=age_days)
        )

        class FakeMarket:
            async def get_quotes(self, mints):
                return {
                    mint: MarketQuote(
                        mint=mint,
                        provenance=P(provider="dexscreener", operation="pair",
                                     observed_at=datetime.now(timezone.utc)),
                        market_cap=Decimal("9000000"),
                        liquidity_usd=Decimal("400000"),
                        pair_created_at=created,
                    )
                }

        class FakeRegistry:
            market_primary = FakeMarket()

        return FakeRegistry()

    async def test_a_three_year_old_token_does_not_qualify(self, isolated_memory):
        from app.config import get_settings
        from app.pipeline import watch

        stream.ingest_many([_launch(1)])
        mint = f"Mint{1:040d}"

        run = await watch.run_watch(
            self._market(mint, age_days=1200), get_settings(), batch_size=5
        )

        assert run.newly_qualified == [], "an established token qualified as a launch"
        assert run.too_old == 1
        assert ledger.get_sighting(mint).qualified_at is None

    async def test_a_token_launched_today_qualifies(self, isolated_memory):
        from app.config import get_settings
        from app.pipeline import watch

        stream.ingest_many([_launch(1)])
        mint = f"Mint{1:040d}"

        run = await watch.run_watch(
            self._market(mint, age_days=0.2), get_settings(), batch_size=5
        )

        assert run.newly_qualified == [mint]
        assert run.too_old == 0

    async def test_a_slow_burner_inside_the_window_still_counts(self, isolated_memory):
        """A token that takes three weeks to run is still a launch."""
        from app.config import get_settings
        from app.pipeline import watch

        stream.ingest_many([_launch(1)])
        mint = f"Mint{1:040d}"

        run = await watch.run_watch(
            self._market(mint, age_days=21), get_settings(), batch_size=5
        )

        assert run.newly_qualified == [mint]

    async def test_an_unknown_age_is_not_treated_as_old(self, isolated_memory):
        """Refusing on missing data would silently drop real launches every
        time the provider omitted the field."""
        from app.config import get_settings
        from app.pipeline import watch

        stream.ingest_many([_launch(1)])
        mint = f"Mint{1:040d}"

        run = await watch.run_watch(
            self._market(mint, age_days=None), get_settings(), batch_size=5
        )

        assert run.newly_qualified == [mint]

    async def test_a_token_that_qualified_before_the_check_is_withdrawn(
        self, isolated_memory
    ):
        """Production already held RAY, Bonk and $WIF as qualified launches.
        The check is about what a token *is*, so those have to stop being
        listed — this is the only place qualification is ever withdrawn."""
        from app.config import get_settings
        from app.memory import db
        from app.pipeline import watch

        stream.ingest_many([_launch(1)])
        mint = f"Mint{1:040d}"
        db.execute(
            "UPDATE sightings SET qualified_at = ?, tier = 100000 WHERE mint = ?",
            ("2026-09-01T00:00:00", mint),
        )

        await watch.run_watch(
            self._market(mint, age_days=1200), get_settings(), batch_size=5
        )

        assert ledger.get_sighting(mint).qualified_at is None

    async def test_a_real_launch_keeps_its_tier_when_the_price_falls(
        self, isolated_memory
    ):
        """Withdrawal is only ever for age. A token that ran and came back
        down genuinely did clear the tier."""
        from app.config import get_settings
        from app.pipeline import watch

        stream.ingest_many([_launch(1)])
        mint = f"Mint{1:040d}"
        await watch.run_watch(
            self._market(mint, age_days=1), get_settings(), batch_size=5
        )
        assert ledger.get_sighting(mint).qualified_at is not None

        from app.providers.types import MarketQuote, Provenance as P

        class Crashed:
            async def get_quotes(self, mints):
                return {mint: MarketQuote(
                    mint=mint,
                    provenance=P(provider="dexscreener", operation="pair",
                                 observed_at=datetime.now(timezone.utc)),
                    market_cap=Decimal("900"), liquidity_usd=Decimal("400000"),
                    pair_created_at=datetime.now(timezone.utc),
                )}

        class Reg:
            market_primary = Crashed()

        await watch.run_watch(Reg(), get_settings(), batch_size=5)

        assert ledger.get_sighting(mint).qualified_at is not None

    async def test_the_price_is_still_recorded_for_an_old_token(
        self, isolated_memory
    ):
        """It is not a launch, but the measurement is still true."""
        from app.config import get_settings
        from app.pipeline import watch

        stream.ingest_many([_launch(1)])
        mint = f"Mint{1:040d}"

        await watch.run_watch(
            self._market(mint, age_days=1200), get_settings(), batch_size=5
        )

        assert ledger.get_sighting(mint).peak_market_cap == 9_000_000


class TestTheMemoryBar:
    """Qualifying and being worth writing about are different questions.

    The $100k qualification floor is correct for statistics — it decides who
    is in the cohort. It is wrong for the notebook. At real Solana volume
    roughly one token a minute clears it, which would be ~42,000 markdown
    files a month, ~1,400 Firestore snapshot writes a day against a 4,000
    budget, and a notebook no person could read. So promotion to a file has
    its own, higher bar.

    Nothing is lost by failing it. The token stays in the ledger, counts in
    every signal, and appears with its contract address and creator in the
    deterministic daily log.
    """

    @staticmethod
    def _market(prices: dict[str, int]):
        from app.providers.types import MarketQuote, Provenance as P

        class FakeMarket:
            async def get_quotes(self, mints):
                return {
                    m: MarketQuote(
                        mint=m,
                        provenance=P(provider="dexscreener", operation="pair",
                                     observed_at=datetime.now(timezone.utc)),
                        market_cap=Decimal(prices[m]),
                        liquidity_usd=Decimal("80000"),
                    )
                    for m in mints if m in prices
                }

        class FakeRegistry:
            market_primary = FakeMarket()

        return FakeRegistry()

    async def test_a_qualifier_below_the_bar_is_counted_but_not_written(self, isolated_memory):
        from app.config import get_settings
        from app.memory import service
        from app.pipeline import watch

        stream.ingest_many([_launch(1)])
        mint = f"Mint{1:040d}"
        settings = get_settings()

        # Comfortably qualified at $120k, comfortably under the $250k
        # memory bar — the shape of the overwhelming majority.
        run = await watch.run_watch(self._market({mint: 120_000}), settings, batch_size=5)

        assert run.newly_qualified == [mint], "it should still be evidence"
        assert run.remembered == []
        assert run.not_remembered == 1
        assert service.read(service.token_path(mint)) is None

    async def test_the_evidence_survives_even_though_the_prose_does_not(self, isolated_memory):
        """The distinction the whole design rests on: not writing about
        something is not the same as not knowing it."""
        from app.config import get_settings
        from app.pipeline import watch

        stream.ingest_many([_launch(1)])
        mint = f"Mint{1:040d}"
        await watch.run_watch(self._market({mint: 120_000}), get_settings(), batch_size=5)

        sighting = ledger.get_sighting(mint)
        assert sighting.qualified_at is not None
        assert sighting.peak_market_cap == 120_000
        assert ledger.qualified_in_window(
            datetime.now(timezone.utc) - timedelta(hours=1), datetime.now(timezone.utc)
        ), "it vanished from the window signals are computed over"

    async def test_a_day_of_exceptional_volume_stops_at_the_cap(self, isolated_memory, monkeypatch):
        """A floor cannot help on a day when a thousand tokens clear it.

        This is the gate that holds when the market does something unusual,
        which is exactly when an ungated system would write the most files.
        """
        from app.config import get_settings
        from app.pipeline import watch

        settings = get_settings()
        monkeypatch.setattr(settings, "max_token_memories_per_day", 4)

        count = 10
        stream.ingest_many([_launch(i) for i in range(count)])
        prices = {f"Mint{i:040d}": 900_000 for i in range(count)}

        run = await watch.run_watch(self._market(prices), settings, batch_size=count)

        assert len(run.newly_qualified) == count
        assert len(run.remembered) == 4
        assert run.not_remembered == count - 4

    async def test_the_cap_is_per_day_not_per_process(self, isolated_memory, monkeypatch):
        """Held in the counters table, which is keyed by UTC day, so a
        restart does not hand the market a fresh budget."""
        from app.config import get_settings
        from app.memory import db
        from app.pipeline import watch

        settings = get_settings()
        monkeypatch.setattr(settings, "max_token_memories_per_day", 2)
        db.counter_add(watch._MEMORY_COUNTER, 2)

        stream.ingest_many([_launch(1)])
        mint = f"Mint{1:040d}"
        run = await watch.run_watch(self._market({mint: 900_000}), settings, batch_size=5)

        assert run.remembered == []
        assert run.not_remembered == 1


class TestSignals:
    def test_characteristics_are_derived_not_stored(self, isolated_memory):
        """Themes come from three short strings via pure functions. The old
        design stored 13-47 feature documents per token and re-read them on
        every trend run, which was the largest single line on the bill."""
        for i in range(30):
            ledger.record_launch(
                mint=f"Mint{i:040d}",
                creator=f"W{i % 3}",
                launchpad="pumpfun",
                symbol="CAT" if i % 2 == 0 else "DOG",
                name="Quantum Cat" if i % 2 == 0 else "Space Dog",
            )
            ledger.record_price(
                mint=f"Mint{i:040d}", market_cap=300_000, liquidity=50_000, tier=250_000
            )

        run = signals.recompute()

        assert run.cohorts >= 1
        assert run.evaluated > 0
        assert db.counters_today().get("firestore_writes", 0) == 0

        found = signals.listing(limit=50, include_thin=True)
        assert any("animal" in (s["value"] or "") or "cat" in (s["name"] or "").lower()
                   for s in found), "no theme signal was produced from the names"

    def test_thin_samples_are_labelled_not_hidden(self, isolated_memory):
        ledger.record_launch(mint=f"Mint{1:040d}", creator="W", symbol="CAT", name="Cat")
        ledger.record_price(mint=f"Mint{1:040d}", market_cap=300_000, liquidity=50_000, tier=250_000)
        signals.recompute()

        assert signals.listing(limit=20, include_thin=False) == []
        thin = signals.listing(limit=20, include_thin=True)
        assert thin and all(s["thin_sample"] for s in thin)

    def test_the_signal_table_is_capped(self, isolated_memory):
        """Without a cap, "keep everything we noticed" reasserts itself one
        row at a time until the table is the old trends collection again."""
        assert signals.MAX_SIGNALS <= 500


class TestReportGeneration:
    async def test_a_report_builds_from_the_ledger(self, isolated_memory):
        from app.reports.generator import generate_daily_report

        now = datetime.now(timezone.utc)
        for i in range(5):
            ledger.record_launch(mint=f"Mint{i:040d}", creator="W", symbol=f"T{i}", name=f"Cat {i}")
            ledger.record_price(
                mint=f"Mint{i:040d}", market_cap=300_000 + i, liquidity=50_000, tier=250_000
            )
        signals.recompute()

        captured = {}

        class FakeRepo:
            async def list_research_tasks(self, **kwargs):
                return [], None

            async def list_research_notes(self, **kwargs):
                return []

            async def data_quality_since(self, *args):
                return []

            async def upsert_report(self, report):
                captured["report"] = report
                return report

        report = await generate_daily_report(
            FakeRepo(), period_start=now - timedelta(hours=24), period_end=now + timedelta(minutes=1)
        )

        assert report.tokens_qualified == 5
        assert report.headline_finding, "a report with five qualified tokens had no headline"
        assert report.counts_by_tier == {"250000": 5}
        assert "Daily digest" in report.markdown


class TestNarrativeClustering:
    async def test_clustering_reads_the_ledger(self, isolated_memory):
        from app.narratives.cluster import run_narrative_clustering

        for i in range(10):
            ledger.record_launch(
                mint=f"Mint{i:040d}", creator="W", symbol="DOGE", name="Doge Coin Supreme"
            )
            ledger.record_price(
                mint=f"Mint{i:040d}", market_cap=300_000, liquidity=50_000, tier=250_000
            )

        upserted = []

        class FakeRepo:
            async def upsert_narrative(self, narrative):
                upserted.append(narrative)

        run = await run_narrative_clustering(FakeRepo(), min_emergent_count=3)

        assert run.qualified_tokens_scanned == 10
        assert upserted, "clustering produced no narratives from a clearly themed cohort"
        assert any(n.slug == "animal" for n in upserted), "the seeded animal theme was not matched"
