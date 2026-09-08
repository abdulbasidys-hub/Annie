"""A full thinking cycle, with the model stubbed.

The cycle is the one path where everything meets: ledger, signals, digest,
the single paid call, memory writes, dossier refresh, the watchlist, and the
Firestore snapshot. Each piece has its own tests; this checks they compose —
and specifically that a failure in any one stage does not take the rest down,
because the alternative is a cycle that silently stops learning.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.memory import bootstrap, ledger, service, signals


class _Message:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Message(content)


class _Usage:
    prompt_tokens = 1700
    completion_tokens = 640


class _Response:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = _Usage()


class FakeCompletions:
    """Records what it was asked, so the prompt itself can be asserted on."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Response(json.dumps(self.payload))


class FakeClient:
    def __init__(self, payload):
        self.chat = type("Chat", (), {"completions": FakeCompletions(payload)})()


class FakeReasoner:
    def __init__(self, payload):
        self.client = FakeClient(payload)

    async def raw_client(self):
        return self.client


class FakeRegistry:
    def __init__(self, payload):
        self.reasoning = FakeReasoner(payload)


class FakeRepo:
    """Only the small operator-facing collections the cycle still touches."""

    def __init__(self):
        self.writes = 0

    async def get_discord_channel_by_purpose(self, purpose, **kwargs):
        return None


EDITS = {
    "headline": "Cat-adjacent names cleared tiers at roughly 3x baseline, third week running.",
    "edits": [
        {
            "op": "rewrite",
            "path": "core/whats-working.md",
            "title": "What's working right now",
            "text": "Cat-adjacent names, specifically the oddly-specific ones, are "
                    "outperforming. Generic cats are at baseline, which suggests the "
                    "specificity is the edge rather than the theme.",
            "tags": ["core", "cat"],
            "keys": [],
            "importance": 0.9,
        },
        {
            "op": "append",
            "path": "notes/cat-specificity.md",
            "title": "Cat specificity",
            "text": "Third week. Worth promoting to the market model if it holds once more.",
            "tags": ["cat"],
            "keys": ["cat"],
            "importance": 0.6,
        },
        {
            "op": "rewrite",
            "path": "notes/not-allowed.md",   # rewrite outside core/ is refused
            "title": "Nope",
            "text": "This should be rejected.",
            "tags": [],
            "keys": [],
            "importance": 0.5,
        },
        {
            "op": "delete",
            "path": "core/market-model.md",   # deleting core/ is refused
            "title": "",
            "text": "",
            "tags": [],
            "keys": [],
            "importance": 0.5,
        },
        {
            "op": "append",
            "path": "../../escape.md",        # path traversal is refused
            "title": "Escape",
            "text": "Should never be written.",
            "tags": [],
            "keys": [],
            "importance": 0.5,
        },
    ],
    "watch": {"creators": ["Wa11etAAA"], "narratives": ["cat-adjacent"]},
}


@pytest.fixture
def seeded(isolated_memory):
    bootstrap._seed_files()
    for i in range(25):
        ledger.record_launch(
            mint=f"Mint{i:040d}", creator=f"W{i % 3}", launchpad="pumpfun",
            symbol="CAT", name="Quantum Cat",
        )
        ledger.record_price(
            mint=f"Mint{i:040d}", market_cap=320_000, liquidity=60_000, tier=250_000
        )
    signals.recompute()


class TestLearningStep:
    async def test_it_applies_good_edits_and_refuses_bad_ones(self, seeded):
        from app.config import get_settings
        from app.memory.learn import learn_from_window

        registry = FakeRegistry(EDITS)
        result = await learn_from_window(registry, get_settings(), window_hours=24)

        applied = {e["path"] for e in result.applied}
        rejected = {e["path"]: e["reason"] for e in result.rejected}

        assert applied == {"core/whats-working.md", "notes/cat-specificity.md"}
        assert "notes/not-allowed.md" in rejected
        assert "rewrite is only allowed" in rejected["notes/not-allowed.md"]
        assert "core/market-model.md" in rejected
        assert "not allowed" in rejected["core/market-model.md"]
        # The traversal never becomes a path at all, so it is reported raw.
        assert any("escape" in p for p in rejected)

        # …and nothing landed outside the memory root.
        assert service.read("core/whats-working.md").body.startswith("Cat-adjacent")
        assert service.read("core/market-model.md") is not None, "core file was deleted"

    async def test_the_prompt_it_sends_is_small(self, seeded):
        from app.config import get_settings
        from app.memory.learn import learn_from_window

        registry = FakeRegistry(EDITS)
        await learn_from_window(registry, get_settings(), window_hours=24)

        call = registry.reasoning.client.chat.completions.calls[0]
        prompt = "".join(m["content"] for m in call["messages"])
        assert len(prompt) // 4 < 4000, f"cycle prompt is ~{len(prompt) // 4} tokens"
        assert call["response_format"]["type"] == "json_schema"

    async def test_a_quiet_window_costs_nothing(self, isolated_memory):
        from app.config import get_settings
        from app.memory.learn import learn_from_window

        registry = FakeRegistry(EDITS)
        result = await learn_from_window(registry, get_settings(), window_hours=6)

        assert result.skipped == "quiet window"
        assert registry.reasoning.client.chat.completions.calls == []

    async def test_a_model_failure_does_not_lose_the_cycle(self, seeded):
        from app.config import get_settings
        from app.memory.learn import learn_from_window

        registry = FakeRegistry(EDITS)

        async def boom(**kwargs):
            raise RuntimeError("upstream 503")

        registry.reasoning.client.chat.completions.create = boom
        result = await learn_from_window(registry, get_settings(), window_hours=24)

        assert result.skipped and "upstream 503" in result.skipped
        assert result.applied == []


