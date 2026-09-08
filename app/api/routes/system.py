"""System routes: capabilities, provider health, data quality, settings."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.schemas import CapabilityOut, DataQualityOut, Page, PipelineRunOut, ProviderHealthOut, SettingOut
from app.config import Settings, get_settings
from app.db.repo import FirestoreRepo, get_repo
from app.pipeline.tracking import (
    fire_and_forget,
    run_discovery_stage,
    run_narratives_stage,
    run_signals_stage,
    run_watch_stage,
)
from app.providers.registry import ProviderRegistry, get_registry

router = APIRouter()


@router.get("/capabilities", response_model=Page[CapabilityOut])
async def capabilities(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """What this deployment can and cannot do, and what would enable the rest."""
    items = settings.capability_report()
    return {"items": items, "total": len(items), "limit": len(items), "offset": 0}


@router.get("/health", response_model=Page[ProviderHealthOut])
async def provider_health(
    live: bool = Query(False, description="Probe each provider now instead of reading stored rollups."),
    repo: FirestoreRepo = Depends(get_repo),
    registry: ProviderRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Provider status.

    Defaults to stored rollups because the dashboard polls this. ``live=1``
    actually calls each provider — useful after changing a key, but it costs a
    request per provider and should not be on a polling path.
    """
    if live:
        snapshot = await registry.health_snapshot()
        items = [
            {
                "provider": row["provider"],
                "capability_label": row["capability_label"],
                "status": row["status"],
                "configured": row["configured"],
                "missing_env_vars": row["missing_env_vars"],
            }
            for row in snapshot
        ]
        return {"items": items, "total": len(items), "limit": len(items), "offset": 0}

    rows = await repo.list_provider_health()
    items = [
        {
            "provider": r.provider,
            "status": r.status,
            "configured": r.status != "disabled",
            "last_success_at": r.last_success_at,
            "last_error_at": r.last_error_at,
            "last_error_message": r.last_error_message,
            "requests_24h": r.requests_24h,
            "errors_24h": r.errors_24h,
            "rate_limited_24h": r.rate_limited_24h,
            "error_rate_24h": r.error_rate_24h,
            "p50_latency_ms": r.p50_latency_ms,
            "p95_latency_ms": r.p95_latency_ms,
            "estimated_cost_24h_usd": r.estimated_cost_24h_usd,
            "data_freshness_seconds": r.data_freshness_seconds,
            "missing_env_vars": [],
        }
        for r in rows
    ]
    return {"items": items, "total": len(items), "limit": len(items), "offset": 0}


@router.get("/data-quality", response_model=Page[DataQualityOut])
async def data_quality(
    days: int = Query(14, ge=1, le=365),
    repo: FirestoreRepo = Depends(get_repo),
) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    until = datetime.now(timezone.utc) + timedelta(days=1)
    rows = await repo.data_quality_since(since, until)
    rows.sort(key=lambda r: r.measured_on, reverse=True)
    items = [
        {
            "measured_on": r.measured_on,
            "stage": r.stage,
            "attempted": r.attempted,
            "succeeded": r.succeeded,
            "failed": r.failed,
            "coverage": r.coverage,
            "is_usable_for_trends": r.is_usable_for_trends,
            "notes": r.notes,
        }
        for r in rows
    ]
    return {"items": items, "total": len(items), "limit": len(items), "offset": 0}


@router.get("/settings", response_model=Page[SettingOut])
async def list_settings(repo: FirestoreRepo = Depends(get_repo)) -> dict[str, Any]:
    rows = await repo.list_settings()
    items = [
        {"key": r.key, "value": r.value, "description": r.description, "updated_at": r.updated_at}
        for r in rows
    ]
    return {"items": items, "total": len(items), "limit": len(items), "offset": 0}


