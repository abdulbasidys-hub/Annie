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
from app.pipeline.tracking import finish_with
from app.providers.registry import ProviderRegistry
from app.scheduling.scheduler import ScheduledJob

log = structlog.get_logger(__name__)


#: How many of the newest discovery-stage tokens the frequent job checks per
#: tick. Sized against real observed volume (~16,000 discoveries/day, ~11/min)
#: plus headroom to also re-check recently-seen-but-not-yet-qualified tokens
#: each pass, while comfortably finishing within DexScreener's 4 req/s budget
#: (600 tokens / 4 req/s = 150s, well under the 10-minute interval below).
FREQUENT_QUALIFICATION_BATCH_SIZE = 600


async def _frequent_qualification(
    registry: ProviderRegistry, repo: FirestoreRepo, settings
) -> dict[str, Any]:
    """Check the newest discovered tokens for qualification every few minutes.

    This is the fix for a real, confirmed production bug (2026-08-25): with
    qualification running once a day, everything discovered after that one
    run sat unchecked for up to 24h — long past when a typical fast-moving
    Pump.fun pump had already risen and reversed. A trader watching the
    market live would see tokens cross $100k that this system's own
    once-daily spot-check never had a chance to observe, because the check
    almost never landed inside the token's brief pump window.

    Always starts from the newest end (no cursor — see
    ``FirestoreRepo.list_tokens_for_qualification``) rather than trying to
    page through the whole backlog: a brand-new token is far more likely to
    still be moving than one that has already sat unqualified for days, and
    each tick naturally re-covers whatever is still fresh. The 6-hourly full
    pipeline cycle below (``_full_pipeline_and_brief``) is the backstop that
    drains the entire backlog and eventually reaches everything else.
    """
    from app.pipeline.enrichment import run_enrichment

    run, _cursor = await run_enrichment(registry, repo, batch_size=FREQUENT_QUALIFICATION_BATCH_SIZE)
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


async def _full_pipeline_and_brief(registry: ProviderRegistry, repo: FirestoreRepo, settings) -> dict[str, Any]:
    """The four System Health pipeline stages, chained in order every 6
    hours at fixed clock times, followed by a briefing delivered to Discord
    (§ 2026-08-25: "after this one is done, the other one runs in order... I
    want Annie to analyze it and give me briefs everyday", later refined to
    "12am Nigerian time, then every 6 hours from there, fixed not flexible").

    Runs at 00:00/06:00/12:00/18:00 in Africa/Lagos (WAT, UTC+1, no DST) —
    fixed-times scheduler mode (app/scheduling/scheduler.py), not interval
    mode. Interval mode was the original design here but structurally can't
    give a chosen wall-clock time: "every 360 minutes since last run" drifts
    with whenever the process happened to last (re)start, which is exactly
    what an operator explicitly asking for "fixed not flexible" was
    rejecting. The 00:00 WAT slot is the day boundary, so it's the one that
    gets the full 24h window and the "full-day" label; the other three get
    the usual 6h window — decided directly from the current WAT-local hour
    rather than any scheduler-internal bookkeeping, so this stays correct
    even if a slot is ever missed and fires late.

    Replaces three separate standalone daily jobs that used to do this
    piecemeal on their own schedules (daily_qualification's full drain,
    narrative_clustering, morning_brief) — one chain, one history, no three
    different clocks disagreeing about when "today's" numbers were last
    refreshed. ``frequent_qualification`` (10 min) and ``trend_engine``
    (hourly) still run independently in between cycles for faster coverage;
    this is the thorough sweep, not a replacement for either.

    One stage failing does not abort the chain — discovery erroring must not
    prevent enrichment from at least trying against whatever is already
    there — each stage's own PipelineRun records the failure and the loop
    moves on; the overall full_cycle run is marked ``error`` only if every
    stage failed.
    """
    from zoneinfo import ZoneInfo

    from app.pipeline.tracking import (
        run_discovery_stage, run_enrichment_all_stage, run_narratives_stage, run_trends_stage,
    )

    now = datetime.now(timezone.utc)
    is_end_of_day = datetime.now(ZoneInfo("Africa/Lagos")).hour == 0

    full_run = await repo.create_pipeline_run("full_cycle", trigger="scheduled")
    stages: dict[str, Any] = {
        "discovery": lambda: run_discovery_stage(registry, repo, hours=24),
        "enrichment": lambda: run_enrichment_all_stage(registry, repo),
        "trends": lambda: run_trends_stage(repo),
        "narratives": lambda: run_narratives_stage(repo),
    }
    stage_results: dict[str, Any] = {}
    for stage_name, stage_fn in stages.items():
        stage_run = await repo.create_pipeline_run(stage_name, trigger="scheduled")
        try:
            stage_results[stage_name] = await finish_with(repo, stage_run.id, stage_fn())
        except Exception as exc:
            log.warning("full_pipeline_stage_failed", stage=stage_name, error=str(exc))
            stage_results[stage_name] = {"error": str(exc)}

    all_failed = all("error" in r for r in stage_results.values())
    await repo.finish_pipeline_run(
        full_run.id, status="error" if all_failed else "done", result=stage_results
    )

    brief = await _deliver_brief(repo, settings, now=now, is_end_of_day=is_end_of_day)
    return {"stages": stage_results, **brief}


