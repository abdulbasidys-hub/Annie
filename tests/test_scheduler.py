"""app/scheduling/scheduler.py's timing/persistence logic.

Uses a fake in-memory repo (not real Firestore) so this suite runs fast and
without live credentials — the real end-to-end wiring (does a job actually
reach Firestore correctly) was verified by hand against the real project
this session; this file locks in the timing rules specifically: fires once
past the trigger time, never twice the same local day, self-heals after a
missed exact minute.

Since the 2026-09-08 rewrite, *config* (enabled/hour/interval) lives in the
repo and *run state* (last_run_at/last_fired/last_result) lives in local
SQLite — see the scheduler module docstring for the cost arithmetic behind
that split. So these tests read config through the fake repo and run state
through ``job_status``. The ``isolated_memory`` fixture in conftest.py gives
each test its own SQLite file, so run state never leaks between them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.scheduling.scheduler import ScheduledJob, Scheduler, job_status


class FakeSetting:
    def __init__(self, value):
        self.value = value


class FakeRepo:
    """Just enough of FirestoreRepo's interface for the scheduler.

    Counts writes so a test can assert the scheduler is not writing to the
    repo on every run — that count is the whole point of moving run state
    into SQLite.
    """

    def __init__(self):
        self._store: dict[str, object] = {}
        self.writes = 0

    async def get_setting(self, key):
        if key not in self._store:
            return None
        return FakeSetting(self._store[key])

    async def upsert_setting(self, key, value, *, description=None, actor="operator", audit=True):
        self._store[key] = value
        self.writes += 1
        return FakeSetting(value)


def _seed_state(settings_key: str, **state) -> None:
    """Pre-load a job's SQLite run state, standing in for an earlier run."""
    import json

    from app.memory import db

    db.kv_set(f"scheduler:{settings_key}", json.dumps(state))


@pytest.fixture
def repo():
    return FakeRepo()


def _later_today(now: datetime) -> tuple[int, int]:
    """An (hour, minute) that is unambiguously still ahead of ``now`` locally.

    Getting this right is fiddlier than it looks and both obvious versions
    are wrong. `min(now.hour + 2, 23)` clamps to hour 23 and reads as
    "already past" whenever the suite runs near midnight;
    `min(now.minute + 2, 59)` clamps to the *current* minute whenever it runs
    at :58 or :59, so the trigger is now rather than later. Stepping to the
    top of the next hour has neither failure mode, and the one degenerate
    case — the final minute of the day, where nothing later exists — is
    skipped explicitly rather than flaking once a day.
    """
    if now.hour >= 23 and now.minute >= 58:
        pytest.skip("no later trigger time exists within today at this instant")
    if now.hour < 23:
        return now.hour + 1, 0
    return 23, 59


def _job(run, *, hour, minute, enabled=True, timezone_="UTC"):
    return ScheduledJob(
        name="test_job", settings_key="scheduler_test_job", run=run,
        default_hour=hour, default_minute=minute, default_timezone=timezone_,
        default_enabled=enabled,
    )


class TestTriggerTiming:
    async def test_fires_when_past_todays_trigger_time_and_not_yet_run(self, repo):
        calls = []

        async def job(registry, repo, settings, *, slot=None):
            calls.append(1)
            return {"ok": True}

        now = datetime.now(timezone.utc)
        past_hour = (now - timedelta(hours=1)).hour if now.hour > 0 else 0
        scheduled = _job(job, hour=past_hour, minute=0)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)
        assert len(calls) == 1

    async def test_does_not_fire_before_todays_trigger_time(self, repo):
        calls = []

        async def job(registry, repo, settings, *, slot=None):
            calls.append(1)

        now = datetime.now(timezone.utc)
        hour, minute = _later_today(now)
        scheduled = _job(job, hour=hour, minute=minute)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)
        assert len(calls) == 0

    async def test_never_fires_twice_in_the_same_local_day(self, repo):
        calls = []

        async def job(registry, repo, settings, *, slot=None):
            calls.append(1)

        scheduled = _job(job, hour=0, minute=0)  # always past trigger
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)
        await scheduler._maybe_run(scheduled)
        await scheduler._maybe_run(scheduled)
        assert len(calls) == 1

    async def test_disabled_job_never_fires(self, repo):
        calls = []

        async def job(registry, repo, settings, *, slot=None):
            calls.append(1)

        scheduled = _job(job, hour=0, minute=0, enabled=False)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)
        assert len(calls) == 0

    async def test_self_heals_a_missed_exact_minute(self, repo):
        # Simulates the process being down at the exact trigger minute and
        # coming back up later the same day — should still fire, not skip
        # the whole day waiting for a minute that already passed.
        calls = []

        async def job(registry, repo, settings, *, slot=None):
            calls.append(1)

        now = datetime.now(timezone.utc)
        long_past_hour = 0 if now.hour > 1 else now.hour
        scheduled = _job(job, hour=long_past_hour, minute=0)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)
        assert len(calls) == 1


