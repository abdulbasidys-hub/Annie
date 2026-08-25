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
    run_enrichment_stage,
    run_narratives_stage,
    run_trends_stage,
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


@router.post("/run/enrichment")
async def run_enrichment_now(
    batch_size: int = Query(50, ge=1, le=200),
    repo: FirestoreRepo = Depends(get_repo),
    registry: ProviderRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Manually trigger Stage 2/3 (§4, §20): qualify and enrich discovered tokens. See run_discovery_now's docstring for the async/polling contract."""
    run = await repo.create_pipeline_run("enrichment", trigger="manual")
    asyncio.create_task(fire_and_forget(repo, run.id, run_enrichment_stage(registry, repo, batch_size=batch_size)))
    return {"run_id": run.id}


@router.post("/run/trends")
async def run_trends_now(
    repo: FirestoreRepo = Depends(get_repo),
) -> dict[str, Any]:
    """Manually trigger the trend engine (§24-§28) over the qualified dataset. See run_discovery_now's docstring for the async/polling contract."""
    run = await repo.create_pipeline_run("trends", trigger="manual")
    asyncio.create_task(fire_and_forget(repo, run.id, run_trends_stage(repo)))
    return {"run_id": run.id}


@router.post("/run/narratives")
async def run_narratives_now(
    repo: FirestoreRepo = Depends(get_repo),
) -> dict[str, Any]:
    """Manually trigger narrative clustering (§16) over the qualified dataset. See run_discovery_now's docstring for the async/polling contract."""
    run = await repo.create_pipeline_run("narratives", trigger="manual")
    asyncio.create_task(fire_and_forget(repo, run.id, run_narratives_stage(repo)))
    return {"run_id": run.id}


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
