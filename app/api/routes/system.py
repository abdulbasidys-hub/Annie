"""System routes: capabilities, provider health, data quality, settings."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.schemas import CapabilityOut, DataQualityOut, Page, ProviderHealthOut, SettingOut
from app.config import Settings, get_settings
from app.db.repo import FirestoreRepo, get_repo
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
    are skipped, not duplicated."""
    from app.pipeline.discovery import run_discovery

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    run = await run_discovery(registry, repo, since=since)
    return {
        "launches_seen": run.launches_seen,
        "tokens_created": run.tokens_created,
        "tokens_already_known": run.tokens_already_known,
        "errors": run.errors,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


@router.post("/run/enrichment")
async def run_enrichment_now(
    batch_size: int = Query(50, ge=1, le=200),
    repo: FirestoreRepo = Depends(get_repo),
    registry: ProviderRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Manually trigger Stage 2/3 (§4, §20): qualify and enrich discovered tokens."""
    from app.pipeline.enrichment import run_enrichment

    run, _cursor = await run_enrichment(registry, repo, batch_size=batch_size)
    return {
        "evaluated": run.evaluated,
        "qualified": run.qualified,
        "enriched": run.enriched,
        "errors": run.errors,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


@router.post("/run/trends")
async def run_trends_now(repo: FirestoreRepo = Depends(get_repo)) -> dict[str, Any]:
    """Manually trigger the trend engine (§24-§28) over the qualified dataset."""
    from app.trends.engine import TrendEngine

    engine = TrendEngine(repo)
    run = await engine.run()
    return {
        "cohorts_evaluated": run.cohorts_evaluated,
        "features_evaluated": run.features_evaluated,
        "trends_created": run.trends_created,
        "trends_updated": run.trends_updated,
        "status_changes": run.status_changes,
        "revivals": run.revivals,
        "skipped_windows": run.skipped_windows,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


@router.post("/run/narratives")
async def run_narratives_now(repo: FirestoreRepo = Depends(get_repo)) -> dict[str, Any]:
    """Manually trigger narrative clustering (§16) over the qualified dataset."""
    from app.narratives.cluster import run_narrative_clustering

    run = await run_narrative_clustering(repo)
    return {
        "qualified_tokens_scanned": run.qualified_tokens_scanned,
        "seeded_narratives_updated": run.seeded_narratives_updated,
        "emergent_narratives_found": run.emergent_narratives_found,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


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