class TestFullCycle:
    async def test_the_whole_cycle_runs_and_writes_memory(self, seeded):
        from app.config import get_settings
        from app.scheduling.jobs import _cycle

        registry = FakeRegistry(EDITS)
        result = await _cycle(registry, FakeRepo(), get_settings())

        assert "error" not in result.get("signals", {})
        assert result["learning"]["applied"], "the cycle applied no memory edits"
        assert result["watchlist_updated"] is True
        # Firestore is not configured in tests, so the snapshot reports itself
        # skipped rather than failing — memory on disk is already durable.
        assert result["snapshot"]["written"] == 0

        watchlist = service.read("core/watchlist.md")
        assert watchlist is not None
        assert "cat-adjacent" in watchlist.body, "the model's watch list was not applied"

    async def test_one_broken_stage_does_not_abort_the_others(self, seeded, monkeypatch):
        """Signals failing must not stop learning; a snapshot failing at the
        end must not make a cycle look failed when the memory on disk is
        already correct."""
        from app.config import get_settings
        from app.memory import signals as signals_module
        from app.scheduling.jobs import _cycle

        def explode(*args, **kwargs):
            raise RuntimeError("signal engine blew up")

        monkeypatch.setattr(signals_module, "recompute", explode)

        registry = FakeRegistry(EDITS)
        result = await _cycle(registry, FakeRepo(), get_settings())

        assert "error" in result["signals"]
        assert result["learning"]["applied"], "learning was skipped because signals failed"

    async def test_the_day_boundary_writes_the_deterministic_log(self, seeded, monkeypatch):
        from app.memory.rollup import write_daily_log

        # 23:00 UTC is when the job actually fires — 00:00 in Africa/Lagos,
        # the configured day boundary. Passed explicitly rather than using
        # the wall clock, because `write_daily_log` deliberately summarises
        # *yesterday* when run in the morning (someone triggering it by hand
        # after midnight means the day that just ended), and a test that
        # passes or fails depending on the hour it runs at is worthless.
        now = datetime.now(timezone.utc).replace(hour=23, minute=0, second=0, microsecond=0)
        result = await write_daily_log(now=now)

        assert result["qualified"] == 25
        log = service.read(result["path"])
        assert log is not None
        # Every qualified token's CA and creator must be in the log verbatim —
        # this is the deterministic half, and it must not depend on the model.
        assert f"Mint{0:040d}" in log.body
        assert "W0" in log.body


class TestTheDayBoundary:
    """What actually broke in production on 2026-09-09.

    The cycle decided "am I the midnight run" by reading the wall clock. The
    scheduler self-heals a missed slot by firing it late, so a 00:00 slot
    starting at 01:30 after a redeploy saw hour 1 — and silently skipped the
    daily log, the launch ideas and the full-day brief for the whole day.
    Nothing errored. The brief simply never arrived.
    """

    async def test_the_midnight_slot_does_the_daily_work_however_late_it_fires(
        self, seeded
    ):
        from app.config import get_settings
        from app.scheduling.jobs import _cycle

        # slot=0 is the midnight run. The wall clock is whatever it is —
        # that is the point.
        result = await _cycle(FakeRegistry(EDITS), FakeRepo(), get_settings(), slot=0)

        assert result["day_boundary"] is True
        assert result["slot"] == 0
        assert "daily_log" in result, "the daily log was skipped on the midnight slot"
        assert "ideas" in result, "launch ideas were skipped on the midnight slot"

    async def test_a_six_hourly_slot_does_not_do_the_daily_work(self, seeded):
        from app.config import get_settings
        from app.scheduling.jobs import _cycle

        result = await _cycle(FakeRegistry(EDITS), FakeRepo(), get_settings(), slot=12)

        assert result["day_boundary"] is False
        assert "daily_log" not in result
        assert "ideas" not in result

    async def test_the_daily_log_is_written_even_with_nothing_qualified(
        self, isolated_memory
    ):
        """A quiet day still gets a log. It is a deterministic record of what
        happened, and "nothing qualified" is a fact worth recording — an
        absent file is indistinguishable from a failed job."""
        from app.config import get_settings
        from app.memory import bootstrap, service
        from app.scheduling.jobs import _cycle

        bootstrap._seed_files()
        result = await _cycle(FakeRegistry(EDITS), FakeRepo(), get_settings(), slot=0)

        assert "daily_log" in result
        written = service.read(result["daily_log"]["path"])
        assert written is not None
        assert "Qualified today" in written.body

    async def test_a_missing_brief_channel_is_reported_not_swallowed(self, seeded):
        """The other half of "no brief arrived": everything ran and there was
        nowhere to send it. That has to be legible without reading logs."""
        from app.config import get_settings
        from app.scheduling.jobs import _cycle

        result = await _cycle(FakeRegistry(EDITS), FakeRepo(), get_settings(), slot=0)

        assert result["delivered"] is False
        assert result["reason"], "no reason given for an undelivered brief"

    async def test_the_diagnosis_surfaces_an_undelivered_brief(self, seeded):
        from app.memory import db, health

        db.kv_set(
            "scheduler:scheduler_cycle",
            json.dumps(
                {
                    "last_run_at": datetime.now(timezone.utc).isoformat(),
                    "last_slot": 0,
                    "last_result": {
                        "delivered": False,
                        "reason": "no Discord channel is set up with purpose "
                                  "'morning_brief' — ask Annie in Discord to create one",
                    },
                }
            ),
        )

        delivery = health.diagnose()["delivery"]

        assert delivery["status"] == "undelivered"
        assert "morning_brief" in delivery["detail"]
        assert "Everything else ran" in delivery["fix"]