@router.post("/run/discovery")
async def run_discovery_now(
    hours: int = Query(24, ge=1, le=168, description="How far back to scan."),
    repo: FirestoreRepo = Depends(get_repo),
    registry: ProviderRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Manually trigger Stage 1 discovery backfill (§20) — the Helius webhook
    (app/api/routes/webhooks.py) is the primary, real-time discovery path;
    this polling backfill and this endpoint exist for catching up a gap, not
    as the everyday mechanism. Safe to call repeatedly; already-known mints
    are skipped, not duplicated.

    Returns immediately with a ``run_id`` rather than blocking until the
    stage finishes (2026-08-25: enrichment in particular can run long enough
    to look identical to a hung request from the browser's side) — poll
    ``GET /run/pipeline-runs/{run_id}`` for status and, once ``done``, the
    result.
    """
    run = await repo.create_pipeline_run("discovery", trigger="manual")
    asyncio.create_task(fire_and_forget(repo, run.id, run_discovery_stage(registry, repo, hours=hours)))
    return {"run_id": run.id}


@router.post("/run/watch")
async def run_watch_now(
    batch_size: int = Query(300, ge=1, le=1000),
    repo: FirestoreRepo = Depends(get_repo),
    registry: ProviderRegistry = Depends(get_registry),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Re-price the watchlist now.

    Replaces the old "run enrichment" button. Lookups are batched 30 mints
    per request and results are written locally, so this is cheap enough to
    click freely. See run_discovery_now's docstring for the async/polling
    contract.
    """
    run = await repo.create_pipeline_run("watch", trigger="manual")
    asyncio.create_task(
        fire_and_forget(repo, run.id, run_watch_stage(registry, settings, batch_size=batch_size))
    )
    return {"run_id": run.id}


@router.post("/run/signals")
async def run_signals_now(repo: FirestoreRepo = Depends(get_repo)) -> dict[str, Any]:
    """Recompute signals from the ledger.

    Free — local reads and pure statistics, no Firestore and no model. See
    run_discovery_now's docstring for the async/polling contract.
    """
    run = await repo.create_pipeline_run("signals", trigger="manual")
    asyncio.create_task(fire_and_forget(repo, run.id, run_signals_stage(repo)))
    return {"run_id": run.id}


@router.post("/run/cycle")
async def run_cycle_now(
    repo: FirestoreRepo = Depends(get_repo),
    registry: ProviderRegistry = Depends(get_registry),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Run a full thinking cycle immediately: signals, digest, learn, tidy, snapshot.

    This is the only manual action here that spends money — one bounded
    model call, unless the window was quiet, in which case it skips the call
    entirely. Everything else on this page is free.
    """
    from app.scheduling.jobs import _cycle

    run = await repo.create_pipeline_run("cycle", trigger="manual")
    asyncio.create_task(fire_and_forget(repo, run.id, _cycle(registry, repo, settings)))
    return {"run_id": run.id}


@router.post("/run/narratives")
async def run_narratives_now(
    repo: FirestoreRepo = Depends(get_repo),
) -> dict[str, Any]:
    """Manually trigger narrative clustering (§16) over the qualified dataset. See run_discovery_now's docstring for the async/polling contract."""
    run = await repo.create_pipeline_run("narratives", trigger="manual")
    asyncio.create_task(fire_and_forget(repo, run.id, run_narratives_stage(repo)))
    return {"run_id": run.id}


@router.get("/cost")
async def cost_report() -> dict[str, Any]:
    """Where this deployment's spend actually is, and how much headroom is left.

    Exists because "too much data" is only visible if someone measures it.
    Firestore counters are this process's own writes and reads against the
    configured budget (well under the Spark plan's 20,000/50,000 daily caps);
    the memory figures are the size of what Annie keeps; the durability
    block says whether that memory survives a redeploy.
    """
    from app.db import budget
    from app.memory import index, ledger
    from app.memory.paths import durability_report
    from app.pipeline import stream
    from app.scheduling.jobs import JOBS
    from app.scheduling.scheduler import job_status

    return {
        "firestore": budget.report(),
        "memory": index.stats(),
        "ledger": ledger.stats(),
        "stream": stream.recent_activity(60),
        "durability": durability_report(),
        "jobs": [
            {"name": j.name, "mode": j.mode, **job_status(j.settings_key)} for j in JOBS
        ],
        "model_calls_per_day": {
            "scheduled": "4 cycle calls (skipped on a quiet window), "
                         "plus 1 weekly and 1 monthly rollup",
            "on_demand": "chat turns, and token_idea only when asked",
            "note": "Counting, ranking, filtering, statistics and every file "
                    "write are deterministic Python over local SQLite and cost nothing.",
        },
    }


@router.get("/pipeline-runs/{run_id}", response_model=PipelineRunOut)
async def get_pipeline_run(run_id: str, repo: FirestoreRepo = Depends(get_repo)) -> Any:
    run = await repo.get_pipeline_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No run with that id.")
    return run


@router.get("/pipeline-runs", response_model=Page[PipelineRunOut])
async def list_pipeline_runs(
    stage: str | None = Query(None),
    limit: int = Query(10, ge=1, le=50),
    repo: FirestoreRepo = Depends(get_repo),
) -> dict[str, Any]:
    """Run history for System Health's per-stage panels — most recent first."""
    runs = await repo.list_pipeline_runs(stage=stage, limit=limit)
    return {"items": runs, "total": len(runs), "limit": limit, "offset": 0}


@router.patch("/settings/{key}", response_model=SettingOut)
async def update_setting(
    key: str,
    body: dict[str, Any],
    repo: FirestoreRepo = Depends(get_repo),
) -> dict[str, Any]:
    """Change a research parameter. Every change is written to the audit log (§67)."""
    if "value" not in body:
        raise HTTPException(status_code=422, detail="Body must contain a 'value' key.")

    setting = await repo.upsert_setting(key, body["value"])
    return {
        "key": setting.key,
        "value": setting.value,
        "description": setting.description,
        "updated_at": setting.updated_at,
    }
