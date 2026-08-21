"""Stage 1 — discovery (§5, §20).

Sweeps known launchpad programs (currently just Pump.fun — see
``app.providers.helius.KNOWN_LAUNCHPAD_PROGRAMS``) for new mints and writes a
minimal token record for each one not already known. Deliberately cheap: no
market data, no metadata, no creator resolution here — that is enrichment's
job, and running it against every launch (most of which never qualify) would
be exactly the unnecessary spend §48 exists to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

from app.db.models.tokens import Token
from app.db.repo import FirestoreRepo
from app.db.enums import PipelineStage
from app.providers.registry import ProviderRegistry

log = structlog.get_logger(__name__)

#: Signatures-per-program ceiling for one discovery run. Kept well under
#: Helius's 1000-per-call RPC limit because each signature in range costs a
#: second RPC call (getTransaction) to inspect — see
#: HeliusAdapter.discover_launches for the cost note.
DEFAULT_SCAN_LIMIT = 200


@dataclass(slots=True)
class DiscoveryRun:
    started_at: datetime
    finished_at: datetime | None = None
    launches_seen: int = 0
    tokens_created: int = 0
    tokens_already_known: int = 0
    errors: list[str] = field(default_factory=list)


async def run_discovery(
    registry: ProviderRegistry,
    repo: FirestoreRepo,
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

    for launch in launches:
        token = Token(
            mint=launch.mint,
            name=launch.name,
            symbol=launch.symbol,
            creator_wallet=launch.creator_wallet,
            launchpad_slug=launch.launchpad_slug,
            launched_at=launch.launched_at,
            launch_signature=launch.signature,
            pipeline_stage=PipelineStage.DISCOVERY,
            source=launch.provenance.provider,
            source_type=launch.provenance.source_type,
            source_observed_at=launch.provenance.observed_at,
            verification_status="unverified",
            data_sources=[launch.provenance.provider],
        )
        created = await repo.create_discovered_token(token)
        if created:
            run.tokens_created += 1
            if launch.launchpad_slug:
                await repo.touch_launchpad_seen(
                    launch.launchpad_slug,
                    name=None,
                    program_id=None,
                    when=launch.launched_at or run.started_at,
                )
        else:
            run.tokens_already_known += 1

    run.finished_at = datetime.now(timezone.utc)
    log.info(
        "discovery_run_complete",
        seen=run.launches_seen,
        created=run.tokens_created,
        known=run.tokens_already_known,
    )
    return run
