"""Annie's current read — the front page of an intelligence system.

The dashboard this replaces led with counts: tokens collected, qualified by
tier, this period against the last. That was the right front page for a
system whose product was a database. It is the wrong one now, because the
product is what Annie *makes* of the market, and a count of qualified tokens
is not that — it is a measure of how much raw material passed through.

So this composes, in order of what should change a decision:

1. Whether the pipeline is even working. Every judgement below is worthless
   while nothing is arriving, so it cannot sit at the bottom of the page.
2. What she concluded at the last cycle — her headline, in her words.
3. What she currently thinks is working, straight out of
   ``core/whats-working.md``.
4. What she is watching, and what actually moved.
5. The statistical backing, so a claim can be checked rather than taken.

Entirely local reads — files and SQLite — so the page is free to open and
free to refresh.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.db.repo import FirestoreRepo, get_repo

from app.memory import health, index, ledger, service, signals

router = APIRouter()

#: Sections that are Annie's own thinking rather than a per-entity record.
#: "What has she been working on" means these — a creator dossier refreshed
#: by a deterministic job is not her having had a thought.
THINKING_SECTIONS = ("core", "playbook", "narratives", "notes")


def _excerpt(path: str, *, limit: int = 900) -> dict[str, Any] | None:
    """One core file, trimmed to something readable on a front page."""
    memory = service.read(path)
    if memory is None:
        return None
    body = memory.body.strip()
    truncated = len(body) > limit
    return {
        "path": memory.path,
        "title": memory.title,
        "updated": memory.updated,
        "body": body[:limit].rsplit("\n", 1)[0] if truncated else body,
        "truncated": truncated,
    }


@router.get("/today")
async def today(
    window_hours: int = Query(24, ge=1, le=168),
    repo: FirestoreRepo = Depends(get_repo),
) -> dict[str, Any]:
    """Everything the front page needs, in one request.

    Also carries the two things the app shell shows on every page — the
    research badge count and data freshness — so the shell can drop the old
    dashboard call. That endpoint issued five Firestore queries (launchpads,
    notes, tasks, anomalies, provider health) on every single page load to
    render one number in the sidebar.
    """
    from app.db.enums import ResearchTaskStatus
    from app.scheduling.scheduler import job_status

    diagnosis = health.diagnose()
    cycle = job_status("scheduler_cycle")
    learning = (cycle.get("last_result") or {}).get("learning") or {}

    movers = ledger.movers(since_hours=window_hours, limit=12)
    meaningful = signals.meaningful(limit=8)
    stats = ledger.stats()

    # Most recently revised thinking, newest first. Deliberately excludes
    # daily/weekly/monthly — those are the record of when she thought
    # something, not the thought.
    tree = service.tree()
    recent_thinking = sorted(
        (
            {**entry, "section": name}
            for name in THINKING_SECTIONS
            for entry in tree.get(name, [])
        ),
        key=lambda e: e.get("updated") or "",
        reverse=True,
    )[:6]

    # One query, for the sidebar badge. The rest of the old dashboard's
    # Firestore reads are not needed to render a shell.
    try:
        tasks, _ = await repo.list_research_tasks(limit=50)
        pending = sum(
            1
            for t in tasks
            if t.status in (ResearchTaskStatus.QUEUED, ResearchTaskStatus.RESEARCHING)
        )
    except Exception:
        pending = 0

    return {
        "research_pending": pending,
        "data_freshness": stats.get("last_sighting_at"),
        "pipeline": {
            "state": diagnosis["state"],
            "headline": diagnosis["headline"],
            "what_to_check": diagnosis["what_to_check"],
        },
        "latest_read": {
            "headline": learning.get("headline") or None,
            "at": cycle.get("last_run_at"),
            "edits": learning.get("applied") or [],
            "skipped": learning.get("skipped"),
        },
        "whats_working": _excerpt("core/whats-working.md"),
        "market_model": _excerpt("core/market-model.md", limit=700),
        "open_questions": _excerpt("core/open-questions.md", limit=600),
        "watching": {
            "creators": ledger.top_creators(limit=6, tracked_only=True),
            "narratives": [
                s["name"] for s in meaningful if s["status"] in ("rising", "new")
            ][:6],
        },
        "movers": [
            {
                "mint": m.mint,
                "symbol": m.symbol,
                "name": m.name,
                "launchpad": m.launchpad,
                "creator": m.creator,
                "peak_market_cap": m.peak_market_cap,
                "market_cap": m.market_cap,
                "tier": m.tier,
                "qualified_at": m.qualified_at,
                "round_tripped": bool(
                    m.peak_market_cap and m.market_cap
                    and m.market_cap < m.peak_market_cap * 0.5
                ),
            }
            for m in movers
        ],
        "signals": meaningful,
        "recent_thinking": recent_thinking,
        "scale": {
            "seen_24h": stats["sightings_24h"],
            "held": stats["sightings_total"],
            "qualified_24h": stats["qualified_24h"],
            "creators_tracked": stats["creators_tracked"],
            "memory_files": index.stats()["files"],
        },
        "window_hours": window_hours,
    }
