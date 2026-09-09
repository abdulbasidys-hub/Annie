"""The scheduled jobs — Annie's cycle, after the 2026-09-08 memory rewrite.

Five jobs, and the shape of them is the whole design:

``watch`` (10 min)
    Re-price the highest-priority slice of the watchlist in batched
    requests. Local writes only. This is the one that runs often, because a
    memecoin's entire run can happen inside an hour and a check that lands
    once a day almost never lands while the token is interesting.

``cycle`` (00:00 / 06:00 / 12:00 / 18:00 WAT)
    The thinking pass. Recompute signals from the ledger (free), build a
    digest (free), then **one** bounded model call that returns edits to the
    memory files. Followed by dossier refresh, pruning, and a Firestore
    snapshot of whatever markdown changed. At the 00:00 slot it also writes
    the day's deterministic log and delivers the briefing.

``weekly_rollup`` (Monday 01:00 WAT)
    One model call reading the week's daily files.

``monthly_rollup`` (1st of month, 02:00 WAT)
    One model call reading the month's weekly files. Never sees raw data —
    that cascade is what keeps cost flat as history grows.

``housekeeping`` (03:00 UTC)
    Prune the ledger, prune old dailies, reconcile the search index, vacuum.

Total scheduled model spend: four small calls a day, plus one weekly and one
monthly. Everything else — counting, ranking, filtering, statistics, file
writes — is deterministic Python over local SQLite and costs nothing. That
is the guard against the obvious failure of this rewrite, which would have
been to move the bill from Firestore to OpenAI.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from app.db.repo import FirestoreRepo
from app.providers.registry import ProviderRegistry
from app.scheduling.scheduler import ScheduledJob

log = structlog.get_logger(__name__)


async def _watch(
    registry: ProviderRegistry, repo: FirestoreRepo, settings, *, slot: int | None = None
) -> dict[str, Any]:
    """Re-price the watchlist. Batched, local, no Firestore."""
    from app.pipeline.watch import run_watch

    run = await run_watch(registry, settings)
    return run.to_dict()


async def _cycle(
    registry: ProviderRegistry,
    repo: FirestoreRepo,
    settings,
    *,
    slot: int | None = None,
) -> dict[str, Any]:
    """One full thinking cycle: observe, learn, tidy, back up.

    Ordered so that the paid step happens against the freshest free work,
    and so that a failure late in the chain cannot lose what was already
    learned — memory files are written to disk by the learning step itself,
    long before the snapshot at the end.

    Stage failures do not abort the cycle. Signals failing must not prevent
    learning from running against the digest, and a Firestore snapshot
    failing at the end must not make the whole cycle look failed when the
    memory on disk is already correct.

    ``slot`` is which configured hour this run is *for*, passed down by the
    scheduler. It is not the current hour, and the difference is the whole
    point: the scheduler self-heals a missed slot by firing it late, so a
    midnight run that actually starts at 01:30 after a redeploy is still the
    midnight run.

    This used to read ``datetime.now(Africa/Lagos).hour == 0`` instead, with
    a comment claiming that "stays correct even if a slot is ever missed and
    fires late". It is exactly backwards — a late midnight slot sees hour 1
    and silently skips the daily log, the launch ideas and the full-day
    brief for that entire day. Confirmed in production 2026-09-09: a deploy
    near midnight, and the day's brief never arrived.
    """
    from zoneinfo import ZoneInfo

    from app.memory import bootstrap, ideas, ledger, rollup, service, signals, snapshot
    from app.memory.learn import learn_from_window
    from app.pipeline.watch import enrich_qualified, refresh_tracked_creators

    now = datetime.now(timezone.utc)
    # Told, not guessed. Falls back to the wall clock only for a manual run,
    # which has no slot — see run_cycle_now's `full_day` for forcing it.
    is_day_boundary = (
        slot == 0 if slot is not None
        else datetime.now(ZoneInfo("Africa/Lagos")).hour == 0
    )
    result: dict[str, Any] = {
        "at": now.isoformat(timespec="seconds"),
        "slot": slot,
        "day_boundary": is_day_boundary,
    }

    async def stage(name: str, coro) -> None:
        try:
            result[name] = await coro
        except Exception as exc:
            log.warning("cycle_stage_failed", stage=name, error=str(exc), exc_info=True)
            result[name] = {"error": str(exc)[:200]}

    # -- free work ------------------------------------------------------------
    try:
        result["signals"] = signals.recompute(now).to_dict()
    except Exception as exc:
        log.warning("cycle_stage_failed", stage="signals", exc_info=True)
        result["signals"] = {"error": str(exc)[:200]}

    await stage("enrichment", enrich_qualified(registry, settings, limit=25))

    # -- the one paid call ----------------------------------------------------
    window_hours = 24 if is_day_boundary else 6
    try:
        learned = await learn_from_window(registry, settings, window_hours=window_hours, now=now)
        result["learning"] = learned.to_dict()
    except Exception as exc:
        log.error("cycle_learning_failed", exc_info=True)
        result["learning"] = {"error": str(exc)[:200]}
        learned = None

    # -- deterministic follow-up ---------------------------------------------
    await stage("dossiers", refresh_tracked_creators(limit=15))

    if is_day_boundary:
        await stage("daily_log", rollup.write_daily_log(now=now))
        # Once a day, not once a cycle. Ideas are a judgement about what to
        # do next, and one that changes every six hours is noise — a day is
        # roughly the shortest window over which "what is working" means
        # anything here. Skips itself when nothing moved.
        await stage("ideas", ideas.generate_daily(registry, settings, count=3, now=now))

    try:
        tracked = ledger.top_creators(limit=12, tracked_only=True)
        watched_tokens = ledger.movers(since_hours=48, limit=10)
        bootstrap.sync_watchlist_section(
            creators=[
                f"`{c['wallet']}` — {c['launches']} launches, {c['winners']} winners"
                for c in tracked
            ],
            narratives=(learned.watch_narratives if learned else []),
            tokens=[
                f"{t.symbol or t.mint[:8]} — `{t.mint}`" for t in watched_tokens
            ],
        )
        result["watchlist_updated"] = True
    except Exception:
        log.warning("watchlist_sync_failed", exc_info=True)
        result["watchlist_updated"] = False

    # -- durability -----------------------------------------------------------
    # Last, and allowed to fail: the files are already on disk. This only
    # writes memory documents whose content hash changed, so a quiet cycle
    # costs zero Firestore writes.
    await stage("snapshot", snapshot.snapshot_all(service.store()))

    brief = await _deliver_brief(repo, settings, result, now=now, full_day=is_day_boundary)
    result.update(brief)
    return result


async def _deliver_brief(
    repo: FirestoreRepo, settings, cycle: dict[str, Any], *, now: datetime, full_day: bool
) -> dict[str, Any]:
    """Post the cycle's headline to Discord, if a channel is set up for it.

    Reads what the cycle already produced rather than re-querying anything.
    No configured channel is a normal state before the operator (or Annie,
    on request) sets one up — that is reported, not treated as a failure.
    """
    if not settings.is_available("discord"):
        log.warning("brief_not_delivered", reason="discord not configured")
        return {"delivered": False, "reason": "discord not configured"}

    channel = await repo.get_discord_channel_by_purpose("morning_brief")
    if channel is None:
        # Logged at warning, not info. This is the single most common reason
        # a brief "never arrives": everything ran correctly and there was
        # simply nowhere to send it. Silently returning a reason nobody reads
        # made a working system look broken.
        log.warning(
            "brief_not_delivered",
            reason="no Discord channel has purpose 'morning_brief'",
            fix="ask Annie in Discord to create one, or set the purpose on an "
                "existing channel",
        )
        missing = {
            "delivered": False,
            "reason": "no Discord channel is set up with purpose 'morning_brief' — "
                      "set one on System Health, or ask Annie in Discord to create it",
        }
        if full_day:
            # The ideas have their own channel purpose, so a deployment that
            # configured only that one should still get them. Losing the
            # brief is not a reason to also drop the thing the brief was
            # merely going to sit above.
            missing.update(await _deliver_ideas(repo, settings, cycle, fallback=None))
        return missing

    from app.bots.discord_bot import send_channel_message
    from app.memory import ledger

    label = "Daily brief" if full_day else "6-hour brief"
    learning = cycle.get("learning") or {}
    stats = ledger.stats()

    lines = [f"**Annie — {label}, {now.strftime('%d %b %H:%M UTC')}**", ""]
    headline = learning.get("headline")
    if headline:
        lines.append(headline)
    lines.append(
        f"\nSeen in 24h: {stats['sightings_24h']} launches · "
        f"{stats['qualified_24h']} reached a tier · "
        f"watching {stats['watching']} · tracking {stats['creators_tracked']} creators"
    )

    qualified = ledger.qualified_in_window(now - timedelta(hours=24 if full_day else 6), now)
    if qualified:
        lines.append("\n**Crossed a tier:**")
        for token in qualified[:5]:
            lines.append(
                f"- {token.symbol or token.mint[:8]} — peak "
                f"${(token.peak_market_cap or 0) / 1000:.0f}k · `{token.mint}`"
            )

    applied = learning.get("applied") or []
    if applied:
        lines.append("\n**Memory updated:**")
        for edit in applied[:5]:
            lines.append(f"- `{edit['path']}` ({edit['op']})")

    delivered = await send_channel_message(
        settings.discord_bot_token, channel.channel_id, "\n".join(lines)
    )
    outcome: dict[str, Any] = {"delivered": delivered, "channel_id": channel.channel_id}
    if full_day:
        outcome.update(await _deliver_ideas(repo, settings, cycle, fallback=channel))
    return outcome


async def _deliver_ideas(
    repo: FirestoreRepo, settings, cycle: dict[str, Any], *, fallback
) -> dict[str, Any]:
    """Post the day's three launch ideas, as their own message.

    Separate from the brief rather than appended to it, for two reasons. The
    brief is a report and the ideas are a proposal — different things to do
    something with, and worth being able to route to different channels. And
    the ideas are long: pinning or quoting one is a normal thing to want,
    and that is awkward when they are the tail of a status summary.

    Routing prefers a channel whose purpose is ``launch_ideas`` and falls
    back to the brief channel, so configuring one channel is enough to start
    and a second is an upgrade rather than a prerequisite.
    """
    from app.bots.discord_bot import send_channel_message
    from app.memory import ideas

    generated = (cycle.get("ideas") or {}).get("generated")
    if not generated:
        # Not a failure. `generate_daily` skips itself when nothing moved,
        # which is the honest output — three speculative ideas from no data
        # would arrive looking exactly like grounded ones.
        return {"ideas_delivered": False, "ideas_reason": "none were generated"}

    latest = ideas.latest(origin="daily")
    text = ideas.format_for_delivery(latest, limit=3) if latest else ""
    if not text:
        log.warning("ideas_not_delivered", reason="generated but could not be formatted")
        return {"ideas_delivered": False, "ideas_reason": "could not be formatted"}

    target = await repo.get_discord_channel_by_purpose("launch_ideas") or fallback
    if target is None:
        log.warning("ideas_not_delivered", reason="no channel for 'launch_ideas' or 'morning_brief'")
        return {
            "ideas_delivered": False,
            "ideas_reason": "no Discord channel is set up for launch ideas",
        }

    delivered = await send_channel_message(
        settings.discord_bot_token, target.channel_id, text
    )
    if not delivered:
        log.warning(
            "ideas_not_delivered",
            reason="discord rejected the send",
            channel_id=target.channel_id,
        )
    return {
        "ideas_delivered": delivered,
        "ideas_channel_id": target.channel_id,
        "ideas_count": generated,
    }


async def _weekly_rollup(
    registry: ProviderRegistry, repo: FirestoreRepo, settings, *, slot: int | None = None
) -> dict[str, Any]:
    from app.memory import snapshot
    from app.memory.rollup import write_weekly_summary

    result = await write_weekly_summary(registry, settings)
    await snapshot.snapshot_all()
    return result


async def _monthly_rollup(
    registry: ProviderRegistry, repo: FirestoreRepo, settings, *, slot: int | None = None
) -> dict[str, Any]:
    from app.memory import snapshot
    from app.memory.rollup import prune_old_dailies, write_monthly_summary

    result = await write_monthly_summary(registry, settings)
    result["dailies_pruned"] = len(await prune_old_dailies())
    await snapshot.snapshot_all()
    return result


async def _housekeeping(
    registry: ProviderRegistry, repo: FirestoreRepo, settings, *, slot: int | None = None
) -> dict[str, Any]:
    """Forgetting, on a schedule. The job that keeps this from becoming a database.

    Everything here is local and free, which is why it can afford to run
    every day rather than being a thing someone remembers to do when storage
    gets tight.
    """
    from app.memory import index, ledger

    pruned = ledger.prune(ttl_hours=settings.watch_ttl_hours)
    reindexed = index.sync_if_stale()

    # VACUUM rewrites the whole file, so it is worth doing only after a
    # prune that actually removed a lot. Below that it is pure I/O for no
    # meaningful space back.
    if pruned["sightings_dropped"] > 5000:
        ledger.vacuum()
        pruned["vacuumed"] = True

    return {"pruned": pruned, "reindexed": reindexed, "index": index.stats()}


async def _research_task_sweep(
    registry: ProviderRegistry, repo: FirestoreRepo, settings
) -> dict[str, Any]:
    """Catch any ResearchTask left ``queued`` by a restart.

    Normally a task starts within seconds of creation (the fire-and-forget
    trigger in ``app/api/routes/intelligence.py``); this only matters for
    one orphaned in that window. Kept on Firestore because research tasks
    are operator-initiated, low-volume, and need to be visible across
    processes — exactly the profile Firestore is still the right tool for.
    """
    from app.db.enums import ResearchTaskStatus
    from app.research.runner import run_research_task

    tasks, _ = await repo.list_research_tasks(status=ResearchTaskStatus.QUEUED, limit=20)
    for task in tasks:
        await run_research_task(task.id, repo=repo, registry=registry, settings=settings)
    return {"swept": len(tasks)}


#: Every job the scheduler runs. Each entry's ``settings_key`` is what shows
#: up as an editable row on the Settings page — change the trigger time,
#: timezone or enabled flag there, no redeploy needed.
JOBS: list[ScheduledJob] = [
    ScheduledJob(
        name="watch",
        settings_key="scheduler_watch",
        run=_watch,
        default_interval_minutes=10,
    ),
    ScheduledJob(
        name="cycle",
        settings_key="scheduler_cycle",
        run=_cycle,
        # Fixed clock times, not interval mode (§ 2026-08-25 — "12am
        # Nigerian time, then every 6 hours from there, fixed not
        # flexible"): interval mode's "every 360 minutes since last run"
        # drifts with whenever the process last restarted and can never land
        # on a chosen wall-clock time.
        default_hours=[0, 6, 12, 18],
        default_minute=0,
        default_timezone="Africa/Lagos",
    ),
    ScheduledJob(
        name="weekly_rollup",
        settings_key="scheduler_weekly_rollup",
        run=_weekly_rollup,
        default_weekday=0,  # Monday, summarising the week that just ended
        default_hour=1,
        default_minute=0,
        default_timezone="Africa/Lagos",
    ),
    ScheduledJob(
        name="monthly_rollup",
        settings_key="scheduler_monthly_rollup",
        run=_monthly_rollup,
        default_day_of_month=1,  # summarising the month that just ended
        default_hour=2,
        default_minute=0,
        default_timezone="Africa/Lagos",
    ),
    ScheduledJob(
        name="housekeeping",
        settings_key="scheduler_housekeeping",
        run=_housekeeping,
        default_hour=3,
        default_minute=0,
        default_timezone="UTC",
    ),
    ScheduledJob(
        name="research_task_sweep",
        settings_key="scheduler_research_task_sweep",
        run=_research_task_sweep,
        default_hour=4,
        default_minute=0,
        default_timezone="UTC",
    ),
]
