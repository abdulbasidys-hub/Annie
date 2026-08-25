"""In-process job scheduler — daily, fixed-multiple-times-daily, and frequent
interval jobs.

No external queue, cron, or library — see README's former "no scheduler is
wired up" gap this closes. Follows the exact background-task pattern
``app/main.py``'s ``_bot_tasks`` already uses: a loop started in ``lifespan``,
cancelled cleanly at shutdown, not a separate process.

Each job's trigger is operator-configurable through the existing ``Setting``
mechanism (System Health / Settings page, ``PATCH /api/system/settings/{key}``)
rather than hardcoded or requiring a redeploy (see ``src/pages/Settings.jsx``).
Three modes, picked by which fields a :class:`ScheduledJob` sets:

- **Daily** (``default_hour``/``default_minute``/``default_timezone``): fires
  once per calendar day in its own configured timezone, the first tick after
  "today hasn't run yet, and local time is past the trigger minute". Setting
  value shape: ``{"enabled", "hour", "minute", "timezone", "last_run_date"}``.
- **Fixed times** (``default_hours``, a list): fires once per listed hour
  per calendar day, same "hasn't fired at this slot today yet" rule applied
  per slot rather than once for the whole job — added 2026-08-25 because an
  operator explicitly wanted clock-anchored runs ("12am Nigerian time, then
  every 6 hours from there, fixed not flexible"), which interval-mode
  structurally cannot give: interval-mode's "N minutes since last run" drifts
  with whenever the process happened to start or last restart, never lands
  on a chosen wall-clock time. Setting value shape: ``{"enabled", "hours",
  "minute", "timezone", "last_fired": {"<hour>": "<date last fired>"}}``.
- **Interval** (``default_interval_minutes``): fires whenever at least that
  many minutes have passed since its last run (or immediately, if it has
  never run). This exists because a once-a-day cadence is structurally wrong
  for anything reacting to a market that moves in minutes — the qualification
  job was daily-only until 2026-08-25, when real production data showed
  16,602 of 16,774 discovered tokens had *never* been evaluated because
  everything discovered after the one daily run sat unchecked for up to 24h,
  long past when a typical Pump.fun pump had already risen and reversed. See
  ``app/scheduling/jobs.py``'s ``frequent_qualification``. Setting value
  shape: ``{"enabled", "interval_minutes", "last_run_at"}``.

Checking every 60 seconds rather than sleeping until the exact instant means
a missed process restart self-heals on the next tick instead of silently
skipping a run, and a daily (or fixed-times) job can never fire twice for
the same slot on the same local day.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

from app.config import Settings
from app.db.repo import FirestoreRepo
from app.providers.registry import ProviderRegistry

log = structlog.get_logger(__name__)

#: How often the loop wakes to check whether any job is due. A job's own
#: configured hour/minute/interval controls *when* it fires, not this — this
#: only bounds how late a fire can be relative to its target.
CHECK_INTERVAL_SECONDS = 60

JobFn = Callable[[ProviderRegistry, FirestoreRepo, Settings], Awaitable[dict[str, Any]]]


@dataclass(slots=True)
class ScheduledJob:
    """One scheduled job — daily, fixed-times, or interval; see the module
    docstring. Set exactly one of ``default_interval_minutes`` (interval),
    ``default_hours`` (fixed times), or neither (falls back to the single-hour
    daily mode using ``default_hour``/``default_minute``).
    """

    name: str
    settings_key: str
    run: JobFn
    default_hour: int = 2
    default_minute: int = 0
    default_timezone: str = "UTC"
    default_enabled: bool = True
    default_interval_minutes: int | None = None
    default_hours: list[int] | None = None


class Scheduler:
    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        repo: FirestoreRepo,
        settings: Settings,
        jobs: list[ScheduledJob],
    ) -> None:
        self._registry = registry
        self._repo = repo
        self._settings = settings
        self._jobs = jobs
        self._stopped = False

    async def run(self) -> None:
        log.info("scheduler_started", jobs=[j.name for j in self._jobs])
        await self._ensure_defaults_visible()
        while not self._stopped:
            for job in self._jobs:
                # Fire-and-forget, not awaited inline: this loop used to
                # await each job's _maybe_run in turn, which meant a single
                # long-running job (full_pipeline_and_brief's full-backlog
                # drain can take hours against a large enough backlog)
                # blocked every *other* job — including frequent_qualification,
                # whose entire purpose is running every 10 minutes — from
                # even being checked until it finished. Confirmed as a real
                # risk 2026-08-25 once the backlog grew past 50,000 tokens.
                # last_run_at is written before the job body runs (see
                # _run_job), so a job already in flight correctly appears
                # "recently started" to the next tick's check regardless of
                # how long it's still running for.
                asyncio.create_task(self._maybe_run_safe(job))
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    async def _maybe_run_safe(self, job: ScheduledJob) -> None:
        try:
            await self._maybe_run(job)
        except Exception:
            # A bug in one job's *scheduling check* (not the job itself,
            # which already has its own try/except in _run_job) must not
            # take every other job down with it.
            log.error("scheduler_tick_failed", job=job.name, exc_info=True)

    def stop(self) -> None:
        self._stopped = True

    async def _ensure_defaults_visible(self) -> None:
        """Write each job's default config the first time it's seen, so it
        shows up as an editable row on the Settings page (and can be turned
        off or rescheduled) before it has ever actually run — not only
        after, which is when `_run_job` would otherwise first write it."""
        for job in self._jobs:
            existing = await self._repo.get_setting(job.settings_key)
            if existing is None:
                if job.default_interval_minutes is not None:
                    value: dict[str, Any] = {
                        "enabled": job.default_enabled,
                        "interval_minutes": job.default_interval_minutes,
                        "last_run_at": None,
                        "last_result": None,
                    }
                    desc = (
                        f"Scheduled job '{job.name}'. Edit interval_minutes/enabled as JSON "
                        f"to change how often it runs — no redeploy needed."
                    )
                elif job.default_hours is not None:
                    value = {
                        "enabled": job.default_enabled,
                        "hours": list(job.default_hours),
                        "minute": job.default_minute,
                        "timezone": job.default_timezone,
                        "last_fired": {},
                        "last_run_at": None,
                        "last_result": None,
                    }
                    desc = (
                        f"Scheduled job '{job.name}'. Edit hours (a list)/minute/timezone/enabled "
                        f"as JSON to change when it runs — no redeploy needed."
                    )
                else:
                    value = {
                        "enabled": job.default_enabled,
                        "hour": job.default_hour,
                        "minute": job.default_minute,
                        "timezone": job.default_timezone,
                        "last_run_date": None,
                        "last_run_at": None,
                        "last_result": None,
                    }
                    desc = (
                        f"Scheduled job '{job.name}'. Edit hour/minute/timezone/enabled as JSON "
                        f"to change when it runs — no redeploy needed."
                    )
                await self._repo.upsert_setting(
                    job.settings_key, value, description=desc, actor="scheduler"
                )

    async def _maybe_run(self, job: ScheduledJob) -> None:
        config = await self._config_for(job)
        if not config["enabled"]:
            return

        if job.default_interval_minutes is not None:
            last_run_at = config.get("last_run_at")
            if last_run_at:
                elapsed = datetime.now(timezone.utc) - _parse_iso(last_run_at)
                interval = timedelta(minutes=config.get("interval_minutes") or job.default_interval_minutes)
                if elapsed < interval:
                    return
            await self._run_job(job, config)
            return

        if job.default_hours is not None:
            tz = _safe_zone(config["timezone"], job.name)
            now_local = datetime.now(tz)
            today = now_local.date().isoformat()
            last_fired: dict[str, Any] = dict(config.get("last_fired") or {})

            for hour in config["hours"]:
                if last_fired.get(str(hour)) == today:
                    continue
                trigger = now_local.replace(hour=hour, minute=config["minute"], second=0, microsecond=0)
                if now_local < trigger:
                    continue
                last_fired[str(hour)] = today
                await self._run_job(job, config, extra={"last_fired": last_fired})
                return  # at most one slot fires per tick; the next tick picks up any other due slot
            return

        tz = _safe_zone(config["timezone"], job.name)
        now_local = datetime.now(tz)
        today = now_local.date().isoformat()

        if config.get("last_run_date") == today:
            return
        trigger = now_local.replace(
            hour=config["hour"], minute=config["minute"], second=0, microsecond=0
        )
        if now_local < trigger:
            return

        await self._run_job(job, config, extra={"last_run_date": today})

    async def _config_for(self, job: ScheduledJob) -> dict[str, Any]:
        setting = await self._repo.get_setting(job.settings_key)
        if job.default_interval_minutes is not None:
            base: dict[str, Any] = {
                "enabled": job.default_enabled,
                "interval_minutes": job.default_interval_minutes,
            }
        elif job.default_hours is not None:
            base = {
                "enabled": job.default_enabled,
                "hours": list(job.default_hours),
                "minute": job.default_minute,
                "timezone": job.default_timezone,
                "last_fired": {},
            }
        else:
            base = {
                "enabled": job.default_enabled,
                "hour": job.default_hour,
                "minute": job.default_minute,
                "timezone": job.default_timezone,
            }
        if setting and isinstance(setting.value, dict):
            base.update(setting.value)
        return base

    async def _run_job(
        self, job: ScheduledJob, config: dict[str, Any], *, extra: dict[str, Any] | None = None
    ) -> None:
        """Records ``last_run_at`` *before* running the job, not only after —
        confirmed as a real, actively-harmful bug (2026-08-25): a job whose
        body can run for minutes to hours (``full_pipeline_and_brief``'s full
        enrichment drain) only had its completion recorded in a ``finally``
        block, so a process restart mid-run (a redeploy — several happened
        in quick succession) killed the task before it ever wrote anything,
        leaving Firestore's ``last_run_at`` stale. The next process's very
        first tick then saw "hasn't run in ages" and fired again immediately
        — observed firing every 6-16 minutes instead of the configured 360,
        with multiple full cycles running concurrently by the time this was
        caught. Writing the timestamp at start means even a mid-run kill
        leaves an accurate, recent ``last_run_at`` behind, so the interval/
        daily/fixed-times gate in ``_maybe_run`` holds across restarts
        instead of only across clean completions.

        ``extra`` carries whichever slot-tracking field this mode uses
        (``last_run_date`` for daily, ``last_fired`` for fixed-times, nothing
        for interval) — kept generic here so this method doesn't need to
        know which mode called it.
        """
        log.info("scheduled_job_starting", job=job.name)
        tz = timezone.utc if job.default_interval_minutes is not None else _safe_zone(
            config.get("timezone", "UTC"), job.name
        )
        started = datetime.now(tz)
        if extra:
            config.update(extra)
        config["last_run_at"] = started.isoformat()
        await self._repo.upsert_setting(job.settings_key, config, actor="scheduler")

        try:
            result = await job.run(self._registry, self._repo, self._settings)
            log.info("scheduled_job_complete", job=job.name, result=result)
        except Exception:
            log.error("scheduled_job_failed", job=job.name, exc_info=True)
            result = {"error": "job raised — see server logs for scheduled_job_failed"}
        finally:
            config["last_result"] = result
            await self._repo.upsert_setting(job.settings_key, config, actor="scheduler")


def _parse_iso(value: Any) -> datetime:
    dt = datetime.fromisoformat(str(value))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _safe_zone(name: Any, job_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        log.warning("scheduler_bad_timezone", job=job_name, timezone=name)
        return ZoneInfo("UTC")
