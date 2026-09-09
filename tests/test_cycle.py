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


class FakeChannel:
    def __init__(self, channel_id):
        self.channel_id = channel_id


class FakeRepo:
    """Only the small operator-facing collections the cycle still touches."""

    def __init__(self, channels: dict | None = None):
        self.writes = 0
        self.channels = channels or {}

    async def get_discord_channel_by_purpose(self, purpose, **kwargs):
        found = self.channels.get(purpose)
        return FakeChannel(found) if found else None


def _capture(monkeypatch) -> list[tuple[str, str]]:
    """Record what Discord would have received, per channel."""
    sent: list[tuple[str, str]] = []

    async def fake_send(bot_token, channel_id, text):
        sent.append((channel_id, text))
        return True

    import app.bots.discord_bot as bot

    monkeypatch.setattr(bot, "send_channel_message", fake_send)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "bot-token")
    from app.config import get_settings

    get_settings.cache_clear()
    return sent


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


#: One payload, both readers. The day boundary makes two model calls through
#: the same fake client — the learning step, which reads `headline`/`edits`,
#: and the ideas step, which reads `ideas`. A payload carrying only the first
#: set makes the ideas step return "generated 0", which silently turns every
#: delivery assertion below into a test of nothing.
EDITS_AND_IDEAS = {
    **EDITS,
    "read_of_the_market": "Cat-adjacent still running; AI agents saturated.",
    "ideas": [
        {
            "name": "Cat Lawyer",
            "ticker": "LAWCAT",
            "description": "objection your honour my bags are down bad",
            "image": "A tabby in an ill-fitting suit behind a courtroom bench, "
                     "flat vector, muted palette.",
            "angle": "A cat in a courtroom filing motions for bag-holders.",
            "why_now": "Third week of cat-adjacent, specific variants at ~3x generic.",
            "evidence": "11 of 14 modified animal names cleared this week.",
            "grounding": "observed",
            "risk": "Week three is usually where a theme saturates.",
        }
    ],
    "avoid": ["AI (token)"],
}


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



