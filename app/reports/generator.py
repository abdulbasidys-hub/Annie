"""Daily digest generator — the browsable `Report` document.

Deterministic, like ``app/memory/rollup.py``'s daily log: every section below
is a direct read over the report's window, never an LLM call. A report is
exactly the kind of artifact the "never invent a number" rule is strictest
about, because it is meant to be trusted at a glance.

Sources, after the 2026-09-08 rewrite: qualified tokens and their tiers come
from the local ledger, characteristics from the signals table (both free),
and research tasks, notes and data-quality rows from Firestore, where those
few operator-facing documents still belong.

This is distinct from the memory files. A report is a formatted snapshot of
one window, generated on demand and safe to regenerate; a memory file is
Annie's own judgement, written once and revised deliberately. Conflating them
would mean either regenerating her thinking or never being able to re-render
a report.
"""

from __future__ import annotations

from datetime import datetime, timezone
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
    from app.memory import ledger, signals

    newly_qualified = ledger.qualified_in_window(period_start, period_end)
    counts_by_tier: dict[str, int] = {}
    for token in newly_qualified:
        key = str(int(token.tier)) if token.tier else "unknown"
        counts_by_tier[key] = counts_by_tier.get(key, 0) + 1

    # `meaningful` already applies the sample-size bars, so a report cannot
    # cite a characteristic seen three times as though it were a finding.
    strong_signals = signals.meaningful(limit=40)
    by_status = {
        status: [s for s in strong_signals if s["status"] == status]
        for status in ("new", "rising", "declining")
    }
    changed = [s for s in strong_signals if s["status"] in ("new", "rising", "declining")]

    completed_tasks, _ = await repo.list_research_tasks(status=ResearchTaskStatus.COMPLETED, limit=200)
    completed_tasks = [
        t for t in completed_tasks if t.completed_at and period_start <= t.completed_at <= period_end
    ]

    all_notes = await repo.list_research_notes(current_only=True, limit=200)
    notes_in_window = [n for n in all_notes if n.created_at and period_start <= n.created_at <= period_end]
    hypothesis_notes = [n for n in notes_in_window if n.claim_type in ("hypothesis", "speculation")]

    # Memory files written in the window, read from disk rather than from a
    # Firestore memories collection that no longer exists.
    memories_in_window = _memories_written_between(period_start, period_end)

    dq_rows = await repo.data_quality_since(period_start, period_end)
    dq_issues = [
        r for r in dq_rows if not r.is_usable_for_trends or (r.coverage is not None and r.coverage < 0.8)
    ]

    headline = _headline(newly_qualified, changed)
    biggest_change = _biggest_change(changed)

    sections: dict[str, Any] = {
        "new_discoveries": [
            {
                "mint": t.mint,
                "symbol": t.symbol,
                "market_cap": t.peak_market_cap,
                "tier": t.tier,
                "launchpad": t.launchpad,
                "creator": t.creator,
            }
            for t in newly_qualified[:15]
        ],
        "rising_trends": [
            {"slug": s["slug"], "name": s["name"]} for s in by_status["rising"][:10]
        ],
        "declining_trends": [
            {"slug": s["slug"], "name": s["name"]} for s in by_status["declining"][:10]
        ],
        "new_trends": [{"slug": s["slug"], "name": s["name"]} for s in by_status["new"][:10]],
        "research_completed": [
            {"question": t.question, "claim_type": t.result_claim_type, "confidence": t.confidence}
            for t in completed_tasks[:10]
        ],
        "worth_investigating": [
            {"title": n.title, "body": n.body[:280]} for n in hypothesis_notes[:10]
        ],
        "memory_promoted": [
            {"title": m.title, "content": m.summary(280), "path": m.path}
            for m in memories_in_window[:10]
        ],
        "data_quality": [
            {"stage": r.stage, "coverage": r.coverage, "usable": r.is_usable_for_trends, "notes": r.notes}
            for r in dq_issues[:10]
        ],
    }

    summary = _summary(newly_qualified, changed, completed_tasks, memories_in_window)
    markdown = _markdown(
        period_start, period_end, headline, biggest_change, summary, sections, counts_by_tier
    )

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
        limitations=(
            "Automated digest — deterministic counts only, no interpretive claims "
            "beyond what is directly observed. Interpretation lives in the memory "
            "files, which are written separately."
        ),
        tokens_qualified=len(newly_qualified),
        counts_by_tier=counts_by_tier,
        trends_new=len(by_status["new"]),
        trends_rising=len(by_status["rising"]),
        trends_declining=len(by_status["declining"]),
        tasks_created=len(completed_tasks),
    )
    return await repo.upsert_report(report)


def _memories_written_between(start: datetime, end: datetime) -> list:
    """Memory files whose ``updated`` stamp falls inside the window.

    Local file reads, so this is free. A malformed or missing timestamp
    excludes the file rather than defaulting it into the window — a report
    claiming Annie wrote something today when she did not is worse than one
    that misses it.
    """
    from app.memory.files import MemoryStore

    found = []
    for memory in MemoryStore().load_all():
        if memory.section in ("daily", "weekly", "monthly"):
            continue  # the period records themselves, not findings within it
        stamp = memory.updated or memory.created
        if not stamp:
            continue
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if start <= when <= end:
            found.append(memory)
    found.sort(key=lambda m: m.importance, reverse=True)
    return found


def _headline(newly_qualified: list, changed_signals: list) -> str | None:
    if newly_qualified:
        top = max(newly_qualified, key=lambda t: t.peak_market_cap or 0)
        label = top.symbol or top.name or top.mint[:8]
        return (
            f"{label} peaked at ${top.peak_market_cap:,.0f} on "
            f"{top.launchpad or 'an unknown launchpad'}."
        )
    if changed_signals:
        top = changed_signals[0]
        return f"'{top['name']}' is {top['status']}."
    return None


def _biggest_change(changed_signals: list) -> str | None:
    if not changed_signals:
        return None
    top = max(changed_signals, key=lambda s: abs(s.get("lift") or 0))
    lift = top.get("lift")
    if not lift:
        return f"'{top['name']}' appeared, with no baseline to compare against yet."
    direction = "over" if lift > 1 else "under"
    return (
        f"'{top['name']}' is running {lift:.1f}x {direction} its baseline "
        f"({top['recent_count']}/{top['recent_total']} of ${int(top['tier']):,}+ tokens)."
    )


def _summary(newly_qualified, changed_signals, completed_tasks, memories) -> str:
    parts = [f"{len(newly_qualified)} token(s) cleared a tier"]
    if changed_signals:
        parts.append(f"{len(changed_signals)} characteristic(s) meaningfully over baseline")
    if completed_tasks:
        parts.append(f"{len(completed_tasks)} research task(s) completed")
    if memories:
        parts.append(f"{len(memories)} memory file(s) written or revised")
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
