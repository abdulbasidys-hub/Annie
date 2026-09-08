"""Stage 1 — discovery (§5, §20).

Two entry points write to the same place through :func:`record_launch`:

* :func:`run_discovery` — the manual/backfill sweep (signature polling via
  Helius RPC). Kept as a supplementary mechanism, not the primary one — see
  its docstring for why polling alone can't cover a program at Pump.fun's
  transaction volume.
* :mod:`app.api.routes.webhooks` — the primary mechanism. Helius pushes a
  ``CREATE`` event the moment one happens, instead of this app polling
  and hoping to catch one in a tiny recent slice of an extremely busy
  program. See that module's docstring for the full reasoning (Build.md §76
  amendment).

Both are deliberately cheap: no market data, no metadata, no creator
resolution here. A launch is a *sighting*, not a subject — pricing it is
:mod:`app.pipeline.watch`'s job, and only for the small slice that earns it.

Since the 2026-09-08 rewrite a sighting costs one local SQLite insert and
touches Firestore not at all. That is what makes it affordable to record
every launch without filtering at the door: the filtering happens later, on
attention rather than on ingest, which is the right place for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

from app.pipeline import stream
from app.providers.registry import ProviderRegistry
from app.providers.types import TokenLaunch

log = structlog.get_logger(__name__)

#: Signatures-per-program ceiling for one discovery run. Kept well under
#: Helius's 1000-per-call RPC limit because each signature in range costs a
#: second RPC call (getTransaction) to inspect — see
#: HeliusAdapter.discover_launches for the cost note. At Pump.fun's real
#: transaction volume this covers only a few seconds of activity per call —
#: it is a backfill/supplement to the webhook, not primary coverage.
DEFAULT_SCAN_LIMIT = 200


@dataclass(slots=True)
class DiscoveryRun:
    started_at: datetime
    finished_at: datetime | None = None
    launches_seen: int = 0
    tokens_created: int = 0
    tokens_already_known: int = 0
    errors: list[str] = field(default_factory=list)


async def record_launch(launch: TokenLaunch, *, now: datetime | None = None) -> bool:
    """Write one discovered launch. Returns ``True`` if it was new.

    Thin wrapper over :func:`app.pipeline.stream.ingest` so that "what a
    discovered token looks like on first sighting" stays defined in exactly
    one place, whichever entry point found it.
    """
    return stream.ingest(launch)


async def run_discovery(
    registry: ProviderRegistry,
    *,
    since: datetime,
    until: datetime | None = None,
    limit: int = DEFAULT_SCAN_LIMIT,
) -> DiscoveryRun:
    run = DiscoveryRun(started_at=datetime.now(timezone.utc))

    if not registry.settings.is_available("blockchain"):
        run.errors.append(
            "Helius (HELIUS_API_KEY / HELIUS_RPC_URL) is not configured; "
            "discovery has no way to find new tokens without it."
        )
        run.finished_at = datetime.now(timezone.utc)
        return run

    try:
        launches = await registry.launches.discover_launches(
            since=since, until=until, limit=limit
        )
    except Exception as exc:  # a provider outage must not crash the caller
        run.errors.append(str(exc))
        run.finished_at = datetime.now(timezone.utc)
        log.warning("discovery_run_failed", error=str(exc))
        return run

    run.launches_seen = len(launches)

    result = stream.ingest_many(launches)
    run.tokens_created = result.new
    run.tokens_already_known = result.repeats
    run.errors.extend(result.errors)

    run.finished_at = datetime.now(timezone.utc)
    log.info(
        "discovery_run_complete",
        seen=run.launches_seen,
        created=run.tokens_created,
        known=run.tokens_already_known,
    )
    return run
