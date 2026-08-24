"""app/scheduling/scheduler.py's timing/persistence logic.

Uses a fake in-memory repo (not real Firestore) so this suite runs fast and
without live credentials — the real end-to-end wiring (does a job actually
reach Firestore correctly) was verified by hand against the real project
this session; this file locks in the timing rules specifically: fires once
past the trigger time, never twice the same local day, self-heals after a
missed exact minute.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.scheduling.scheduler import ScheduledJob, Scheduler


class FakeSetting:
    def __init__(self, value):
        self.value = value


class FakeRepo:
    """Just enough of FirestoreRepo's interface for the scheduler."""

    def __init__(self):
        self._store: dict[str, object] = {}

    async def get_setting(self, key):
        if key not in self._store:
            return None
        return FakeSetting(self._store[key])

    async def upsert_setting(self, key, value, *, description=None, actor="operator"):
        self._store[key] = value
        return FakeSetting(value)


@pytest.fixture
def repo():
    return FakeRepo()


def _job(run, *, hour, minute, enabled=True, timezone_="UTC"):
    return ScheduledJob(
        name="test_job", settings_key="scheduler_test_job", run=run,
        default_hour=hour, default_minute=minute, default_timezone=timezone_,
        default_enabled=enabled,
    )


class TestTriggerTiming:
    async def test_fires_when_past_todays_trigger_time_and_not_yet_run(self, repo):
        calls = []

        async def job(registry, repo, settings):
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

        async def job(registry, repo, settings):
            calls.append(1)

        # A trigger two minutes from now, within the current hour, is
        # unambiguously "later today" regardless of what hour it is right
        # now — unlike `min(now.hour + 2, 23)`, which clamps to hour 23 and
        # spuriously looks "already past" whenever the suite runs within two
        # hours of UTC midnight.
        now = datetime.now(timezone.utc)
        future_minute = min(now.minute + 2, 59)
        scheduled = _job(job, hour=now.hour, minute=future_minute)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)
        assert len(calls) == 0

    async def test_never_fires_twice_in_the_same_local_day(self, repo):
        calls = []

        async def job(registry, repo, settings):
            calls.append(1)

        now = datetime.now(timezone.utc)
        scheduled = _job(job, hour=0, minute=0)  # always past trigger
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)
        await scheduler._maybe_run(scheduled)
        await scheduler._maybe_run(scheduled)
        assert len(calls) == 1

    async def test_disabled_job_never_fires(self, repo):
        calls = []

        async def job(registry, repo, settings):
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

        async def job(registry, repo, settings):
            calls.append(1)

        now = datetime.now(timezone.utc)
        long_past_hour = 0 if now.hour > 1 else now.hour
        scheduled = _job(job, hour=long_past_hour, minute=0)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)
        assert len(calls) == 1


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

        async def job(registry, repo, settings):
            calls.append(1)

        scheduled = _interval_job(job, minutes=15)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)
        assert len(calls) == 1

    async def test_does_not_fire_again_before_interval_elapses(self, repo):
        calls = []

        async def job(registry, repo, settings):
            calls.append(1)

        scheduled = _interval_job(job, minutes=15)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)
        await scheduler._maybe_run(scheduled)
        await scheduler._maybe_run(scheduled)
        assert len(calls) == 1

    async def test_fires_again_once_interval_has_elapsed(self, repo):
        calls = []

        async def job(registry, repo, settings):
            calls.append(1)

        scheduled = _interval_job(job, minutes=15)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await repo.upsert_setting(
            "scheduler_test_interval_job",
            {
                "enabled": True,
                "interval_minutes": 15,
                "last_run_at": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
            },
        )
        await scheduler._maybe_run(scheduled)
        assert len(calls) == 1

    async def test_disabled_interval_job_never_fires(self, repo):
        calls = []

        async def job(registry, repo, settings):
            calls.append(1)

        scheduled = _interval_job(job, minutes=15, enabled=False)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)
        assert len(calls) == 0

    async def test_ensure_defaults_visible_writes_interval_shape(self, repo):
        async def job(registry, repo, settings):
            pass

        scheduled = _interval_job(job, minutes=10)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._ensure_defaults_visible()

        setting = await repo.get_setting("scheduler_test_interval_job")
        assert setting.value["interval_minutes"] == 10
        assert "hour" not in setting.value


class TestConfigPersistence:
    async def test_run_records_last_run_date_and_result(self, repo):
        async def job(registry, repo, settings):
            return {"evaluated": 3}

        scheduled = _job(job, hour=0, minute=0)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])
        await scheduler._maybe_run(scheduled)

        setting = await repo.get_setting("scheduler_test_job")
        assert setting.value["last_result"] == {"evaluated": 3}
        assert setting.value["last_run_date"] == datetime.now(timezone.utc).date().isoformat()

    async def test_a_raising_job_is_recorded_as_failed_not_crashed(self, repo):
        async def job(registry, repo, settings):
            raise RuntimeError("boom")

        scheduled = _job(job, hour=0, minute=0)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await scheduler._maybe_run(scheduled)  # must not raise

        setting = await repo.get_setting("scheduler_test_job")
        assert "error" in setting.value["last_result"]
        # still marked as run today, so a failing job doesn't retry-loop all day
        assert setting.value["last_run_date"] == datetime.now(timezone.utc).date().isoformat()

    async def test_ensure_defaults_visible_writes_config_before_first_run(self, repo):
        async def job(registry, repo, settings):
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
        async def job(registry, repo, settings):
            pass

        scheduled = _job(job, hour=5, minute=30)
        scheduler = Scheduler(registry=None, repo=repo, settings=None, jobs=[scheduled])

        await repo.upsert_setting("scheduler_test_job", {"enabled": False, "hour": 9, "minute": 0, "timezone": "UTC"})
        await scheduler._ensure_defaults_visible()

        setting = await repo.get_setting("scheduler_test_job")
        assert setting.value["hour"] == 9  # operator's existing config preserved, not clobbered
        assert setting.value["enabled"] is False