def _fixed_times_job(run, *, hours, minute=0, enabled=True, timezone_="UTC"):
    return ScheduledJob(
        name="test_fixed_job", settings_key="scheduler_test_fixed_job", run=run,
        default_hours=hours, default_minute=minute, default_timezone=timezone_,
        default_enabled=enabled,
    )


class TestFixedTimesTiming:
    """Clock-anchored multi-slot mode (§ 2026-08-25 — "12am Nigerian time,
    then every 6 hours from there, fixed not flexible"): interval-mode
    drifts with whenever the process last restarted and can never land on
    a chosen wall-clock time; this locks in that fixed-times actually does."""

    async def test_fires_for_the_current_hour_slot(self, repo):
        calls = []

        async def job(registry, repo, settings, *, slot=None):
            calls.append(1)

        now = datetime.now(timezone.utc)
        scheduled = _fixed_times_job(job, hours=[now.hour])
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)
        assert len(calls) == 1

    async def test_does_not_fire_twice_for_the_same_slot_same_day(self, repo):
        calls = []

        async def job(registry, repo, settings, *, slot=None):
            calls.append(1)

        now = datetime.now(timezone.utc)
        scheduled = _fixed_times_job(job, hours=[now.hour])
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)
        await scheduler._maybe_run(scheduled)
        await scheduler._maybe_run(scheduled)
        assert len(calls) == 1

    async def test_does_not_fire_before_the_slots_trigger_minute(self, repo):
        calls = []

        async def job(registry, repo, settings, *, slot=None):
            calls.append(1)

        now = datetime.now(timezone.utc)
        hour, minute = _later_today(now)
        scheduled = _fixed_times_job(job, hours=[hour], minute=minute)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)
        assert len(calls) == 0

    async def test_fires_independently_for_a_different_slot_same_day(self, repo):
        """Two slots due the same day both fire — a whole-day last_run_date
        model (daily mode) would wrongly treat one firing as covering the
        entire day; fixed-times tracks per-slot via last_fired."""
        calls = []

        async def job(registry, repo, settings, *, slot=None):
            calls.append(1)

        now = datetime.now(timezone.utc)
        other_hour = (now.hour - 1) % 24
        if other_hour == now.hour:
            pytest.skip("degenerate at this run time")
        scheduled = _fixed_times_job(job, hours=[other_hour, now.hour])
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])
        _seed_state(
            "scheduler_test_fixed_job",
            last_fired={str(other_hour): now.date().isoformat()},
        )

        await scheduler._maybe_run(scheduled)
        assert len(calls) == 1

    async def test_disabled_fixed_times_job_never_fires(self, repo):
        calls = []

        async def job(registry, repo, settings, *, slot=None):
            calls.append(1)

        now = datetime.now(timezone.utc)
        scheduled = _fixed_times_job(job, hours=[now.hour], enabled=False)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)
        assert len(calls) == 0

    async def test_ensure_defaults_visible_writes_fixed_times_shape(self, repo):
        async def job(registry, repo, settings, *, slot=None):
            pass

        scheduled = _fixed_times_job(job, hours=[0, 6, 12, 18], timezone_="Africa/Lagos")
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._ensure_defaults_visible()

        setting = await repo.get_setting("scheduler_test_fixed_job")
        assert setting.value["hours"] == [0, 6, 12, 18]
        assert setting.value["timezone"] == "Africa/Lagos"
        # Run state is no longer written into the config document — the
        # Settings page shows what the operator can edit, nothing else.
        assert "last_fired" not in setting.value


