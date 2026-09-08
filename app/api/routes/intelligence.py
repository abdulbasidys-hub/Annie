"""Intelligence routes: dashboard, trends, research, reports."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.schemas import (
    AnomalyOut,
    HypothesisOut,
    Page,
    ReportDetail,
    ReportSummary,
    ResearchNoteOut,
    ResearchTaskSummary,
)
from app.config import Settings, get_settings
from app.db.enums import ResearchTaskStatus, TrendMaturity
from app.db.repo import FirestoreRepo, get_repo
from app.providers.registry import ProviderRegistry, get_registry

router = APIRouter()

MATURITY_ORDER = {
    TrendMaturity.OBSERVATION: 0,
    TrendMaturity.CANDIDATE: 1,
    TrendMaturity.VALIDATED: 2,
}

DEFAULT_TIERS = [Decimal("100000"), Decimal("250000"), Decimal("500000"), Decimal("1000000")]


def _signal_summary(row: dict[str, Any], series: list[float] | None = None) -> dict[str, Any]:
    """One signal, in the shape the Trends pages and the dashboard render.

    Nested ``recent``/``baseline`` objects are not decoration: ``<Sample>`` on
    the frontend is the only sanctioned way to display a rate, and it requires
    the denominator alongside the value. That is what stops "100% of winners
    were cat-themed" being rendered without the "…out of 3" that makes it
    meaningless. Flattening these would quietly re-open exactly the failure
    the statistics module exists to prevent.

    ``change`` is derived here rather than stored, since it is just the
    difference of two frequencies the row already carries.
    """
    recent_freq = row.get("recent_freq")
    baseline_freq = row.get("baseline_freq")
    change = (
        recent_freq - baseline_freq
        if recent_freq is not None and baseline_freq is not None
        else None
    )
    return {
        "id": row["slug"],
        "slug": row["slug"],
        "name": row["name"],
        "category": row["category"],
        "status": row["status"],
        # The old model had a separate `maturity` field; a signal's maturity
        # is now exactly its sample adequacy, which `thin_sample` already
        # says. Mapped rather than dropped so the existing <Maturity> badge
        # keeps working.
        "maturity": "observation" if row.get("thin_sample") else "pattern",
        "confidence": row.get("confidence"),
        "cohort_threshold_usd": row.get("tier"),
        "subject_namespace": row.get("namespace"),
        "subject_key": row.get("key"),
        "subject_value": row.get("value"),
        "recent": {
            "count": row.get("recent_count"),
            "total": row.get("recent_total"),
            "frequency": recent_freq,
        },
        "baseline": {
            "count": None,  # stored as a frequency only; the cohort size varies by window
            "total": None,
            "frequency": baseline_freq,
        },
        "recent_window_days": 7,
        "baseline_window_days": 90,
        "change": change,
        "relative_change": (change / baseline_freq) if change is not None and baseline_freq else None,
        "lift": row.get("lift"),
        "p_value": row.get("p_value"),
        "recent_series": series or [],
        "first_detected_at": row.get("first_seen"),
        "last_observed_at": row.get("last_seen"),
        "persistence_days": row.get("persistence"),
        "thin_sample": row.get("thin_sample"),
    }


def _signal_with_series(row: dict[str, Any]) -> dict[str, Any]:
    """As above, plus the daily frequency series the sparkline draws."""
    from app.memory import signals

    detail = signals.get(row["slug"]) or {}
    series = [
        point["freq"]
        for point in reversed(detail.get("series") or [])
        if point.get("freq") is not None
    ]
    return _signal_summary(row, series)


# -----------------------------------------------------------------------------
# Dashboard
# -----------------------------------------------------------------------------


# The /dashboard endpoint lived here until 2026-09-08. It was the data-era
# front page — counts by tier, this period against the last — and it issued
# five Firestore queries (launchpads, notes, tasks, anomalies, provider
# health) on every page load because the app shell used it for one sidebar
# number. app/api/routes/today.py replaced it: local reads plus a single
# query, and a shape that leads with what Annie concluded rather than how
# much material went past.


@router.get("/trends")
async def list_trends(
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    include_low_confidence: bool = Query(False),
) -> dict[str, Any]:
    """Signals, under the route the frontend already calls "trends".

    Same concept as before — a characteristic's frequency among tokens that
    cleared a tier, compared against baseline — recomputed from the local
    ledger instead of a Firestore collection. ``/api/signals`` is the same
    data under its current name; this alias stays so an existing bookmark or
    a saved query does not break.
    """
    from app.memory import signals

    items = signals.listing(
        status=status, limit=limit + offset, include_thin=include_low_confidence
    )
    return {
        "items": [_signal_summary(row) for row in items[offset : offset + limit]],
        "total": len(items),
        "limit": limit,
        "offset": offset,
        "counts": signals.counts(),
    }


@router.get("/trends/{slug}")
async def get_trend(slug: str) -> dict[str, Any]:
    """One signal in full, with its daily series and fitted slope."""
    from app.memory import index, signals

    found = signals.get(slug)
    if found is None:
        raise HTTPException(status_code=404, detail=f"No signal {slug}")
    series = [
        point["freq"] for point in reversed(found.get("series") or [])
        if point.get("freq") is not None
    ]
    return {
        **_signal_summary(found, series),
        "series": found.get("series") or [],
        "slope": found.get("slope"),
        # Anything Annie has actually written about this characteristic —
        # usually far more useful than the numbers, which are all above.
        "related_memories": [h.to_dict() for h in index.search(found["name"], limit=4)],
    }


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
