"""The actual scheduled jobs, registered with :class:`app.scheduling.scheduler.Scheduler`.

Kept separate from ``scheduler.py`` so the generic timing/persistence
machinery never has to change when a new job is added — see
``app/main.py``'s ``_start_scheduler`` for where this list is wired in.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from app.db.repo import FirestoreRepo
from app.providers.registry import ProviderRegistry
from app.scheduling.scheduler import ScheduledJob

log = structlog.get_logger(__name__)


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


async def _daily_log(registry: ProviderRegistry, repo: FirestoreRepo, settings) -> dict[str, Any]:
    """A chronological, deterministic record of the last 24h's work — see
    the Memory page's "Daily Logs" tab. Deliberately not an LLM-authored
    narrative: every line here is a direct count from a real query, so
    there is nothing for it to get wrong. Memory consolidation (below) is
    where judgment/synthesis actually happens, reading these logs as input.
    """
    from app.db.enums import MemoryType, ResearchTaskStatus
    from app.db.models.research import Memory

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)

    newly_qualified = await repo.qualified_tokens_in_window(start=since, end=now)
    completed_tasks, _ = await repo.list_research_tasks(status=ResearchTaskStatus.COMPLETED, limit=100)
    completed_tasks = [t for t in completed_tasks if t.completed_at and t.completed_at >= since]
    recent_notes = await repo.list_research_notes(current_only=True, limit=50)
    notes_today = [n for n in recent_notes if n.created_at and n.created_at >= since]

    lines = [f"Daily log — {now.date().isoformat()}", ""]
    lines.append(f"Tokens newly qualified: {len(newly_qualified)}")
    for t in newly_qualified[:10]:
        lines.append(f"  - {t.symbol or t.mint} on {t.launchpad_slug}: ${t.qualified_market_cap}")
    lines.append(f"Research tasks completed: {len(completed_tasks)}")
    for t in completed_tasks[:10]:
        lines.append(f"  - {t.question} -> {t.result_claim_type}/{t.confidence}")
    lines.append(f"Research notes recorded: {len(notes_today)}")

    content = "\n".join(lines)
    memory = await repo.create_memory(
        Memory(
            type=MemoryType.DAILY_LOG,
            title=f"Daily log — {now.date().isoformat()}",
            content=content,
            structured_data={
                "tokens_qualified": len(newly_qualified),
                "tasks_completed": len(completed_tasks),
                "notes_recorded": len(notes_today),
            },
            source_type="pipeline",
            confidence="high",  # deterministic counts, not a claim to hedge
            importance=0.3,
            tags=["daily_log", now.date().isoformat()],
        )
    )

    result: dict[str, Any] = {
        "memory_id": memory.id,
        "tokens_qualified": len(newly_qualified),
        "tasks_completed": len(completed_tasks),
        "notes_recorded": len(notes_today),
        "delivered": False,
    }

    # Same "generate regardless, deliver only if a channel exists for it"
    # pattern as the Morning Brief below — no configured daily_log channel
    # is a normal state before the operator (or Annie, on request) sets one
    # up, not a failure.
    if settings.is_available("discord"):
        channel = await repo.get_discord_channel_by_purpose("daily_log")
        if channel is not None:
            from app.bots.discord_bot import send_channel_message

            result["delivered"] = await send_channel_message(
                settings.discord_bot_token, channel.channel_id, f"**{memory.title}**\n\n{content}"
            )
            result["channel_id"] = channel.channel_id

    return result


CONSOLIDATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["promote", "archive_memory_ids"],
    "properties": {
        "promote": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "content", "confidence", "importance", "tags"],
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "importance": {"type": "number"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "archive_memory_ids": {"type": "array", "items": {"type": "string"}},
    },
}

CONSOLIDATION_PROMPT = (
    "You are Annie reviewing your own recent work memory (§ memory consolidation). "
    "You'll see recent daily logs, recent research findings, and your existing "
    "long-term memories. Decide:\n"
    "- promote: NEW long-term memories worth keeping permanently — genuinely "
    "recurring patterns, durable lessons, methodological discoveries. Do NOT "
    "promote every observation; most daily activity is not worth a permanent "
    "memory. An empty list is a correct answer if nothing stands out.\n"
    "- archive_memory_ids: existing long-term memory IDs (from the list shown) "
    "that are now obsolete, superseded, or turned out not to hold up. Only IDs "
    "you were actually shown — never invent one.\n"
    "Ground every promoted memory in what the digest actually shows. A memory "
    "that can't point at real evidence from this digest should not be promoted."
)


async def _memory_consolidation(
    registry: ProviderRegistry, repo: FirestoreRepo, settings
) -> dict[str, Any]:
    """Periodic review ("Dreams") of recent activity against existing
    long-term memory — the one piece of this system's memory that requires
    judgment rather than a deterministic query, hence the single bounded
    OpenAI call (not a tool-calling loop; there's nothing to look up beyond
    what's already gathered into the digest below).
    """
    from app.db.enums import MemoryStatus, MemoryType
    from app.db.models.research import ConsolidationRun, Memory

    if not settings.is_available("ai"):
        return {"skipped": "ai not configured"}

    daily_logs, _ = await repo.list_memories(type_=MemoryType.DAILY_LOG, limit=7)
    existing_long_term = await repo.active_memories_by_importance(type_=MemoryType.LONG_TERM, limit=30)
    recent_notes = await repo.list_research_notes(current_only=True, limit=15)

    if not daily_logs and not recent_notes:
        await repo.create_consolidation_run(
            ConsolidationRun(
                run_at=datetime.now(timezone.utc),
                summary="Nothing to review — no daily logs or research findings since the last run.",
            )
        )
        return {"skipped": "nothing to consolidate"}

    digest = _consolidation_digest(daily_logs, existing_long_term, recent_notes)

    client = await registry.reasoning.raw_client()
    model = settings.openai_reasoning_model
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": CONSOLIDATION_PROMPT},
                {"role": "user", "content": digest},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "consolidation", "strict": True, "schema": CONSOLIDATION_SCHEMA},
            },
            max_completion_tokens=1500,
            temperature=0.2,
            reasoning_effort="none",
        )
    except Exception as exc:
        log.error("memory_consolidation_call_failed", exc_info=True)
        await repo.create_consolidation_run(
            ConsolidationRun(run_at=datetime.now(timezone.utc), error=str(exc)[:500], model=model)
        )
        return {"error": "OpenAI call failed — see memory_consolidation_call_failed in logs"}

    try:
        payload = json.loads(response.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        payload = {}

    promoted_ids: list[str] = []
    for item in (payload.get("promote") or [])[:5]:
        created = await repo.create_memory(
            Memory(
                type=MemoryType.LONG_TERM,
                title=str(item.get("title", ""))[:120],
                content=str(item.get("content", "")),
                confidence=item.get("confidence") or "medium",
                importance=float(item.get("importance") or 0.5),
                tags=list(item.get("tags") or []),
                source_type="consolidation",
                status=MemoryStatus.ACTIVE,
                related_research_ids=[n.id for n in recent_notes[:5]],
            )
        )
        promoted_ids.append(created.id)

    # Only archive IDs the model was actually shown — never trust an
    # invented ID to correspond to a real document.
    valid_ids = {m.id for m in existing_long_term}
    archived_ids: list[str] = []
    for mid in payload.get("archive_memory_ids") or []:
        if mid in valid_ids:
            await repo.update_memory(mid, status=MemoryStatus.ARCHIVED)
            archived_ids.append(mid)

    reviewed = len(daily_logs) + len(existing_long_term) + len(recent_notes)
    await repo.create_consolidation_run(
        ConsolidationRun(
            run_at=datetime.now(timezone.utc),
            memories_reviewed=reviewed,
            memories_promoted=len(promoted_ids),
            memories_archived=len(archived_ids),
            promoted_memory_ids=promoted_ids,
            archived_memory_ids=archived_ids,
            summary=(
                f"Reviewed {reviewed} item(s): promoted {len(promoted_ids)}, archived {len(archived_ids)}."
                if promoted_ids or archived_ids
                else f"Reviewed {reviewed} item(s): nothing met the bar for promotion or archival."
            ),
            model=model,
        )
    )

    return {
        "promoted": len(promoted_ids),
        "archived": len(archived_ids),
        "reviewed": reviewed,
    }


def _consolidation_digest(daily_logs, existing_long_term, recent_notes) -> str:
    lines = ["## Recent daily logs"]
    for m in daily_logs:
        lines.append(f"- {m.title}: {m.content[:300]}")
    lines.append("\n## Existing long-term memories (id: title — content)")
    for m in existing_long_term:
        lines.append(f"- {m.id}: {m.title} — {m.content[:200]}")
    lines.append("\n## Recent research findings")
    for n in recent_notes:
        lines.append(f"- [{n.claim_type}/{n.confidence}] {n.title}: {n.body[:200]}")
    return "\n".join(lines)


async def _morning_brief(registry: ProviderRegistry, repo: FirestoreRepo, settings) -> dict[str, Any]:
    """Generates today's daily Report (app/reports/generator.py — the same
    generator the Reports page reads) and, if Discord is configured and a
    channel has been set up for it, delivers a Discord-formatted version.

    Never assumes a channel exists: no configured `morning_brief`-purpose
    channel is a normal, expected state before the operator (or Annie, on
    request) sets one up — this generates the report either way and simply
    skips delivery, logged plainly rather than treated as a failure.
    """
    from app.reports.generator import generate_daily_report

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    report = await generate_daily_report(repo, period_start=since, period_end=now)

    if not settings.is_available("discord"):
        return {"report_id": report.id, "delivered": False, "reason": "discord not configured"}

    channel = await repo.get_discord_channel_by_purpose("morning_brief")
    if channel is None:
        return {"report_id": report.id, "delivered": False, "reason": "no channel configured for morning_brief purpose"}

    from app.bots.discord_bot import send_channel_message

    text = _format_brief_for_discord(report)
    delivered = await send_channel_message(settings.discord_bot_token, channel.channel_id, text)
    return {"report_id": report.id, "delivered": delivered, "channel_id": channel.channel_id}


def _format_brief_for_discord(report) -> str:
    lines = [f"**Annie Morning Brief — {report.title.split(' — ')[-1]}**", ""]
    if report.headline_finding:
        lines.append(f"**What changed:** {report.headline_finding}")
    if report.biggest_change:
        lines.append(f"**Biggest change:** {report.biggest_change}")
    lines.append(f"\n{report.summary or ''}")
    if report.sections.get("rising_trends"):
        lines.append("\n**Rising trends:**")
        for t in report.sections["rising_trends"][:5]:
            lines.append(f"- {t['name']}")
    if report.sections.get("worth_investigating"):
        lines.append("\n**Worth investigating:**")
        for n in report.sections["worth_investigating"][:3]:
            lines.append(f"- {n['title']}")
    if report.sections.get("data_quality"):
        lines.append("\n**Data quality issues:**")
        for d in report.sections["data_quality"][:3]:
            lines.append(f"- {d['stage']}: {d.get('notes') or 'below coverage threshold'}")
    return "\n".join(lines)


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
        name="daily_log",
        settings_key="scheduler_daily_log",
        run=_daily_log,
        default_hour=2,
        default_minute=30,
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
    ScheduledJob(
        name="memory_consolidation",
        settings_key="scheduler_memory_consolidation",
        run=_memory_consolidation,
        default_hour=4,
        default_minute=0,
        default_timezone="UTC",
    ),
    ScheduledJob(
        name="morning_brief",
        settings_key="scheduler_morning_brief",
        run=_morning_brief,
        default_hour=8,
        default_minute=0,
        default_timezone="UTC",
    ),
]
