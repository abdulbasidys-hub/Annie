"""Naming the winners.

Every token arrives nameless. The Helius webhook payload carries a mint, a
fee payer and a signature — no name, no symbol — so a name is always a second
lookup, and the system deliberately only pays for it on tokens that actually
cleared a tier.

That lookup was doing one request per mint, 25 mints per six-hour cycle: 100
names a day against roughly 750 qualifiers. It fell behind on the first
afternoon and could never catch up, so every page read "Unnamed", the digest
handed the model mint addresses instead of names, and theme signals were
computed over empty strings — the engine's whole input is names.

The adapter had a batch method the entire time.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.memory import db, ledger
from app.pipeline import watch
from app.providers.types import Provenance, TokenMetadata


def _qualified(n: int, *, prefix: str = "Mint") -> list[str]:
    mints = []
    for i in range(n):
        mint = f"{prefix}{i:040d}"[:44]
        ledger.record_launch(mint=mint, creator=f"W{i}", launchpad="pumpfun")
        ledger.record_price(mint=mint, market_cap=300_000, liquidity=50_000, tier=250_000)
        mints.append(mint)
    return mints


def _metadata(mint: str, name: str, symbol: str) -> TokenMetadata:
    return TokenMetadata(
        mint=mint,
        provenance=Provenance(
            provider="helius", operation="get_asset", observed_at=datetime.now(timezone.utc)
        ),
        name=name,
        symbol=symbol,
    )


class FakeBlockchain:
    """Records how it was asked, not just what it was asked."""

    def __init__(self, known: dict[str, tuple[str, str]] | None = None, fail: bool = False):
        self.known = known or {}
        self.fail = fail
        self.batch_calls: list[list[str]] = []
        self.single_calls: list[str] = []

    async def get_token_metadata_batch(self, mints):
        if self.fail:
            raise RuntimeError("helius unreachable")
        self.batch_calls.append(list(mints))
        return {
            m: _metadata(m, *self.known[m]) for m in mints if m in self.known
        }

    async def get_token_metadata(self, mint):
        self.single_calls.append(mint)
        return None


class FakeRegistry:
    def __init__(self, blockchain):
        self.blockchain = blockchain


@pytest.fixture
def settings():
    from app.config import get_settings

    s = get_settings()
    assert s.is_available("blockchain") or True
    return s


@pytest.fixture(autouse=True)
def _blockchain_available(monkeypatch):
    from app.config import Settings

    monkeypatch.setattr(Settings, "is_available", lambda self, cap: True)


class TestItAsksInBatches:
    async def test_three_hundred_names_is_a_handful_of_requests(
        self, isolated_memory, settings
    ):
        """The regression. Per-mint requests are why this could not keep up
        with a market producing ~750 qualifiers a day."""
        mints = _qualified(120)
        chain = FakeBlockchain({m: (f"Token {i}", f"TK{i}") for i, m in enumerate(mints)})

        result = await watch.enrich_qualified(FakeRegistry(chain), settings)

        assert result["enriched"] == 120
        assert chain.single_calls == [], "it went back to one request per mint"
        assert len(chain.batch_calls) == 1, "the batch was split unnecessarily"

    async def test_the_names_actually_land_on_the_rows(self, isolated_memory, settings):
        mints = _qualified(3)
        chain = FakeBlockchain({m: (f"Quantum Cat {i}", f"QCAT{i}") for i, m in enumerate(mints)})

        await watch.enrich_qualified(FakeRegistry(chain), settings)

        first = ledger.get_sighting(mints[0])
        assert first.name == "Quantum Cat 0"
        assert first.symbol == "QCAT0"

    async def test_it_only_names_tokens_that_cleared_a_tier(self, isolated_memory, settings):
        """Naming the thousands that never traded is the cost this system
        exists to avoid."""
        _qualified(2)
        ledger.record_launch(mint="Mint" + "9" * 40, creator="W", launchpad="pumpfun")
        chain = FakeBlockchain()

        await watch.enrich_qualified(FakeRegistry(chain), settings)

        assert len(chain.batch_calls[0]) == 2
        assert not any(m.startswith("Mint999") for m in chain.batch_calls[0])


class TestTokensItCannotResolve:
    async def test_they_are_stamped_so_they_stop_being_reselected(
        self, isolated_memory, settings
    ):
        """Otherwise the permanently-unresolvable sit at the head of the
        queue and crowd out real winners on every pass — the same failure
        `mark_checked` prevents in the pricing loop."""
        mints = _qualified(3)
        chain = FakeBlockchain({mints[0]: ("Known", "KNW")})

        first = await watch.enrich_qualified(FakeRegistry(chain), settings)
        second = await watch.enrich_qualified(FakeRegistry(chain), settings)

        assert first["enriched"] == 1 and first["unresolvable"] == 2
        assert second == {"enriched": 0}, "it asked about the same dead mints again"

    async def test_a_provider_outage_does_not_stamp_anything(
        self, isolated_memory, settings
    ):
        """An outage is not evidence about any particular mint. Stamping here
        would permanently skip everything in the batch."""
        mints = _qualified(3)

        failed = await watch.enrich_qualified(FakeRegistry(FakeBlockchain(fail=True)), settings)

        assert failed["enriched"] == 0
        chain = FakeBlockchain({m: ("Recovered", "REC") for m in mints})
        assert (await watch.enrich_qualified(FakeRegistry(chain), settings))["enriched"] == 3

    async def test_an_already_named_token_is_not_looked_up_again(
        self, isolated_memory, settings
    ):
        mints = _qualified(2)
        db.execute("UPDATE sightings SET name = 'Already' WHERE mint = ?", (mints[0],))
        chain = FakeBlockchain()

        await watch.enrich_qualified(FakeRegistry(chain), settings)

        assert chain.batch_calls[0] == [mints[1]]


class TestTheColumnsThisNeeds:
    def test_they_are_added_to_a_database_that_predates_them(self, isolated_memory):
        """`CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so a
        new column reaches a fresh deployment and never reaches the one with
        the data in it — which on a mounted volume is the only one that
        matters."""
        conn = db.connect()
        conn.execute("DROP TABLE IF EXISTS probe")
        conn.execute("CREATE TABLE probe (mint TEXT PRIMARY KEY)")
        db._ADDED_COLUMNS["probe"] = {"added_later": "TEXT"}
        try:
            db._add_missing_columns(conn)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(probe)")}
            assert "added_later" in cols

            db._add_missing_columns(conn)  # idempotent — runs on every boot
            assert len([r for r in conn.execute("PRAGMA table_info(probe)")]) == 2
        finally:
            db._ADDED_COLUMNS.pop("probe", None)
            conn.execute("DROP TABLE IF EXISTS probe")

    def test_sightings_has_both_tracking_columns(self, isolated_memory):
        cols = {r[1] for r in db.connect().execute("PRAGMA table_info(sightings)")}

        assert "metadata_checked_at" in cols
        assert "deployer_checked_at" in cols
