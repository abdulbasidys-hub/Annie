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

    memory = await repo.create_memory(
        Memory(
            type=MemoryType.DAILY_LOG,
            title=f"Daily log — {now.date().isoformat()}",
            content="\n".join(lines),
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
    return {
        "memory_id": memory.id,
        "tokens_qualified": len(newly_qualified),
        "tasks_completed": len(completed_tasks),
        "notes_recorded": len(notes_today),
    }


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
    from app.db.models.research import Memory

    if not settings.is_available("ai"):
        return {"skipped": "ai not configured"}

    daily_logs, _ = await repo.list_memories(type_=MemoryType.DAILY_LOG, limit=7)
    existing_long_term = await repo.active_memories_by_importance(type_=MemoryType.LONG_TERM, limit=30)
    recent_notes = await repo.list_research_notes(current_only=True, limit=15)

    if not daily_logs and not recent_notes:
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
    except Exception:
        log.error("memory_consolidation_call_failed", exc_info=True)
        return {"error": "OpenAI call failed — see memory_consolidation_call_failed in logs"}

    try:
        payload = json.loads(response.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        payload = {}

    promoted = 0
    for item in (payload.get("promote") or [])[:5]:
        await repo.create_memory(
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
        promoted += 1

    # Only archive IDs the model was actually shown — never trust an
    # invented ID to correspond to a real document.
    valid_ids = {m.id for m in existing_long_term}
    archived = 0
    for mid in payload.get("archive_memory_ids") or []:
        if mid in valid_ids:
            await repo.update_memory(mid, status=MemoryStatus.ARCHIVED)
            archived += 1

    return {
        "promoted": promoted,
        "archived": archived,
        "reviewed": len(daily_logs) + len(existing_long_term) + len(recent_notes),
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
]
