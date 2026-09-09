"""In-process job scheduler — interval, daily, fixed-times, weekly and monthly.

No external queue, cron, or library. Follows the same background-task pattern
``app/main.py``'s ``_bot_tasks`` uses: a loop started in ``lifespan``,
cancelled cleanly at shutdown, not a separate process.

Each job's trigger is operator-configurable through the ``Setting`` mechanism
(Settings page, ``PATCH /api/system/settings/{key}``) rather than hardcoded.
Five modes, picked by which fields a :class:`ScheduledJob` sets:

- **Interval** (``default_interval_minutes``): fires whenever that many
  minutes have passed since its last run. A once-a-day cadence is
  structurally wrong for anything reacting to a market that moves in
  minutes.
- **Daily** (``default_hour``/``default_minute``/``default_timezone``): once
  per calendar day in its own timezone.
- **Fixed times** (``default_hours``, a list): once per listed hour per day.
  Interval mode structurally cannot give a chosen wall-clock time — "N
  minutes since last run" drifts with whenever the process last restarted —
  which is why this mode exists (§ 2026-08-25, "fixed not flexible").
- **Weekly** (``default_weekday``, 0=Monday): daily rules, plus a weekday gate.
- **Monthly** (``default_day_of_month``): daily rules, plus a day gate, with
  the day clamped to the month's length so 31 still fires in February.

**Where run state lives (changed 2026-09-08).** Config — enabled, hour,
interval, timezone — stays in Firestore, because that is what the operator
edits and it must survive a redeploy. Run *state* — ``last_run_at``,
``last_fired``, ``last_result`` — moved to local SQLite. The reason is
arithmetic: the previous version wrote the whole setting document twice per
job run and read it once per job per 60-second tick. With a job on a
10-minute interval that is ~288 Firestore writes and ~8,600 reads a day for
pure bookkeeping, on a plan allowing 20,000 writes total. Run state is
per-deployment and reconstructible, so SQLite is simply where it belongs;
config is read through a TTL cache (:data:`CONFIG_CACHE_SECONDS`) so an
operator's edit still takes effect within a few minutes without a read on
every tick.

Checking every 60 seconds rather than sleeping until the exact instant means
a missed process restart self-heals on the next tick instead of silently
skipping a run, and a daily job can never fire twice for the same local day.

That self-healing is bounded (:data:`DEFAULT_CATCH_UP_HOURS`). Unbounded, a
fixed-times job with no run state for today fires every slot the day already
passed, one per tick — three briefs a minute apart on a deploy at 14:30. The
grace window is what separates "we were down across the trigger" from "this
state is simply new".
"""

from __future__ import annotations

import asyncio
import calendar
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

from app.config import Settings
from app.db.repo import FirestoreRepo
from app.memory import db
from app.providers.registry import ProviderRegistry

log = structlog.get_logger(__name__)

#: How often the loop wakes to check whether any job is due.
CHECK_INTERVAL_SECONDS = 60

#: How long a job's Firestore-held config is reused before being re-read.
#: The trade is "an operator's schedule edit takes up to this long to take
#: effect" against "one Firestore read per job per minute, forever". Five
#: minutes is comfortably fast for a scheduling change and cuts the read
#: count by 5x.
CONFIG_CACHE_SECONDS = 300

#: How late a fixed-time slot may be and still fire.
#:
#: The self-healing path exists for "the process was down across 00:00 and
#: came back at 00:20". It is not meant for "it is 14:30, this deployment has
#: no run state, so let us run midnight, 06:00 and noon back to back" — which
#: is exactly what an empty ``last_fired`` produced: one brief per tick, a
#: minute apart, until the day was caught up. Empty run state is normal, not
#: exceptional. It happens on a first deploy, after a volume wipe, and on
#: every redeploy if the memory directory is not actually on a volume.
#:
#: Two hours is comfortably longer than any deploy or restart and far shorter
#: than the six between slots, so at most one slot is ever inside the window.
#: Overridable per job as ``catch_up_hours`` in its config document.
DEFAULT_CATCH_UP_HOURS = 2