def _interval_job(run, *, minutes, enabled=True):
    return ScheduledJob(
        name="test_interval_job", settings_key="scheduler_test_interval_job", run=run,
        default_interval_minutes=minutes, default_enabled=enabled,
    )


class TestIntervalTiming:
    """The frequent-qualification job's timing rules (§ 2026-08-25 fix) — a
    once-a-day cadence is what let 16,602 of 16,774 discovered tokens go
    unevaluated in production, so this locks in that an interval job fires
    immediately on first tick and then respects its own interval."""

    async def test_fires_immediately_when_never_run(self, repo):
        calls = []

        async def job(registry, repo, settings, *, slot=None):
            calls.append(1)

        scheduled = _interval_job(job, minutes=15)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)
        assert len(calls) == 1

    async def test_does_not_fire_again_before_interval_elapses(self, repo):
        calls = []

        async def job(registry, repo, settings, *, slot=None):
            calls.append(1)

        scheduled = _interval_job(job, minutes=15)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)
        await scheduler._maybe_run(scheduled)
        await scheduler._maybe_run(scheduled)
        assert len(calls) == 1

    async def test_fires_again_once_interval_has_elapsed(self, repo):
        calls = []

        async def job(registry, repo, settings, *, slot=None):
            calls.append(1)

        scheduled = _interval_job(job, minutes=15)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        _seed_state(
            "scheduler_test_interval_job",
            last_run_at=(datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
        )
        await scheduler._maybe_run(scheduled)
        assert len(calls) == 1

    async def test_disabled_interval_job_never_fires(self, repo):
        calls = []

        async def job(registry, repo, settings, *, slot=None):
            calls.append(1)

        scheduled = _interval_job(job, minutes=15, enabled=False)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)
        assert len(calls) == 0

    async def test_ensure_defaults_visible_writes_interval_shape(self, repo):
        async def job(registry, repo, settings, *, slot=None):
            pass

        scheduled = _interval_job(job, minutes=10)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._ensure_defaults_visible()

        setting = await repo.get_setting("scheduler_test_interval_job")
        assert setting.value["interval_minutes"] == 10
        assert "hour" not in setting.value


