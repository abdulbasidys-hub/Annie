"""The watch loop — deciding what to re-price, and what it costs.

Ingest is free; *watching* is the part with a real bill, because it means
asking a market-data provider what something is worth. The old pipeline did
that one mint at a time and, worse, wrote the answer to Firestore whether or
not the answer was interesting. This does neither.

Three things make it cheap:

1. **Batched lookups.** DexScreener accepts 30 comma-separated mints per
   request, and the adapter already exposes that as ``get_quotes``. Nine
   hundred mints is 30 HTTP requests, not 900 — at the adapter's 4 req/s
   limit, about eight seconds.
2. **Prioritised, not exhaustive.** :func:`app.memory.ledger.due_for_check`
   orders by tracked-creator first, then already-moving, then never-checked,
   then longest-since-checked. Anything flat and unloved falls off the end
   and is pruned within 48 hours.
3. **Local writes.** Results land in SQLite. Firestore is touched zero times
   by this loop.

The escalation rule is where intelligence starts: a token that crosses a tier
stops being a row and becomes a memory (a file with its CA and creator), and
its creator gets re-examined for tracking. That is the only point at which
anything from the stream earns durable attention — which is what a person
watching this market does, and what the previous "store everything, decide
later" design never did.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import structlog

from app.config import Settings
from app.memory import ledger
from app.pipeline.qualification import DEFAULT_TIERS, MIN_LIQUIDITY_USD, tier_for
from app.providers.registry import ProviderRegistry

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class WatchRun:
    started_at: datetime
    finished_at: datetime | None = None
    checked: int = 0
    priced: int = 0
    unpriced: int = 0
    newly_qualified: list[str] = field(default_factory=list)
    new_peaks: int = 0
    #: Qualifiers that earned a memory file, and how many did not. The second
    #: number is the interesting one at real volume — it is the filtering.
    remembered: list[str] = field(default_factory=list)
    not_remembered: int = 0
    promoted_creators: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "priced": self.priced,
            "unpriced": self.unpriced,
            "newly_qualified": self.newly_qualified[:25],
            "qualified_count": len(self.newly_qualified),
            "remembered": len(self.remembered),
            "not_remembered": self.not_remembered,
            "new_peaks": self.new_peaks,
            "promoted_creators": self.promoted_creators[:10],
            "errors": self.errors[:5],
        }


async def run_watch(
    registry: ProviderRegistry,
    settings: Settings,
    *,
    batch_size: int | None = None,
    now: datetime | None = None,
) -> WatchRun:
    """Re-price the highest-priority slice of the watchlist.

    Runs frequently (every ~10 minutes). Frequency is what actually matters
    for this market: a Pump.fun token's whole run can happen inside an hour,
    so a check that lands once a day almost never lands inside the window
    where the token was interesting. Making each pass cheap is what makes
    running it often affordable.
    """
    now = now or datetime.now(timezone.utc)
    run = WatchRun(started_at=now)
    limit = batch_size or settings.watch_batch_size

    candidates = ledger.due_for_check(limit)
    if not candidates:
        run.finished_at = datetime.now(timezone.utc)
        return run

    run.checked = len(candidates)
    mints = [c.mint for c in candidates]

    try:
        quotes = await registry.market_primary.get_quotes(mints)
    except Exception as exc:
        run.errors.append(str(exc)[:200])
        run.finished_at = datetime.now(timezone.utc)
        log.warning("watch_batch_failed", error=str(exc), mints=len(mints))
        return run

    unpriced: list[str] = []
    for mint in mints:
        quote = quotes.get(mint)
        if quote is None:
            unpriced.append(mint)
            continue

        market_cap = quote.market_cap or quote.fully_diluted_valuation
        liquidity = quote.liquidity_usd

        # The same guard qualification has always applied: a market cap
        # computed from a pool nobody could exit is not a measurement of
        # value. Thin-pool spikes routinely imply eight-figure caps on $400
        # of liquidity, and a memory built on those is worse than no memory.
        tier: Decimal | None = None
        if market_cap is not None and (liquidity or Decimal(0)) >= MIN_LIQUIDITY_USD:
            tier = tier_for(market_cap, DEFAULT_TIERS)

        outcome = ledger.record_price(
            mint=mint,
            market_cap=market_cap,
            liquidity=liquidity,
            volume_24h=quote.volume_24h_usd,
            tier=tier,
        )
        run.priced += 1
        if outcome["new_peak"]:
            run.new_peaks += 1
        if outcome["newly_qualified"]:
            run.newly_qualified.append(mint)

    if unpriced:
        # Most launches never get a tradeable pair at all. Stamping them is
        # what stops `due_for_check` (which orders by last_checked) handing
        # back the same dead mints on every single pass forever.
        ledger.mark_checked(unpriced)
        run.unpriced = len(unpriced)

    for mint in run.newly_qualified:
        await _escalate(mint, run, settings)

    run.finished_at = datetime.now(timezone.utc)
    log.info("watch_run_complete", **run.to_dict())
    return run


#: Counter name for the daily token-memory budget, in the local counters table.
_MEMORY_COUNTER = "token_memories_written"


def _earns_a_memory(sighting, settings) -> tuple[bool, str]:
    """Should this token get its own page in the notebook?

    Two gates, and they answer different questions.

    The **tier floor** asks "is this notable at all". At real Solana volume
    roughly one token a minute clears $100k, so the qualification floor —
    correct for deciding cohort membership in the statistics — would make a
    memory file for every one of them: ~42,000 files a month, and a notebook
    nobody could read.

    The **daily cap** asks "has today already been exceptional". A floor
    cannot help on a day when a thousand tokens clear it. This can.

    Failing either is not data loss. The token stays in the ledger, counts in
    every signal, is reachable by contract address from chat and the API, and
    is named in the daily log if it was among the day's biggest. It just does
    not get prose written about it.
    """
    from app.memory import db

    peak = sighting.peak_market_cap or 0
    floor = float(settings.memory_tier_usd)
    if peak < floor:
        return False, f"peak ${peak:,.0f} is below the ${floor:,.0f} memory bar"

    written = db.counter_get(_MEMORY_COUNTER)
    cap = int(settings.max_token_memories_per_day)
    if written >= cap:
        return False, f"already wrote {written} token memories today (cap {cap})"

    return True, ""


async def _escalate(mint: str, run: WatchRun, settings) -> None:
    """A token cleared a tier — decide whether it becomes a memory.

    This is the promotion boundary. Below it, a token is a row that gets
    pruned. Above it, a markdown file carrying its contract address and
    creator wallet, indexed so either can be looked up directly from chat.

    The creator's dossier is refreshed whenever a *tracked* wallet produces a
    qualifier, regardless of the token's own bar — a wallet's record is the
    subject there, and one more winner changes it whether or not that
    particular token was remarkable.
    """
    from app.memory import db
    from app.memory.rollup import update_creator_dossier, write_token_memory

    sighting = ledger.get_sighting(mint)
    if sighting is None:
        return

    earns, why_not = _earns_a_memory(sighting, settings)
    if earns:
        try:
            await write_token_memory(mint)
            db.counter_add(_MEMORY_COUNTER)
            run.remembered.append(mint)
        except Exception:
            log.warning("token_memory_write_failed", mint=mint, exc_info=True)
    else:
        run.not_remembered += 1
        log.debug("token_not_remembered", mint=mint, reason=why_not)

    if not sighting.creator:
        return

    creator = ledger.get_creator(sighting.creator)
    if creator and creator.get("tracked"):
        try:
            await update_creator_dossier(
                sighting.creator,
                reason=f"produced {sighting.symbol or mint[:8]}, which cleared a tier",
            )
            run.promoted_creators.append(sighting.creator)
        except Exception:
            log.warning("creator_dossier_failed", wallet=sighting.creator, exc_info=True)


async def enrich_qualified(
    registry: ProviderRegistry, settings: Settings, *, limit: int = 25
) -> dict[str, Any]:
    """Fill in on-chain metadata for qualified tokens that are missing it.

    Only qualified tokens, only the ones still missing a name — a bounded
    handful per cycle rather than the old "resolve metadata and the real
    deployer wallet for everything" pass. Metadata matters because the
    signals engine derives themes from names, so a winner with no name is a
    winner that teaches nothing; it does not matter at all for the thousands
    that never traded.

    A failed creator lookup means "unknown", never a fallback wallet. The
    creator-tracking model depends on that being the real deployer.
    """
    if not settings.is_available("blockchain"):
        return {"skipped": "helius not configured"}

    from app.memory import db

    rows = db.query(
        """
        SELECT mint FROM sightings
         WHERE qualified_at IS NOT NULL AND (name IS NULL OR name = '')
         ORDER BY qualified_at DESC LIMIT ?
        """,
        (limit,),
    )
    if not rows:
        return {"enriched": 0}

    enriched = failed = 0
    for row in rows:
        mint = row["mint"]
        try:
            metadata = await registry.blockchain.get_token_metadata(mint)
            if metadata is None:
                continue
            db.execute(
                "UPDATE sightings SET name = COALESCE(?, name), symbol = COALESCE(?, symbol) "
                "WHERE mint = ?",
                (metadata.name, metadata.symbol, mint),
            )
            enriched += 1
        except Exception:
            failed += 1
            log.info("qualified_enrichment_failed", mint=mint, exc_info=True)

        try:
            wallet = await registry.blockchain.get_creator_wallet(mint)
        except Exception:
            wallet = None
        if wallet:
            existing = db.query_one("SELECT creator FROM sightings WHERE mint = ?", (mint,))
            if existing and existing["creator"] != wallet:
                db.execute("UPDATE sightings SET creator = ? WHERE mint = ?", (wallet, mint))
                ledger.record_creator_move(
                    wallet=wallet, mint=mint, kind="deployer_confirmed",
                    detail="resolved from chain after qualification",
                )

    return {"enriched": enriched, "failed": failed, "considered": len(rows)}


async def refresh_tracked_creators(*, limit: int = 15) -> dict[str, Any]:
    """Keep the dossiers of tracked wallets current.

    Deterministic and free — it reads the ledger and rewrites markdown. Runs
    once per cycle over the most recently active tracked wallets, so a
    wallet that went quiet is not rewritten pointlessly every six hours.
    """
    from app.memory.rollup import update_creator_dossier

    tracked = ledger.top_creators(limit=limit, tracked_only=True, window_hours=24)
    if not tracked:
        tracked = ledger.top_creators(limit=min(limit, 5), tracked_only=True)

    written: list[str] = []
    for creator in tracked:
        try:
            path = await update_creator_dossier(creator["wallet"])
            if path:
                written.append(path)
        except Exception:
            log.warning("dossier_refresh_failed", wallet=creator["wallet"], exc_info=True)
    return {"dossiers_written": len(written), "paths": written[:10]}
