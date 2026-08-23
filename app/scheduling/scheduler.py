"""In-process daily job scheduler.

No external queue, cron, or library — see README's former "no scheduler is
wired up" gap this closes. Follows the exact background-task pattern
``app/main.py``'s ``_bot_tasks`` already uses: a loop started in ``lifespan``,
cancelled cleanly at shutdown, not a separate process.

Each job's trigger time is operator-configurable through the existing
``Setting`` mechanism (System Health / Settings page,
``PATCH /api/system/settings/{key}``) rather than hardcoded or requiring a
redeploy — the value is a small JSON object: ``{"enabled", "hour", "minute",
"timezone"}``, editable as raw JSON the same way any other setting already is
(see ``src/pages/Settings.jsx``).

A job fires once per calendar day **in its own configured timezone**, the
first time this loop notices "today hasn't run yet, and local time is past
the configured hour:minute". Checking every 60 seconds rather than sleeping
until the exact instant means a missed process restart at the trigger minute
self-heals (the next tick after the process comes back up still satisfies
"today, past the trigger time, not yet run") instead of silently skipping a
whole day, and a job can never fire twice in the same local day.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

from app.config import Settings
from app.db.repo import FirestoreRepo
from app.providers.registry import ProviderRegistry

log = structlog.get_logger(__name__)

#: How often the loop wakes to check whether any job is due. A job's own
#: configured hour/minute controls *when* it fires, not this — this only
#: bounds how late a fire can be relative to its target minute.
CHECK_INTERVAL_SECONDS = 60

JobFn = Callable[[ProviderRegistry, FirestoreRepo, Settings], Awaitable[dict[str, Any]]]


@dataclass(slots=True)
class ScheduledJob:
    """One daily job. ``settings_key`` is where its live config lives —
    see the module docstring for the value shape."""

    name: str
    settings_key: str
    run: JobFn
    default_hour: int = 2
    default_minute: int = 0
    default_timezone: str = "UTC"
    default_enabled: bool = True


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
                try:
                    await self._maybe_run(job)
                except Exception:
                    # A bug in one job's *scheduling check* (not the job
                    # itself, which already has its own try/except below)
                    # must not take every other job down with it.
                    log.error("scheduler_tick_failed", job=job.name, exc_info=True)
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

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
                await self._repo.upsert_setting(
                    job.settings_key,
                    {
                        "enabled": job.default_enabled,
                        "hour": job.default_hour,
                        "minute": job.default_minute,
                        "timezone": job.default_timezone,
                        "last_run_date": None,
                        "last_run_at": None,
                        "last_result": None,
                    },
                    actor="scheduler",
                )

    async def _maybe_run(self, job: ScheduledJob) -> None:
        config = await self._config_for(job)
        if not config["enabled"]:
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

        await self._run_job(job, config, today)

    async def _config_for(self, job: ScheduledJob) -> dict[str, Any]:
        setting = await self._repo.get_setting(job.settings_key)
        base: dict[str, Any] = {
            "enabled": job.default_enabled,
            "hour": job.default_hour,
            "minute": job.default_minute,
            "timezone": job.default_timezone,
        }
        if setting and isinstance(setting.value, dict):
            base.update(setting.value)
        return base

    async def _run_job(self, job: ScheduledJob, config: dict[str, Any], today: str) -> None:
        log.info("scheduled_job_starting", job=job.name)
        started = datetime.now(tz=_safe_zone(config["timezone"], job.name))
        try:
            result = await job.run(self._registry, self._repo, self._settings)
            log.info("scheduled_job_complete", job=job.name, result=result)
        except Exception:
            log.error("scheduled_job_failed", job=job.name, exc_info=True)
            result = {"error": "job raised — see server logs for scheduled_job_failed"}
        finally:
            config["last_run_date"] = today
            config["last_run_at"] = started.isoformat()
            config["last_result"] = result
            await self._repo.upsert_setting(job.settings_key, config, actor="scheduler")


def _safe_zone(name: Any, job_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        log.warning("scheduler_bad_timezone", job=job_name, timezone=name)
        return ZoneInfo("UTC")
