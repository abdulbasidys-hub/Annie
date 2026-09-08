"""Runs a pipeline stage under a PipelineRun record (§20, §2026-08-25).

Shared between the manual "Run now" endpoints (app/api/routes/system.py —
fire-and-forget, polled via GET /api/system/pipeline-runs/{id}) and the
scheduled 6-hour full-cycle job (app/scheduling/jobs.py's
``_full_pipeline_and_brief``, which awaits each stage directly so it can
chain them in order) — one shared code path so System Health's run history
reflects both trigger types identically instead of the operator only ever
seeing their own manual clicks.

Each ``_run_*_stage`` function wraps one pipeline stage's real entry point
and flattens its result dataclass into a plain dict — the same shape the
manual endpoints already returned before this existed, so the frontend
contract doesn't change, only how the run gets there.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable

from app.db.repo import FirestoreRepo
from app.providers.registry import ProviderRegistry


async def finish_with(repo: FirestoreRepo, run_id: str, awaitable: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    """Awaits ``awaitable``, recording success or failure on the run record.
    Re-raises on failure — callers awaiting this directly (the scheduled
    chain) can decide whether one stage's failure should stop the next;
    callers firing this in the background (manual "Run now") should wrap it
    so an unhandled exception doesn't surface as a bare asyncio warning."""
    try:
        result = await awaitable
        await repo.finish_pipeline_run(run_id, status="done", result=result)
        return result
    except Exception as exc:
        await repo.finish_pipeline_run(run_id, status="error", error=str(exc))
        raise


async def fire_and_forget(repo: FirestoreRepo, run_id: str, awaitable: Awaitable[dict[str, Any]]) -> None:
    try:
        await finish_with(repo, run_id, awaitable)
    except Exception:
        pass  # already recorded on the run — nothing awaits this task's result


async def run_discovery_stage(registry: ProviderRegistry, repo: FirestoreRepo, *, hours: int = 24) -> dict[str, Any]:
    """Backfill sweep. Supplements the webhook; it cannot replace it.

    Pump.fun's real transaction volume means 1,000 signatures covers about
    six seconds, so polling can never provide primary coverage — see
    app/pipeline/discovery.py. Kept for gap-filling after an outage.
    """
    from app.pipeline.discovery import run_discovery

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    run = await run_discovery(registry, since=since)
    return {
        "launches_seen": run.launches_seen,
        "tokens_created": run.tokens_created,
        "tokens_already_known": run.tokens_already_known,
        "errors": run.errors,
    }


async def run_watch_stage(registry: ProviderRegistry, settings, *, batch_size: int | None = None) -> dict[str, Any]:
    """Re-price the watchlist. Batched, local writes, no Firestore."""
    from app.pipeline.watch import run_watch

    run = await run_watch(registry, settings, batch_size=batch_size)
    return run.to_dict()


async def run_signals_stage(repo: FirestoreRepo) -> dict[str, Any]:
    """Recompute signals from the ledger.

    Replaces the old ``run_trends_stage``, which read every qualified
    token's feature subcollection from Firestore on every pass — the single
    largest driver of the billing incident this rewrite addresses. Features
    are now derived from three short strings by pure functions, so this
    reads only local rows and costs nothing.
    """
    from app.memory import signals

    return signals.recompute().to_dict()


async def run_narratives_stage(repo: FirestoreRepo) -> dict[str, Any]:
    from app.narratives.cluster import run_narrative_clustering

    run = await run_narrative_clustering(repo)
    return {
        "qualified_tokens_scanned": run.qualified_tokens_scanned,
        "seeded_narratives_updated": run.seeded_narratives_updated,
        "emergent_narratives_found": run.emergent_narratives_found,
    }