#: A job receives the registry, repo and settings, plus ``slot`` — which of
#: its configured fixed-times hours this run is *for*. That is not always the
#: current hour: the scheduler deliberately self-heals a missed slot by firing
#: it late, so a job that needs to know "am I the midnight run" must be told,
#: never infer it from the wall clock. See ``_run_job``.
JobFn = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(slots=True)
class ScheduledJob:
    """One scheduled job. Set exactly one of ``default_interval_minutes``,
    ``default_hours``, ``default_weekday``, ``default_day_of_month``, or none
    of them for plain daily mode."""

    name: str
    settings_key: str
    run: JobFn
    default_hour: int = 2
    default_minute: int = 0
    default_timezone: str = "UTC"
    default_enabled: bool = True
    default_interval_minutes: int | None = None
    default_hours: list[int] | None = None
    #: 0 = Monday. Weekly jobs also honour ``default_hour``/``default_minute``.
    default_weekday: int | None = None
    #: 1-31, clamped to the month's actual length.
    default_day_of_month: int | None = None

    @property
    def mode(self) -> str:
        if self.default_interval_minutes is not None:
            return "interval"
        if self.default_hours is not None:
            return "fixed_times"
        if self.default_weekday is not None:
            return "weekly"
        if self.default_day_of_month is not None:
            return "monthly"
        return "daily"


