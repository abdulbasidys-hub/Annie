"""Daily digest generator (§41-§42) — the same engine backs two surfaces:
the Reports page (a `Report` document, browsable in the frontend) and the
Discord Morning Brief (app/scheduling/jobs.py's `_morning_brief` formats
this same Report for delivery). Building it once, not twice, is the point —
"Morning Brief" and "the daily Report" were the same missing thing wearing
two names in the original spec.

Deterministic, like `app/scheduling/jobs.py`'s `_daily_log` — every section
below is a direct read from real collections over the report's window, never
an LLM call. A report is exactly the kind of artifact §4/§49's "never invent
a number" rule is strictest about: it's meant to be trusted at a glance.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.db.enums import ResearchTaskStatus
from app.db.models.research import Report
from app.db.repo import FirestoreRepo

TIER_LABELS = {
    "100000": "$100k",
    "250000": "$250k",
    "500000": "$500k",
    "1000000": "$1M",
}


async def generate_daily_report(
    repo: FirestoreRepo, *, period_start: datetime, period_end: datetime
) -> Report:
    newly_qualified = await repo.qualified_tokens_in_window(start=period_start, end=period_end)
    counts_by_tier: dict[str, int] = {}
    for t in newly_qualified:
        key = str(t.qualified_threshold) if t.qualified_threshold is not None else "unknown"
        counts_by_tier[key] = counts_by_tier.get(key, 0) + 1

    all_trends = await repo.all_trends()
    changed_in_window = [
        tr for tr in all_trends
        if tr.last_status_change_at and period_start <= tr.last_status_change_at <= period_end
    ]
    trends_new = [tr for tr in changed_in_window if tr.status == "new"]
    trends_rising = [tr for tr in changed_in_window if tr.status == "rising"]
    trends_declining = [tr for tr in changed_in_window if tr.status == "declining"]

    completed_tasks, _ = await repo.list_research_tasks(status=ResearchTaskStatus.COMPLETED, limit=200)
    completed_tasks = [t for t in completed_tasks if t.completed_at and period_start <= t.completed_at <= period_end]

    all_notes = await repo.list_research_notes(current_only=True, limit=200)
    notes_in_window = [n for n in all_notes if n.created_at and period_start <= n.created_at <= period_end]
    hypothesis_notes = [n for n in notes_in_window if n.claim_type in ("hypothesis", "speculation")]

    from app.db.enums import MemoryType
    long_term_memories, _ = await repo.list_memories(type_=MemoryType.LONG_TERM, limit=200)
    memories_in_window = [
        m for m in long_term_memories if m.created_at and period_start <= m.created_at <= period_end
    ]

    dq_rows = await repo.data_quality_since(period_start, period_end)
    dq_issues = [r for r in dq_rows if not r.is_usable_for_trends or (r.coverage is not None and r.coverage < 0.8)]

    headline = _headline(newly_qualified, changed_in_window)
    biggest_change = _biggest_change(changed_in_window)

    sections: dict[str, Any] = {
        "new_discoveries": [
            {"mint": t.mint, "symbol": t.symbol, "market_cap": str(t.qualified_market_cap), "launchpad": t.launchpad_slug}
            for t in newly_qualified[:15]
        ],
        "rising_trends": [{"slug": tr.slug, "name": tr.name} for tr in trends_rising[:10]],
        "declining_trends": [{"slug": tr.slug, "name": tr.name} for tr in trends_declining[:10]],
        "new_trends": [{"slug": tr.slug, "name": tr.name} for tr in trends_new[:10]],
        "research_completed": [
            {"question": t.question, "claim_type": t.result_claim_type, "confidence": t.confidence}
            for t in completed_tasks[:10]
        ],
        "worth_investigating": [
            {"title": n.title, "body": n.body[:280]} for n in hypothesis_notes[:10]
        ],
        "memory_promoted": [{"title": m.title, "content": m.content[:280]} for m in memories_in_window[:10]],
        "data_quality": [
            {"stage": r.stage, "coverage": r.coverage, "usable": r.is_usable_for_trends, "notes": r.notes}
            for r in dq_issues[:10]
        ],
    }

    summary = _summary(newly_qualified, changed_in_window, completed_tasks, memories_in_window)
    markdown = _markdown(period_start, period_end, headline, biggest_change, summary, sections, counts_by_tier)

    report = Report(
        kind="daily",
        period_start=period_start,
        period_end=period_end,
        title=f"Daily digest — {period_start.date().isoformat()}",
        summary=summary,
        sections=sections,
        markdown=markdown,
        headline_finding=headline,
        biggest_change=biggest_change,
        limitations="Automated daily digest — deterministic counts only, no interpretive claims beyond what's directly observed.",
        tokens_qualified=len(newly_qualified),
        counts_by_tier=counts_by_tier,
        trends_new=len(trends_new),
        trends_rising=len(trends_rising),
        trends_declining=len(trends_declining),
        tasks_created=len(completed_tasks),
    )
    return await repo.upsert_report(report)


def _headline(newly_qualified: list, changed_trends: list) -> str | None:
    if newly_qualified:
        top = max(newly_qualified, key=lambda t: t.qualified_market_cap or 0)
        return f"{top.symbol or top.mint} qualified at ${top.qualified_market_cap} on {top.launchpad_slug}."
    if changed_trends:
        top = changed_trends[0]
        return f"Trend '{top.name}' moved to {top.status}."
    return None


def _biggest_change(changed_trends: list) -> str | None:
    if not changed_trends:
        return None
    top = max(changed_trends, key=lambda tr: abs(tr.change or 0))
    direction = "up" if (top.change or 0) > 0 else "down"
    return f"'{top.name}' moved {direction} the most ({top.change})."


def _summary(newly_qualified, changed_trends, completed_tasks, memories) -> str:
    parts = [f"{len(newly_qualified)} token(s) qualified"]
    if changed_trends:
        parts.append(f"{len(changed_trends)} trend(s) changed status")
    if completed_tasks:
        parts.append(f"{len(completed_tasks)} research task(s) completed")
    if memories:
        parts.append(f"{len(memories)} finding(s) promoted to long-term memory")
    return ", ".join(parts) + "."


def _markdown(period_start, period_end, headline, biggest_change, summary, sections, counts_by_tier) -> str:
    lines = [f"# Daily digest — {period_start.date().isoformat()}", "", summary, ""]
    if headline:
        lines += ["## What changed", headline, ""]
    if biggest_change:
        lines += ["## Biggest change", biggest_change, ""]
    if counts_by_tier:
        lines.append("## Qualified by tier")
        for tier, count in sorted(counts_by_tier.items(), key=lambda kv: kv[0]):
            lines.append(f"- {TIER_LABELS.get(tier, tier)}: {count}")
        lines.append("")
    for key, title in [
        ("new_discoveries", "New discoveries"),
        ("rising_trends", "Rising trends"),
        ("declining_trends", "Declining trends"),
        ("research_completed", "Research completed"),
        ("worth_investigating", "Worth investigating"),
        ("memory_promoted", "Promoted to memory"),
        ("data_quality", "Data quality"),
    ]:
        rows = sections.get(key) or []
        if not rows:
            continue
        lines.append(f"## {title}")
        for row in rows:
            lines.append(f"- {row}")
        lines.append("")
    return "\n".join(lines)
