"""The actual scheduled jobs, registered with :class:`app.scheduling.scheduler.Scheduler`.

Kept separate from ``scheduler.py`` so the generic timing/persistence
machinery never has to change when a new job is added — see
``app/main.py``'s ``_start_scheduler`` for where this list is wired in.
"""

from __future__ import annotations

from typing import Any

from app.db.repo import FirestoreRepo
from app.providers.registry import ProviderRegistry
from app.scheduling.scheduler import ScheduledJob


async def _daily_qualification(
    registry: ProviderRegistry, repo: FirestoreRepo, settings
) -> dict[str, Any]:
    """Drain the full discovery-stage backlog through qualification + enrichment.

    This is the daily counterpart to the manual ``/api/system/run/enrichment``
    endpoint (which still processes one bounded batch on demand, for a quick
    operator-triggered check). See ``app/pipeline/enrichment.py``'s
    ``run_enrichment_all`` for why a full drain is safe to run unattended:
    qualification structurally cannot succeed for a token that hasn't
    migrated off its bonding curve (DexScreener has no quote for one, and
    DexScreener is this deployment's only market-data source) — confirmed
    directly against real chain data on 2026-08-24 — so this never mistakes
    "still on the bonding curve" for "doesn't qualify" and never needs a
    separate migration check of its own.
    """
    from app.pipeline.enrichment import run_enrichment_all

    run = await run_enrichment_all(registry, repo)
    return {
        "evaluated": run.evaluated,
        "qualified": run.qualified,
        "enriched": run.enriched,
        "errors": len(run.errors),
    }


async def _research_task_sweep(
    registry: ProviderRegistry, repo: FirestoreRepo, settings
) -> dict[str, Any]:
    """Catch any ResearchTask still ``queued`` — normally a task starts
    within seconds of creation (the fire-and-forget trigger in
    ``app/api/routes/intelligence.py``'s ``create_task``); this only matters
    for one orphaned by a process restart in that window. See
    ``app/research/runner.py``'s module docstring.
    """
    from app.db.enums import ResearchTaskStatus
    from app.research.runner import run_research_task

    tasks, _ = await repo.list_research_tasks(status=ResearchTaskStatus.QUEUED, limit=20)
    for task in tasks:
        await run_research_task(task.id, repo=repo, registry=registry, settings=settings)
    return {"swept": len(tasks)}


#: Every job the scheduler runs. Each entry's ``settings_key`` is what shows
#: up as an editable row on the Settings page — change the trigger time/
#: timezone/enabled flag there, no redeploy needed.
JOBS: list[ScheduledJob] = [
    ScheduledJob(
        name="daily_qualification",
        settings_key="scheduler_daily_qualification",
        run=_daily_qualification,
        default_hour=2,
        default_minute=0,
        default_timezone="UTC",
    ),
    ScheduledJob(
        name="research_task_sweep",
        settings_key="scheduler_research_task_sweep",
        run=_research_task_sweep,
        default_hour=3,
        default_minute=0,
        default_timezone="UTC",
    ),
]