class TestRunState:
    """Run bookkeeping now lives in SQLite, not in the Firestore setting."""

    async def test_run_records_last_run_date_and_result(self, repo):
        async def job(registry, repo, settings, *, slot=None):
            return {"evaluated": 3}

        scheduled = _job(job, hour=0, minute=0)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])
        await scheduler._maybe_run(scheduled)

        state = job_status("scheduler_test_job")
        assert state["last_result"] == {"evaluated": 3}
        assert state["last_run_date"] == datetime.now(timezone.utc).date().isoformat()

    async def test_a_raising_job_is_recorded_as_failed_not_crashed(self, repo):
        async def job(registry, repo, settings, *, slot=None):
            raise RuntimeError("boom")

        scheduled = _job(job, hour=0, minute=0)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)  # must not raise

        state = job_status("scheduler_test_job")
        assert "error" in state["last_result"]
        # still marked as run today, so a failing job doesn't retry-loop all day
        assert state["last_run_date"] == datetime.now(timezone.utc).date().isoformat()

    async def test_a_job_run_does_not_write_to_the_repo(self, repo):
        """The cost fix, asserted directly.

        A 10-minute job used to rewrite its whole Firestore setting document
        twice per run — ~288 writes/day of pure bookkeeping against a
        20,000/day plan cap. Nothing about a run may touch the repo now.
        """

        async def job(registry, repo, settings, *, slot=None):
            return {"ok": True}

        scheduled = _interval_job(job, minutes=10)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)

        assert repo.writes == 0, "a job run wrote to the repo"
        assert job_status("scheduler_test_interval_job")["last_result"] == {"ok": True}

    async def test_stale_run_state_in_an_old_config_document_is_ignored(self, repo):
        """A document written by a pre-rewrite deployment must not resurrect.

        Without the _RUN_STATE_KEYS filter, an old last_run_at still sitting
        in Firestore would merge back into config and could suppress the job
        indefinitely.
        """

        async def job(registry, repo, settings, *, slot=None):
            return {"ok": True}

        scheduled = _interval_job(job, minutes=15)
        await repo.upsert_setting(
            "scheduler_test_interval_job",
            {
                "enabled": True,
                "interval_minutes": 15,
                "last_run_at": datetime.now(timezone.utc).isoformat(),  # old shape
            },
        )
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])
        config = await scheduler._config_for(scheduled)

        assert "last_run_at" not in config

    async def test_ensure_defaults_visible_writes_config_before_first_run(self, repo):
        async def job(registry, repo, settings, *, slot=None):
            pass

        scheduled = _job(job, hour=5, minute=30, timezone_="America/New_York")
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        assert await repo.get_setting("scheduler_test_job") is None
        await scheduler._ensure_defaults_visible()

        setting = await repo.get_setting("scheduler_test_job")
        assert setting.value["hour"] == 5
        assert setting.value["minute"] == 30
        assert setting.value["timezone"] == "America/New_York"

    async def test_ensure_defaults_visible_does_not_overwrite_existing_config(self, repo):
        async def job(registry, repo, settings, *, slot=None):
            pass

        scheduled = _job(job, hour=5, minute=30)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await repo.upsert_setting("scheduler_test_job", {"enabled": False, "hour": 9, "minute": 0, "timezone": "UTC"})
        await scheduler._ensure_defaults_visible()

        setting = await repo.get_setting("scheduler_test_job")
        assert setting.value["hour"] == 9  # operator's existing config preserved, not clobbered
        assert setting.value["enabled"] is False


class TestSlotIsToldNotGuessed:
    """The bug that lost a day's brief in production (2026-09-09).

    The scheduler deliberately self-heals a missed slot by firing it late.
    A job that then works out "am I the midnight run" from the wall clock
    gets the wrong answer — a 00:00 slot firing at 01:30 after a redeploy
    sees hour 1, and silently skips the daily log, the launch ideas and the
    full-day brief for that entire day.
    """

    async def test_the_fired_slot_is_passed_to_the_job(self, repo):
        seen = []

        async def job(registry, repo, settings, *, slot=None):
            seen.append(slot)

        now = datetime.now(timezone.utc)
        scheduled = _fixed_times_job(job, hours=[now.hour])
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)

        assert seen == [now.hour], "the job was not told which slot it was running for"

    async def test_a_late_slot_reports_the_slot_not_the_current_hour(self, repo):
        """The heart of it. An earlier slot firing now must identify as that
        slot, never as whatever hour it happens to be."""
        seen = []

        async def job(registry, repo, settings, *, slot=None):
            seen.append(slot)

        now = datetime.now(timezone.utc)
        earlier = (now.hour - 2) % 24
        if earlier >= now.hour:
            pytest.skip("degenerate near midnight — no earlier slot exists today")

        scheduled = _fixed_times_job(job, hours=[earlier])
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)

        assert seen == [earlier]
        assert seen[0] != now.hour, "a late slot reported itself as the current hour"

    async def test_the_slot_is_recorded_for_inspection(self, repo):
        async def job(registry, repo, settings, *, slot=None):
            return {"ok": True}

        now = datetime.now(timezone.utc)
        scheduled = _fixed_times_job(job, hours=[now.hour])
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)

        assert job_status("scheduler_test_fixed_job")["last_slot"] == now.hour

    async def test_non_slot_modes_pass_none(self, repo):
        seen = []

        async def job(registry, repo, settings, *, slot=None):
            seen.append(slot)

        scheduled = _job(job, hour=0, minute=0)  # daily mode — no slot concept
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)

        assert seen == [None]