class TestTheCycleStillIngests:
    """The regression this class exists for.

    The old cycle ran discovery as its first stage every six hours. The
    memory rewrite replaced that job wholesale and did not carry the stage
    across, so the webhook became the only way anything could enter the
    ledger — with no schedule, no fallback and no test noticing.

    The consequence was not subtle. One misconfigured webhook (an empty
    `accountAddresses`, which matches no transactions and so never fires)
    meant nothing arrived at all, and every downstream stage correctly
    reported zero: no qualifiers, no signals, no ideas, a brief full of
    zeros. Everything worked and nothing happened.

    Polling cannot be primary coverage — a few hundred signatures covers
    seconds at Pump.fun's volume — but it is the difference between a thin
    trickle and total silence, and between a visible fault and an invisible
    one.
    """

    async def test_the_cycle_attempts_ingest(self, seeded, monkeypatch):
        called = {}

        async def fake_discovery(registry, repo, *, hours=24):
            called["hours"] = hours
            return {"launches_seen": 3, "tokens_created": 2}

        import app.pipeline.tracking as tracking

        monkeypatch.setattr(tracking, "run_discovery_stage", fake_discovery)

        from app.config import get_settings
        from app.scheduling.jobs import _cycle

        result = await _cycle(FakeRegistry(EDITS), FakeRepo(), get_settings(), slot=12)

        assert called, "the cycle computed over the ledger without ever filling it"
        assert result["discovery"]["tokens_created"] == 2

    async def test_ingest_runs_before_the_work_that_reads_it(self, seeded, monkeypatch):
        """Order matters: signals, learning and the daily log all compute
        over the ledger, so a sweep after them lands a cycle late."""
        order = []

        async def fake_discovery(registry, repo, *, hours=24):
            order.append("discovery")
            return {}

        import app.memory.signals as signals_mod
        import app.pipeline.tracking as tracking

        real_recompute = signals_mod.recompute

        def traced(*args, **kwargs):
            order.append("signals")
            return real_recompute(*args, **kwargs)

        monkeypatch.setattr(tracking, "run_discovery_stage", fake_discovery)
        monkeypatch.setattr(signals_mod, "recompute", traced)

        from app.config import get_settings
        from app.scheduling.jobs import _cycle

        await _cycle(FakeRegistry(EDITS), FakeRepo(), get_settings(), slot=12)

        assert order[:2] == ["discovery", "signals"]

    async def test_a_failing_sweep_does_not_abort_the_cycle(self, seeded, monkeypatch):
        """Helius being down must not cost the thinking. There is already a
        ledger to reason over."""
        async def boom(registry, repo, *, hours=24):
            raise RuntimeError("helius unreachable")

        import app.pipeline.tracking as tracking

        monkeypatch.setattr(tracking, "run_discovery_stage", boom)

        from app.config import get_settings
        from app.scheduling.jobs import _cycle

        result = await _cycle(FakeRegistry(EDITS), FakeRepo(), get_settings(), slot=12)

        assert "error" in result["discovery"]
        assert result["signals"]["cohorts"] >= 0, "the cycle stopped at the failed stage"
        assert "learning" in result


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

    async def test_the_heading_carries_no_byline_and_no_timestamp(
        self, seeded, monkeypatch
    ):
        """It read `**Annie — Daily brief, 09 Sep 00:03 UTC**`.

        She is the only thing posting in the channel, so the name is noise on
        every message. And Discord stamps every message itself, so restating
        the time only ever added a second clock — one that disagrees with the
        first whenever a slot self-heals and fires late.
        """
        from app.config import get_settings
        from app.scheduling.jobs import _cycle

        sent = _capture(monkeypatch)
        await _cycle(FakeRegistry(EDITS_AND_IDEAS), FakeRepo({"morning_brief": "111"}),
                     get_settings(), slot=0)

        brief = next(text for channel, text in sent if channel == "111")
        assert brief.startswith("**Daily brief**")
        assert "Annie" not in brief.splitlines()[0]
        assert "UTC" not in brief.splitlines()[0]

    async def test_a_six_hourly_brief_still_says_which_kind_it_is(
        self, seeded, monkeypatch
    ):
        """Dropping the timestamp must not also drop the window. A six-hour
        brief and a daily one carry different numbers under the same
        headings, and nothing else in the message distinguishes them."""
        from app.config import get_settings
        from app.scheduling.jobs import _cycle

        sent = _capture(monkeypatch)
        await _cycle(FakeRegistry(EDITS), FakeRepo({"morning_brief": "111"}),
                     get_settings(), slot=12)

        brief = next(text for channel, text in sent if channel == "111")
        assert brief.startswith("**6-hour brief**")

    async def test_the_days_ideas_are_actually_sent(self, seeded, monkeypatch):
        """They were generated, written to a memory file, and then posted
        nowhere. `format_for_delivery` existed and was tested; nothing in
        production ever called it, so the brief-channel confirmation message
        promised "the day's three launch ideas" to a channel that would never
        receive one.
        """
        from app.config import get_settings
        from app.scheduling.jobs import _cycle

        sent = _capture(monkeypatch)
        repo = FakeRepo({"morning_brief": "111"})

        result = await _cycle(FakeRegistry(EDITS_AND_IDEAS), repo, get_settings(), slot=0)

        assert result["ideas_delivered"] is True
        bodies = [text for _, text in sent]
        assert any("Launch ideas" in b for b in bodies), "the ideas were never posted"

    async def test_the_ideas_go_to_their_own_channel_when_one_is_set(
        self, seeded, monkeypatch
    ):
        """A proposal and a report are different things to act on, and
        pinning an idea is awkward when it is the tail of a status summary."""
        from app.config import get_settings
        from app.scheduling.jobs import _cycle

        sent = _capture(monkeypatch)
        repo = FakeRepo({"morning_brief": "111", "launch_ideas": "222"})

        result = await _cycle(FakeRegistry(EDITS_AND_IDEAS), repo, get_settings(), slot=0)

        assert result["ideas_channel_id"] == "222"
        ideas_post = next(text for channel, text in sent if channel == "222")
        assert "Launch ideas" in ideas_post
        brief_post = next(text for channel, text in sent if channel == "111")
        assert "brief" in brief_post.lower()

    async def test_ideas_still_arrive_when_only_their_own_channel_is_set(
        self, seeded, monkeypatch
    ):
        """Losing the brief is not a reason to also drop the thing the brief
        was merely going to sit above."""
        from app.config import get_settings
        from app.scheduling.jobs import _cycle

        sent = _capture(monkeypatch)
        repo = FakeRepo({"launch_ideas": "222"})

        result = await _cycle(FakeRegistry(EDITS_AND_IDEAS), repo, get_settings(), slot=0)

        assert result["delivered"] is False, "there is no brief channel"
        assert result["ideas_delivered"] is True
        assert [channel for channel, _ in sent] == ["222"]

    async def test_a_six_hourly_slot_sends_no_ideas(self, seeded, monkeypatch):
        """Ideas are a judgement about what to do next, and one that changes
        every six hours is noise."""
        from app.config import get_settings
        from app.scheduling.jobs import _cycle

        sent = _capture(monkeypatch)
        repo = FakeRepo({"morning_brief": "111"})

        result = await _cycle(FakeRegistry(EDITS_AND_IDEAS), repo, get_settings(), slot=12)

        assert "ideas_delivered" not in result
        assert not any("Launch ideas" in text for _, text in sent)

    async def test_nothing_is_posted_when_no_ideas_were_generated(
        self, isolated_memory, monkeypatch
    ):
        """An empty ledger produces no ideas by design. Posting a heading
        with nothing under it would read as a failure of the market rather
        than an honest abstention."""
        from app.config import get_settings
        from app.memory import bootstrap
        from app.scheduling.jobs import _cycle

        bootstrap._seed_files()
        sent = _capture(monkeypatch)
        repo = FakeRepo({"morning_brief": "111"})

        result = await _cycle(FakeRegistry(EDITS), repo, get_settings(), slot=0)

        assert result["ideas_delivered"] is False
        assert result["ideas_reason"] == "none were generated"
        assert not any("Launch ideas" in text for _, text in sent)

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
