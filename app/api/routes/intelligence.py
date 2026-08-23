"""Intelligence routes: dashboard, trends, research, reports."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.schemas import (
    AnomalyOut,
    DashboardOut,
    HypothesisOut,
    Page,
    ReportDetail,
    ReportSummary,
    ResearchNoteOut,
    ResearchTaskSummary,
    TrendDetail,
    TrendSummary,
)
from app.config import Settings, get_settings
from app.db.enums import ResearchTaskStatus, TrendMaturity, TrendStatus
from app.db.models.intelligence import Trend
from app.db.repo import FirestoreRepo, get_repo
from app.providers.registry import ProviderRegistry, get_registry

router = APIRouter()

MATURITY_ORDER = {
    TrendMaturity.OBSERVATION: 0,
    TrendMaturity.CANDIDATE: 1,
    TrendMaturity.VALIDATED: 2,
}

DEFAULT_TIERS = [Decimal("100000"), Decimal("250000"), Decimal("500000"), Decimal("1000000")]


def _trend_summary(trend: Trend, series: list[float] | None = None) -> dict[str, Any]:
    return {
        "id": trend.slug,
        "slug": trend.slug,
        "name": trend.name,
        "category": trend.category,
        "status": trend.status,
        "maturity": trend.maturity,
        "confidence": trend.confidence,
        "cohort_threshold_usd": trend.cohort_threshold_usd,
        "recent": {
            "count": trend.recent_count,
            "total": trend.recent_total,
            "frequency": trend.recent_frequency,
        },
        "baseline": {
            "count": trend.baseline_count,
            "total": trend.baseline_total,
            "frequency": trend.baseline_frequency,
        },
        "recent_window_days": trend.recent_window_days,
        "baseline_window_days": trend.baseline_window_days,
        "change": trend.change,
        "relative_change": trend.relative_change,
        "lift": trend.lift,
        "recent_series": series or [],
        "first_detected_at": trend.first_detected_at,
        "last_observed_at": trend.last_observed_at,
        "persistence_days": trend.persistence_days,
    }


# -----------------------------------------------------------------------------
# Dashboard
# -----------------------------------------------------------------------------


@router.get("/dashboard", response_model=DashboardOut)
async def dashboard(
    window_days: int = Query(7, ge=1, le=365),
    repo: FirestoreRepo = Depends(get_repo),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=window_days)
    prior_start = start - timedelta(days=window_days)

    async def tier_counts(frm: datetime, to: datetime) -> dict[str, int]:
        out: dict[str, int] = {}
        for tier in DEFAULT_TIERS:
            tokens = await repo.qualified_tokens_in_window(start=frm, end=to, min_peak=tier)
            out[str(tier)] = len(tokens)
        return out

    all_tokens, total_tokens = await repo.list_tokens(limit=1)
    _, qualified_total = await repo.list_tokens(qualified_only=True, limit=1)

    all_trends = await repo.all_trends()
    active_trends = [t for t in all_trends if t.status != TrendStatus.DEAD.value]

    def top_by_change(status: str, n: int = 5) -> list[Trend]:
        matching = [t for t in all_trends if t.status == status]
        matching.sort(key=lambda t: abs(t.change or 0), reverse=True)
        return matching[:n]

    async def with_series(trends: list[Trend]) -> list[dict[str, Any]]:
        out = []
        for t in trends:
            obs = await repo.trend_observations(t.slug, limit=14)
            series = [o.frequency for o in reversed(obs) if o.frequency is not None]
            out.append(_trend_summary(t, series))
        return out

    launchpads, _ = await repo.list_launchpads(limit=200)
    emerging = sorted(
        [lp for lp in launchpads if lp.lifecycle in ("emerging", "growing")],
        key=lambda lp: lp.growth_rate_7d or 0,
        reverse=True,
    )[:5]

    notes = await repo.list_research_notes(current_only=True, limit=5)
    tasks, _ = await repo.list_research_tasks(limit=50)
    pending_tasks = [
        t for t in tasks if t.status in (ResearchTaskStatus.QUEUED, ResearchTaskStatus.RESEARCHING)
    ]
    pending_tasks.sort(key=lambda t: t.priority or 0, reverse=True)

    anomalies = await repo.list_anomalies(unacknowledged_only=True, limit=50)
    anomalies.sort(key=lambda a: a.severity or 0, reverse=True)

    freshness_rows = await repo.data_quality_since(now - timedelta(days=3), now + timedelta(days=1))
    freshness = max((r.measured_on for r in freshness_rows), default=None)

    provider_health = await repo.list_provider_health()

    return {
        "tokens_collected": total_tokens,
        "tokens_qualified": qualified_total,
        "counts_by_tier": await tier_counts(start, now),
        "counts_by_tier_previous": await tier_counts(prior_start, start),
        "window_days": window_days,
        "trends_active": len(active_trends),
        "trends_new": sum(1 for t in all_trends if t.status == TrendStatus.NEW.value),
        "trends_rising": sum(1 for t in all_trends if t.status == TrendStatus.RISING.value),
        "trends_declining": sum(1 for t in all_trends if t.status == TrendStatus.DECLINING.value),
        "rising_trends": await with_series(top_by_change(TrendStatus.RISING.value)),
        "new_trends": await with_series(top_by_change(TrendStatus.NEW.value)),
        "declining_trends": await with_series(top_by_change(TrendStatus.DECLINING.value)),
        "emerging_launchpads": [
            {
                "id": lp.slug, "slug": lp.slug, "name": lp.name, "lifecycle": lp.lifecycle,
                "launch_count": lp.launch_count, "qualified_count": lp.qualified_count,
                "success_rate": lp.success_rate, "market_share": lp.market_share,
                "growth_rate_7d": lp.growth_rate_7d, "growth_rate_30d": lp.growth_rate_30d,
                "is_known": lp.is_known, "first_seen_at": lp.first_seen_at,
                "last_seen_at": lp.last_seen_at,
            }
            for lp in emerging
        ],
        "recent_notes": [
            {
                "id": n.id, "title": n.title, "body": n.body, "claim_type": n.claim_type,
                "confidence": n.confidence, "category": n.category, "tags": n.tags,
                "sample_size": n.sample_size, "period_start": n.period_start,
                "period_end": n.period_end, "created_at": n.created_at,
                "is_current": n.is_current, "evidence": n.evidence,
                "counter_evidence": n.counter_evidence,
            }
            for n in notes
        ],
        "pending_tasks": [
            {
                "id": t.id, "question": t.question, "reason": t.reason, "origin": t.origin,
                "status": t.status, "priority": t.priority, "confidence": t.confidence,
                "created_at": t.created_at, "started_at": t.started_at,
                "completed_at": t.completed_at, "cost_usd": t.cost_usd,
            }
            for t in pending_tasks[:5]
        ],
        "open_anomalies": [
            {
                "id": a.id, "kind": a.kind, "title": a.title, "description": a.description,
                "detected_at": a.detected_at, "severity": a.severity, "magnitude": a.magnitude,
                "sample_size": a.sample_size, "acknowledged": a.acknowledged,
                "research_task_id": a.research_task_id, "evidence": a.evidence,
            }
            for a in anomalies[:5]
        ],
        "data_freshness_seconds": int((now - freshness).total_seconds()) if freshness else None,
        "last_ingestion_at": freshness,
        "last_trend_run_at": max(
            (t.last_observed_at for t in all_trends if t.last_observed_at), default=None
        ),
        "provider_health": [
            {
                "provider": r.provider, "status": r.status, "configured": r.status != "disabled",
                "last_success_at": r.last_success_at, "last_error_at": r.last_error_at,
                "last_error_message": r.last_error_message, "requests_24h": r.requests_24h,
                "errors_24h": r.errors_24h, "rate_limited_24h": r.rate_limited_24h,
                "error_rate_24h": r.error_rate_24h, "p50_latency_ms": r.p50_latency_ms,
                "p95_latency_ms": r.p95_latency_ms,
                "estimated_cost_24h_usd": r.estimated_cost_24h_usd,
                "data_freshness_seconds": r.data_freshness_seconds, "missing_env_vars": [],
            }
            for r in provider_health
        ],
        # Surfaced at the top of the dashboard: every figure below is suspect
        # while a source is down, so this cannot be buried on another page.
        "degraded_capabilities": [
            c for c in settings.capability_report() if c["status"] != "available"
        ],
    }


# -----------------------------------------------------------------------------
# Trends
# -----------------------------------------------------------------------------


@router.get("/trends", response_model=Page[TrendSummary])
async def list_trends(
    status: str | None = None,
    cohort_threshold: Decimal | None = None,
    min_maturity: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    repo: FirestoreRepo = Depends(get_repo),
) -> dict[str, Any]:
    trends, total = await repo.list_trends(status=status, limit=10000, offset=0)

    if cohort_threshold is not None:
        trends = [t for t in trends if t.cohort_threshold_usd == cohort_threshold]
    if min_maturity:
        floor = MATURITY_ORDER.get(TrendMaturity(min_maturity), 0)
        trends = [t for t in trends if MATURITY_ORDER.get(TrendMaturity(t.maturity), 0) >= floor]

    trends.sort(key=lambda t: abs(t.change or 0), reverse=True)
    total = len(trends)
    page = trends[offset : offset + limit]

    items = []
    for t in page:
        obs = await repo.trend_observations(t.slug, limit=14)
        series = [o.frequency for o in reversed(obs) if o.frequency is not None]
        items.append(_trend_summary(t, series))

    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/trends/{slug}", response_model=TrendDetail)
async def get_trend(slug: str, repo: FirestoreRepo = Depends(get_repo)) -> dict[str, Any]:
    trend = await repo.get_trend(slug)
    if trend is None:
        raise HTTPException(status_code=404, detail=f"No trend {slug}")

    observations = await repo.trend_observations(slug, limit=90)
    observations = list(reversed(observations))  # oldest first for the chart
    history = await repo.trend_history(slug, limit=100)
    history = list(reversed(history))  # oldest first

    examples = []
    for mint in trend.example_token_mints[:12]:
        t = await repo.get_token(mint)
        if t is not None:
            examples.append(t)

    evidence = trend.evidence or {}
    series = [o.frequency for o in observations if o.frequency is not None]
    return {
        **_trend_summary(trend, series),
        "description": trend.description,
        "p_value": trend.p_value,
        "effect_size": trend.effect_size,
        "ci_low": trend.ci_low,
        "ci_high": trend.ci_high,
        "variance": trend.variance,
        "revival_count": trend.revival_count,
        "peak_frequency": trend.peak_frequency,
        "peak_frequency_at": trend.peak_frequency_at,
        # Surfaced as a first-class field. These are the reasons the trend is
        # not stronger than stated, and the UI renders them inline (§26).
        "caveats": evidence.get("caveats", []),
        "evidence": evidence,
        "observations": [
            {
                "observed_on": o.observed_on, "window_days": o.window_days, "count": o.count,
                "total": o.total, "frequency": o.frequency,
                "baseline_frequency": o.baseline_frequency, "p_value": o.p_value,
            }
            for o in observations
        ],
        "history": [
            {
                "changed_at": h.changed_at, "from_status": h.from_status,
                "to_status": h.to_status, "to_maturity": h.to_maturity, "reason": h.reason,
            }
            for h in history
        ],
        "example_tokens": [
            {
                "id": t.mint, "mint": t.mint, "name": t.name, "symbol": t.symbol,
                "image_url": t.image_url, "launchpad_slug": t.launchpad_slug,
                "creator_wallet": t.creator_wallet, "launched_at": t.launched_at,
                "qualified_at": t.qualified_at, "qualified_market_cap": t.qualified_market_cap,
                "peak_market_cap": t.peak_market_cap, "peak_tier": t.peak_tier,
                "is_qualified": t.is_qualified, "verification_status": t.verification_status,
                "themes": [],
            }
            for t in examples
        ],
    }


# -----------------------------------------------------------------------------
# Research
# -----------------------------------------------------------------------------


@router.get("/research/tasks", response_model=Page[ResearchTaskSummary])
async def list_tasks(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    repo: FirestoreRepo = Depends(get_repo),
) -> dict[str, Any]:
    tasks, total = await repo.list_research_tasks(limit=limit, offset=offset)
    tasks.sort(key=lambda t: (t.priority or 0, t.created_at or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    items = [
        {
            "id": t.id, "question": t.question, "reason": t.reason, "origin": t.origin,
            "status": t.status, "priority": t.priority, "confidence": t.confidence,
            "created_at": t.created_at, "started_at": t.started_at,
            "completed_at": t.completed_at, "cost_usd": t.cost_usd,
        }
        for t in tasks
    ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("/research/tasks", response_model=ResearchTaskSummary, status_code=201)
async def create_task(
    body: dict[str, Any],
    repo: FirestoreRepo = Depends(get_repo),
    registry: ProviderRegistry = Depends(get_registry),
    settings: Settings = Depends(get_settings),
) -> Any:
    from app.db.models.research import ResearchTask

    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=422, detail="A question is required.")

    task = ResearchTask(
        question=question,
        reason=body.get("reason") or "Queued by operator.",
        origin=body.get("origin", "user"),
        status=ResearchTaskStatus.QUEUED,
        # Operator-created tasks outrank autonomous ones by default. A person
        # who typed a question is waiting for the answer.
        priority=0.75,
    )
    created = await repo.create_research_task(task)

    # Fire-and-forget: work the task in the background rather than making
    # the caller wait for a multi-round research loop. Same pattern the bots
    # use for a slow Annie reply (app/bots/telegram_bot.py's `_handle`). The
    # daily scheduled sweep (app/scheduling/jobs.py) is the safety net if
    # this task never gets a chance to run before a restart.
    from app.research.runner import run_research_task

    asyncio.create_task(
        run_research_task(created.id, repo=repo, registry=registry, settings=settings),
        name=f"research_task_{created.id}",
    )

    return {
        "id": created.id, "question": created.question, "reason": created.reason,
        "origin": created.origin, "status": created.status, "priority": created.priority,
        "confidence": created.confidence, "created_at": created.created_at,
        "started_at": created.started_at, "completed_at": created.completed_at,
        "cost_usd": created.cost_usd,
    }


@router.get("/research/notes", response_model=Page[ResearchNoteOut])
async def list_notes(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    repo: FirestoreRepo = Depends(get_repo),
) -> dict[str, Any]:
    notes = await repo.list_research_notes(current_only=False, limit=limit + offset)
    page = notes[offset : offset + limit]
    items = [
        {
            "id": n.id, "title": n.title, "body": n.body, "claim_type": n.claim_type,
            "confidence": n.confidence, "category": n.category, "tags": n.tags,
            "sample_size": n.sample_size, "period_start": n.period_start,
            "period_end": n.period_end, "created_at": n.created_at, "is_current": n.is_current,
            "evidence": n.evidence, "counter_evidence": n.counter_evidence,
        }
        for n in page
    ]
    return {"items": items, "total": len(notes), "limit": limit, "offset": offset}


@router.get("/research/hypotheses", response_model=Page[HypothesisOut])
async def list_hypotheses(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    repo: FirestoreRepo = Depends(get_repo),
) -> dict[str, Any]:
    hyps = await repo.list_hypotheses(limit=limit + offset)
    page = hyps[offset : offset + limit]
    items = [
        {
            "id": h.slug, "slug": h.slug, "statement": h.statement, "rationale": h.rationale,
            "status": h.status, "confidence": h.confidence, "sample_size": h.sample_size,
            "supporting_observations": h.supporting_observations,
            "contradicting_observations": h.contradicting_observations,
            "p_value": h.p_value, "effect_size": h.effect_size, "test_method": h.test_method,
            "first_tested_at": h.first_tested_at, "last_tested_at": h.last_tested_at,
            "evidence": h.evidence, "counter_evidence": h.counter_evidence,
        }
        for h in page
    ]
    return {"items": items, "total": len(hyps), "limit": limit, "offset": offset}


@router.get("/research/anomalies", response_model=Page[AnomalyOut])
async def list_anomalies(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    repo: FirestoreRepo = Depends(get_repo),
) -> dict[str, Any]:
    anomalies = await repo.list_anomalies(limit=limit + offset)
    page = anomalies[offset : offset + limit]
    items = [
        {
            "id": a.id, "kind": a.kind, "title": a.title, "description": a.description,
            "detected_at": a.detected_at, "severity": a.severity, "magnitude": a.magnitude,
            "sample_size": a.sample_size, "acknowledged": a.acknowledged,
            "research_task_id": a.research_task_id, "evidence": a.evidence,
        }
        for a in page
    ]
    return {"items": items, "total": len(anomalies), "limit": limit, "offset": offset}


# -----------------------------------------------------------------------------
# Reports
# -----------------------------------------------------------------------------


@router.get("/reports", response_model=Page[ReportSummary])
async def list_reports(
    kind: str = Query("daily"),
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
    repo: FirestoreRepo = Depends(get_repo),
) -> dict[str, Any]:
    reports = await repo.list_reports(kind=kind, limit=limit + offset)
    page = reports[offset : offset + limit]
    items = [
        {
            "id": r.id, "kind": r.kind, "title": r.title, "period_start": r.period_start,
            "period_end": r.period_end, "headline_finding": r.headline_finding,
            "tokens_qualified": r.tokens_qualified, "trends_new": r.trends_new,
            "trends_rising": r.trends_rising, "trends_declining": r.trends_declining,
        }
        for r in page
    ]
    return {"items": items, "total": len(reports), "limit": limit, "offset": offset}


@router.get("/reports/{report_id}", response_model=ReportDetail)
async def get_report(report_id: str, repo: FirestoreRepo = Depends(get_repo)) -> Any:
    report = await repo.get_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"No report {report_id}")
    return {
        "id": report.id, "kind": report.kind, "title": report.title,
        "period_start": report.period_start, "period_end": report.period_end,
        "headline_finding": report.headline_finding, "tokens_qualified": report.tokens_qualified,
        "trends_new": report.trends_new, "trends_rising": report.trends_rising,
        "trends_declining": report.trends_declining, "summary": report.summary,
        "sections": report.sections, "markdown": report.markdown,
        "biggest_change": report.biggest_change, "limitations": report.limitations,
        "counts_by_tier": report.counts_by_tier, "tasks_created": report.tasks_created,
        "generated_by_model": report.generated_by_model,
    }