class _RunState:
    """Per-job run bookkeeping, in local SQLite.

    Deliberately not Firestore. This is high-frequency, single-deployment,
    fully reconstructible state — the worst case for losing it is that a job
    runs once more than it strictly needed to after a volume wipe.
    """

    def __init__(self, key: str) -> None:
        self._key = f"scheduler:{key}"

    def load(self) -> dict[str, Any]:
        raw = db.kv_get(self._key)
        if not raw:
            return {}
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}

    def save(self, **updates: Any) -> dict[str, Any]:
        state = self.load()
        state.update(updates)
        db.kv_set(self._key, json.dumps(state, default=str))
        return state


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
        self._config_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        #: Jobs currently executing. The previous version relied on writing
        #: last_run_at before the body ran; that is still done, but an
        #: in-process guard is both cheaper and stricter — it makes a second
        #: concurrent run of the same job impossible rather than unlikely.
        self._in_flight: set[str] = set()

    async def run(self) -> None:
        log.info("scheduler_started", jobs=[(j.name, j.mode) for j in self._jobs])
        await self._ensure_defaults_visible()
        while not self._stopped:
            for job in self._jobs:
                # Fire-and-forget rather than awaited in turn: a single long
                # job used to block every other job from even being checked
                # until it finished — including the 10-minute one whose whole
                # purpose is frequency.
                asyncio.create_task(self._maybe_run_safe(job))
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    async def _maybe_run_safe(self, job: ScheduledJob) -> None:
        try:
            await self._maybe_run(job)
        except Exception:
            # A bug in one job's scheduling *check* must not take the others
            # down. The job body has its own guard in _run_job.
            log.error("scheduler_tick_failed", job=job.name, exc_info=True)

    def stop(self) -> None:
        self._stopped = True

    # -- config ---------------------------------------------------------------

    def _defaults(self, job: ScheduledJob) -> dict[str, Any]:
        base: dict[str, Any] = {"enabled": job.default_enabled, "mode": job.mode}
        if job.mode == "interval":
            base["interval_minutes"] = job.default_interval_minutes
            return base
        base.update(
            {"minute": job.default_minute, "timezone": job.default_timezone}
        )
        if job.mode == "fixed_times":
            base["hours"] = list(job.default_hours or [])
            base["catch_up_hours"] = DEFAULT_CATCH_UP_HOURS
        else:
            base["hour"] = job.default_hour
        if job.mode == "weekly":
            base["weekday"] = job.default_weekday
        if job.mode == "monthly":
            base["day_of_month"] = job.default_day_of_month
        return base

    async def _ensure_defaults_visible(self) -> None:
        """Write each job's default config the first time it is seen, so it
        appears as an editable row on the Settings page before it has ever
        run rather than only after.

        Also repairs a document whose ``mode`` no longer matches the job's.
        That mismatch means the code changed shape under a document written
        by an earlier deployment — the cycle moving from "every 360 minutes"
        to "at 00:00, 06:00, 12:00 and 18:00", say. It is not a correctness
        bug, because :meth:`_due` branches on ``job.mode`` from the code and
        an interval document carries no ``hours`` key to override with. It is
        worse than that in practice: the Settings page would keep showing
        ``interval_minutes: 360`` while the scheduler ran fixed times, so the
        one place an operator looks to confirm a schedule would show them
        something that is not what runs.

        ``enabled`` is carried across, because that is an operator's choice
        rather than a stale artefact of the old shape.
        """
        descriptions = {
            "interval": "Edit interval_minutes/enabled as JSON.",
            "fixed_times": "Edit hours (a list)/minute/timezone/catch_up_hours/enabled "
                            "as JSON. catch_up_hours is how late a missed slot may "
                            "still fire.",
            "daily": "Edit hour/minute/timezone/enabled as JSON.",
            "weekly": "Edit weekday (0=Monday)/hour/minute/timezone/enabled as JSON.",
            "monthly": "Edit day_of_month/hour/minute/timezone/enabled as JSON.",
        }
        for job in self._jobs:
            existing = await self._repo.get_setting(job.settings_key)
            if existing is not None:
                stored = existing.value if isinstance(existing.value, dict) else {}
                # Only a document that *states* a mode and states the wrong
                # one is stale. One with no mode at all was hand-written or
                # predates the field, and rewriting it would throw away an
                # operator's chosen hours to fix a cosmetic problem.
                if "mode" not in stored or stored["mode"] == job.mode:
                    continue
                defaults = self._defaults(job)
                if "enabled" in stored:
                    defaults["enabled"] = stored["enabled"]
                await self._repo.upsert_setting(
                    job.settings_key,
                    defaults,
                    description=(
                        f"Scheduled job '{job.name}' ({job.mode}). "
                        f"{descriptions[job.mode]} No redeploy needed. "
                        f"Run history is kept locally, not here."
                    ),
                    actor="scheduler",
                )
                log.info(
                    "scheduler_config_migrated",
                    job=job.name,
                    was=stored.get("mode"),
                    now=job.mode,
                )
                continue
            await self._repo.upsert_setting(
                job.settings_key,
                self._defaults(job),
                description=(
                    f"Scheduled job '{job.name}' ({job.mode}). "
                    f"{descriptions[job.mode]} No redeploy needed. "
                    f"Run history is kept locally, not here."
                ),
                actor="scheduler",
            )

    async def _config_for(self, job: ScheduledJob) -> dict[str, Any]:
        cached = self._config_cache.get(job.settings_key)
        loop_now = asyncio.get_running_loop().time()
        if cached and loop_now - cached[0] < CONFIG_CACHE_SECONDS:
            return cached[1]

        base = self._defaults(job)
        try:
            setting = await self._repo.get_setting(job.settings_key)
        except Exception:
            # A Firestore blip must not stop the scheduler; defaults are a
            # correct fallback and the next refresh picks up the real config.
            log.warning("scheduler_config_read_failed", job=job.name, exc_info=True)
            setting = None
        if setting and isinstance(setting.value, dict):
            base.update({k: v for k, v in setting.value.items() if k not in _RUN_STATE_KEYS})

        self._config_cache[job.settings_key] = (loop_now, base)
        return base

    # -- scheduling -----------------------------------------------------------

    async def _maybe_run(self, job: ScheduledJob) -> None:
        config = await self._config_for(job)
        if not config.get("enabled", True):
            return
        if job.name in self._in_flight:
            return

        state = _RunState(job.settings_key)
        current = state.load()

        if job.mode == "interval":
            last_run_at = current.get("last_run_at")
            if last_run_at:
                interval = timedelta(
                    minutes=config.get("interval_minutes") or job.default_interval_minutes or 10
                )
                if datetime.now(timezone.utc) - _parse_iso(last_run_at) < interval:
                    return
            await self._run_job(job, state)
            return

        tz = _safe_zone(config.get("timezone", "UTC"), job.name)
        now_local = datetime.now(tz)
        today = now_local.date().isoformat()

        if job.mode == "fixed_times":
            last_fired: dict[str, Any] = dict(current.get("last_fired") or {})
            grace = timedelta(
                hours=float(config.get("catch_up_hours", DEFAULT_CATCH_UP_HOURS))
            )
            stale: list[int] = []
            for hour in config.get("hours") or []:
                if last_fired.get(str(hour)) == today:
                    continue
                trigger = _trigger_at(now_local, hour, config.get("minute", 0))
                if now_local < trigger:
                    continue

                # Past its time. Recent enough to be a miss worth healing, or
                # old enough that running it now would be re-enacting a
                # moment rather than reporting on one?
                if now_local - trigger > grace:
                    stale.append(int(hour))
                    last_fired[str(hour)] = today
                    continue

                last_fired[str(hour)] = today
                if stale:
                    # Retired alongside a real run, so they ride its write.
                    log.info("scheduled_slots_skipped_as_stale", job=job.name, slots=stale)
                await self._run_job(
                    job, state, extra={"last_fired": last_fired}, slot=hour
                )
                return  # one slot per tick; the next tick picks up any other due slot

            if stale:
                # Nothing ran, so the retirement needs its own write —
                # otherwise every tick re-derives the same stale list and the
                # log fills up once a minute for the rest of the day.
                state.save(last_fired=last_fired)
                log.warning(
                    "scheduled_slots_skipped_as_stale",
                    job=job.name,
                    slots=stale,
                    reason=f"more than {grace} past their time — no run state for today, "
                           f"which is normal on a first deploy or after a volume wipe",
                )
            return

        if current.get("last_run_date") == today:
            return

        if job.mode == "weekly":
            if now_local.weekday() != int(config.get("weekday", job.default_weekday or 0)):
                return
        elif job.mode == "monthly":
            wanted = int(config.get("day_of_month", job.default_day_of_month or 1))
            # Clamp so a job set to the 31st still fires in a 30-day month,
            # on its last day, rather than silently never running.
            last_day = calendar.monthrange(now_local.year, now_local.month)[1]
            if now_local.day != min(wanted, last_day):
                return

        if now_local < _trigger_at(now_local, config.get("hour", 0), config.get("minute", 0)):
            return

        await self._run_job(job, state, extra={"last_run_date": today})

    async def _run_job(
        self,
        job: ScheduledJob,
        state: _RunState,
        *,
        extra: dict[str, Any] | None = None,
        slot: int | None = None,
    ) -> None:
        """Record the start, run, record the result — all locally.

        ``last_run_at`` is written *before* the body runs, not only after.
        That is load-bearing: a job whose body can run for minutes had its
        completion recorded only in a ``finally``, so a redeploy mid-run
        killed the task before it wrote anything, and the next process's
        first tick saw "hasn't run in ages" and fired again immediately —
        observed firing every 6-16 minutes instead of the configured 360,
        with several full cycles running concurrently. Writing at start means
        even a mid-run kill leaves an accurate ``last_run_at`` behind.
        """
        started = datetime.now(timezone.utc)
        state.save(last_run_at=started.isoformat(), last_slot=slot, **(extra or {}))
        self._in_flight.add(job.name)

        # A slot firing well after its hour is the self-healing path working,
        # not a fault — but it is worth seeing in the logs, because it is
        # also the condition under which a job that guessed its slot from the
        # wall clock would silently do the wrong work.
        late_by = None
        if slot is not None:
            tz = _safe_zone(
                (await self._config_for(job)).get("timezone", "UTC"), job.name
            )
            local = datetime.now(tz)
            late_by = (local.hour - slot) % 24
            if late_by:
                log.warning(
                    "scheduled_slot_fired_late",
                    job=job.name,
                    slot=slot,
                    local_hour=local.hour,
                    hours_late=late_by,
                )

        log.info("scheduled_job_starting", job=job.name, slot=slot)

        try:
            result = await job.run(self._registry, self._repo, self._settings, slot=slot)
            log.info("scheduled_job_complete", job=job.name, result=result)
        except Exception:
            log.error("scheduled_job_failed", job=job.name, exc_info=True)
            result = {"error": "job raised — see server logs for scheduled_job_failed"}
        finally:
            self._in_flight.discard(job.name)
            state.save(
                last_result=result,
                last_finished_at=datetime.now(timezone.utc).isoformat(),
                last_duration_seconds=round(
                    (datetime.now(timezone.utc) - started).total_seconds(), 1
                ),
            )

    # -- introspection --------------------------------------------------------

    def status(self) -> list[dict[str, Any]]:
        """What each job is doing, for the System Health page. All local."""
        rows = []
        for job in self._jobs:
            state = _RunState(job.settings_key).load()
            rows.append(
                {
                    "name": job.name,
                    "mode": job.mode,
                    "settings_key": job.settings_key,
                    "running": job.name in self._in_flight,
                    "last_run_at": state.get("last_run_at"),
                    "last_slot": state.get("last_slot"),
                    "last_finished_at": state.get("last_finished_at"),
                    "last_duration_seconds": state.get("last_duration_seconds"),
                    "last_result": state.get("last_result"),
                }
            )
        return rows


#: Keys that used to live in the Firestore setting document and now live in
#: SQLite. Filtered out when merging a stored config so an old document
#: written by a previous deployment cannot resurrect stale run state.
_RUN_STATE_KEYS = frozenset(
    {"last_run_at", "last_run_date", "last_fired", "last_result", "last_finished_at",
     "last_duration_seconds"}
)


def job_status(settings_key: str) -> dict[str, Any]:
    """Run state for one job, readable without a Scheduler instance."""
    return _RunState(settings_key).load()


def _trigger_at(now_local: datetime, hour: Any, minute: Any) -> datetime:
    return now_local.replace(
        hour=int(hour or 0), minute=int(minute or 0), second=0, microsecond=0
    )


def _parse_iso(value: Any) -> datetime:
    dt = datetime.fromisoformat(str(value))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _safe_zone(name: Any, job_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        log.warning("scheduler_bad_timezone", job=job_name, timezone=name)
        return ZoneInfo("UTC")