async def _deliver_brief(repo: FirestoreRepo, settings, *, now: datetime, is_end_of_day: bool) -> dict[str, Any]:
    """Generates the period's Report (app/reports/generator.py — the same
    generator the Reports page reads) and, if Discord is configured and a
    channel has been set up for it, delivers a Discord-formatted version.
    ``is_end_of_day`` widens the window to the full 24h and labels it as
    such — see ``_full_pipeline_and_brief``'s docstring for how that's
    decided. Never assumes a channel exists: no configured channel is a
    normal, expected state before the operator (or Annie, on request) sets
    one up — this generates the report either way and simply skips
    delivery, logged plainly rather than treated as a failure."""
    from app.reports.generator import generate_daily_report

    period_hours = 24 if is_end_of_day else 6
    since = now - timedelta(hours=period_hours)
    report = await generate_daily_report(repo, period_start=since, period_end=now)

    if not settings.is_available("discord"):
        return {"report_id": report.id, "delivered": False, "reason": "discord not configured"}

    channel = await repo.get_discord_channel_by_purpose("morning_brief")
    if channel is None:
        return {"report_id": report.id, "delivered": False, "reason": "no channel configured for morning_brief purpose"}

    from app.bots.discord_bot import send_channel_message

    label = "Full-day briefing" if is_end_of_day else "6-hour briefing"
    text = _format_brief_for_discord(report, label=label)
    delivered = await send_channel_message(settings.discord_bot_token, channel.channel_id, text)
    return {"report_id": report.id, "delivered": delivered, "channel_id": channel.channel_id, "is_end_of_day": is_end_of_day}


def _format_brief_for_discord(report, *, label: str = "Briefing") -> str:
    lines = [f"**Annie {label} — {report.title.split(' — ')[-1]}**", ""]
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


async def _trend_engine(registry: ProviderRegistry, repo: FirestoreRepo, settings) -> dict[str, Any]:
    """Recompute trends against the latest qualified-token cohorts.

    This had no scheduled job at all until 2026-08-25 — only the manual
    "Run now" trigger on System Health (app/api/routes/system.py's
    /run/trends) ever called TrendEngine.run(). Invisible while qualification
    itself was broken (nothing new to compare), but the moment the
    qualification-frequency fix started producing real data (1 -> 290
    qualified tokens in a few hours), every existing trend document was
    stuck at whatever tiny cohort (recent_total=1) existed the one time this
    was ever manually triggered — confirmed directly: a chat request for
    "new" trends returned zero rows because none of the 18 stale documents
    could pass the significance bars, while the dashboard's raw count still
    said 18, an inconsistency Annie correctly refused to paper over. Runs
    hourly rather than daily: qualification itself now updates every 10
    minutes, so trends frozen for a full day would just recreate a milder
    version of the same staleness.
    """
    from app.trends.engine import TrendEngine

    engine = TrendEngine(repo)
    run = await engine.run()
    return {
        "cohorts_evaluated": run.cohorts_evaluated,
        "features_evaluated": run.features_evaluated,
        "trends_created": run.trends_created,
        "trends_updated": run.trends_updated,
        "status_changes": run.status_changes,
        "skipped_windows": run.skipped_windows,
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
        name="frequent_qualification",
        settings_key="scheduler_frequent_qualification",
        run=_frequent_qualification,
        default_interval_minutes=10,
    ),
    ScheduledJob(
        name="full_pipeline_and_brief",
        settings_key="scheduler_full_pipeline_and_brief",
        run=_full_pipeline_and_brief,
        # Fixed clock times, not interval-mode (§ 2026-08-25 — "12am
        # Nigerian time, then every 6 hours from there, fixed not
        # flexible"): interval-mode's "every 360 minutes since last run"
        # drifts with whenever the process last restarted and can never
        # land on a chosen wall-clock time.
        default_hours=[0, 6, 12, 18],
        default_minute=0,
        default_timezone="Africa/Lagos",
    ),
    ScheduledJob(
        name="trend_engine",
        settings_key="scheduler_trend_engine",
        run=_trend_engine,
        default_interval_minutes=60,
    ),
    ScheduledJob(
        name="daily_log",
        settings_key="scheduler_daily_log",
        run=_daily_log,
        default_hour=0,
        default_minute=0,
        default_timezone="Africa/Lagos",
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
]
