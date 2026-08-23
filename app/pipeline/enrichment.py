"""Stage 2/3 — qualification and enrichment (§4, §20).

For every token still sitting in ``pipeline_stage=discovery``: resolve a
market cap and decide qualification (:mod:`app.pipeline.qualification`,
unchanged from the original design — it never depended on Postgres). Tokens
that qualify get enriched: on-chain metadata, the real deployer wallet, and
deterministic name/ticker/description features (:mod:`app.analysis.features`,
also unchanged).

Tokens that do not qualify are still updated with their qualification
evidence — a "no" is a recorded decision, not silence (§4: "the system must
record the evidence used to determine qualification").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

from app.analysis.features import extract_all
from app.db.repo import FirestoreRepo
from app.pipeline.qualification import Qualifier
from app.providers.registry import ProviderRegistry

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class EnrichmentRun:
    started_at: datetime
    finished_at: datetime | None = None
    evaluated: int = 0
    qualified: int = 0
    enriched: int = 0
    errors: list[str] = field(default_factory=list)


async def run_enrichment(
    registry: ProviderRegistry,
    repo: FirestoreRepo,
    *,
    batch_size: int = 50,
) -> EnrichmentRun:
    run = EnrichmentRun(started_at=datetime.now(timezone.utc))
    qualifier = Qualifier(registry)

    pending = await repo.list_tokens_for_qualification(limit=batch_size)
    for token in pending:
        run.evaluated += 1
        try:
            verdict = await qualifier.evaluate(token.mint)
        except Exception as exc:
            run.errors.append(f"{token.mint} (qualify): {exc}")
            log.warning("qualification_failed", mint=token.mint, error=str(exc))
            continue

        await repo.apply_qualification(token.mint, verdict)

        if verdict.qualified:
            run.qualified += 1
            try:
                await _enrich_one(registry, repo, token.mint)
                run.enriched += 1
            except Exception as exc:
                run.errors.append(f"{token.mint} (enrich): {exc}")
                log.warning("enrichment_failed", mint=token.mint, error=str(exc))

    run.finished_at = datetime.now(timezone.utc)
    log.info(
        "enrichment_run_complete",
        evaluated=run.evaluated,
        qualified=run.qualified,
        enriched=run.enriched,
    )
    return run


async def run_enrichment_all(
    registry: ProviderRegistry,
    repo: FirestoreRepo,
    *,
    batch_size: int = 200,
    max_batches: int = 200,
) -> EnrichmentRun:
    """Drain the entire discovery-stage backlog, not just one batch.

    Used by the daily scheduled qualification job
    (:mod:`app.scheduling.jobs`) — the manual ``/api/system/run/enrichment``
    endpoint still calls :func:`run_enrichment` directly for one bounded
    batch, since an operator clicking "Run now" wants a fast result, not a
    run that could process thousands of tokens before responding.

    ``max_batches`` is a safety cap (200 x 200 = 40,000/day), not a tuned
    limit — real daily discovery volume should be far below it; it exists so
    a bug that makes ``list_tokens_for_qualification`` never shrink can't
    turn this into an unbounded loop.
    """
    total = EnrichmentRun(started_at=datetime.now(timezone.utc))
    for _ in range(max_batches):
        run = await run_enrichment(registry, repo, batch_size=batch_size)
        total.evaluated += run.evaluated
        total.qualified += run.qualified
        total.enriched += run.enriched
        total.errors.extend(run.errors)
        if run.evaluated < batch_size:
            break
    total.finished_at = datetime.now(timezone.utc)
    return total


async def _enrich_one(registry: ProviderRegistry, repo: FirestoreRepo, mint: str) -> None:
    metadata = None
    if registry.settings.is_available("blockchain"):
        metadata = await registry.blockchain.get_token_metadata(mint)

    creator_wallet: str | None = None
    if registry.settings.is_available("blockchain"):
        try:
            creator_wallet = await registry.blockchain.get_creator_wallet(mint)
        except Exception as exc:
            # §17's whole model depends on this being the real deployer, not a
            # guess. A failure here means "unknown", never a fallback wallet.
            log.info("creator_wallet_lookup_failed", mint=mint, error=str(exc))

    features = extract_all(
        metadata.name if metadata else None,
        metadata.symbol if metadata else None,
        metadata.description if metadata else None,
    )

    await repo.apply_enrichment(
        mint, metadata=metadata, creator_wallet=creator_wallet, features=features
    )
