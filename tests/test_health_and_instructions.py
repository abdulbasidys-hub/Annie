"""Two things that make an empty deployment usable instead of baffling.

**The diagnosis.** "No data" has several causes needing different actions, and
undistinguished they all produce the same reply: a list of zeros. Honest, but
useless — and it makes a broken pipeline look identical to a new one. These
tests pin each state to the right verdict, because a wrong verdict costs the
operator an afternoon looking in the wrong place.

**Standing instructions.** Something the operator told Annie to keep doing has
to reach every future cycle and every future conversation, whether or not that
particular moment happened to search for it. An instruction that only surfaces
on a matching query is not standing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.annie.agent import _tool_remember_instruction, _tool_system_status
from app.memory import bootstrap, db, health, index, ledger, service


class FakeAgent:
    pass


@pytest.fixture
def agent(isolated_memory):
    bootstrap._seed_files()
    index.reindex_all()
    return FakeAgent()


def _age(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _durability(monkeypatch, *, looks_like_volume: bool) -> None:
    """Force the durability verdict.

    The test fixture points ANNIE_MEMORY_DIR at a temp directory outside the
    working tree, which is exactly the shape `durability_report` reads as a
    mounted volume — so without this every test would silently run in the
    "durable" branch and the volume-detection case would never be exercised.
    """
    monkeypatch.setattr(
        health,
        "durability_report",
        lambda: {
            "root": "/data/memory" if looks_like_volume else "/app/memory",
            "configured": looks_like_volume,
            "looks_like_volume": looks_like_volume,
            "writable": True,
            "note": "",
        },
    )


class TestDiagnosis:
    def test_a_deployment_that_never_received_anything_blames_the_webhook(
        self, agent, monkeypatch
    ):
        _durability(monkeypatch, looks_like_volume=False)
        report = health.diagnose()

        assert report["state"] == "never_started"
        assert "not reaching this deployment" in report["headline"]
        assert any("Helius webhook" in a for a in report["what_to_check"])
        # And it must not read as a market observation.
        assert "quiet market" in report["headline"]

    def test_it_does_not_tell_you_to_attach_a_volume_when_one_is_attached(
        self, agent, monkeypatch
    ):
        """Sending someone to fix storage when their webhook is dead wastes
        their afternoon."""
        _durability(monkeypatch, looks_like_volume=True)
        report = health.diagnose()

        assert report["state"] == "never_started"
        assert not any("Volume" in a for a in report["what_to_check"])

    def test_arriving_but_never_surviving_points_at_the_volume(self, agent, monkeypatch):
        """The signature of a missing volume: data comes in, the notebook
        never grows past its seeded files."""
        _durability(monkeypatch, looks_like_volume=False)
        ledger.record_launch(mint="Mint" + "0" * 40, creator="W", symbol="T", name="Token")
        report = health.diagnose()

        assert report["state"] == "memory_not_durable"
        assert any("Volume" in a for a in report["what_to_check"])

    def test_a_stopped_stream_is_not_reported_as_a_quiet_market(self, agent, monkeypatch):
        """The failure that used to be invisible — a dead webhook and a quiet
        market produce identical numbers."""
        _durability(monkeypatch, looks_like_volume=True)
        ledger.record_launch(mint="Mint" + "0" * 40, creator="W", symbol="T", name="Token")
        # Age everything out of the last hour.
        db.execute("UPDATE sightings SET first_seen = ?, last_seen = ?", (_age(9), _age(9)))

        report = health.diagnose()

        assert report["state"] == "stream_stopped"
        assert "should be seconds, not hours" in report["headline"]

    def test_a_filling_ledger_with_no_winners_is_just_new(self, agent, monkeypatch):
        _durability(monkeypatch, looks_like_volume=True)
        for i in range(50):
            ledger.record_launch(mint=f"Mint{i:040d}", creator=f"W{i}", symbol="T", name="Token")

        report = health.diagnose()

        assert report["state"] == "warming_up"
        assert "nothing to fix" in " ".join(report["what_to_check"]).lower()

    def test_a_thin_cohort_is_labelled_as_sample_size_not_fault(self, agent, monkeypatch):
        _durability(monkeypatch, looks_like_volume=True)
        for i in range(50):
            ledger.record_launch(mint=f"Mint{i:040d}", creator=f"W{i}", symbol="T", name="Token")
        for i in range(3):
            ledger.record_price(
                mint=f"Mint{i:040d}", market_cap=300_000, liquidity=50_000, tier=250_000
            )

        report = health.diagnose()

        assert report["state"] == "warming_up"
        assert "statistically meaningful" in report["headline"]

    async def test_the_tool_hands_annie_the_verdict_not_just_counts(self, agent, monkeypatch):
        _durability(monkeypatch, looks_like_volume=True)
        result = await _tool_system_status(agent, {})

        assert result["state"] == "never_started"
        assert result["headline"]
        assert result["what_to_check"]
        assert "expected_rate" in result["stream"]

    async def test_the_dashboard_tells_her_not_to_recite_zeros(self, agent, monkeypatch):
        _durability(monkeypatch, looks_like_volume=True)
        from app.annie.agent import _tool_dashboard_summary

        result = await _tool_dashboard_summary(agent, {})

        assert result["pipeline_state"] == "never_started"
        assert "why_there_is_nothing" in result
        assert "do NOT recite the zeros" in result["note"]

    async def test_a_healthy_deployment_says_nothing_alarming(self, agent, monkeypatch):
        _durability(monkeypatch, looks_like_volume=True)
        for i in range(60):
            ledger.record_launch(mint=f"Mint{i:040d}", creator=f"W{i}", symbol="T", name="Token")
            ledger.record_price(
                mint=f"Mint{i:040d}", market_cap=300_000, liquidity=50_000, tier=250_000
            )
        db.kv_set(
            "scheduler:scheduler_cycle",
            '{"last_run_at": "' + datetime.now(timezone.utc).isoformat() + '"}',
        )

        report = health.diagnose()

        assert report["state"] == "healthy"
        assert report["what_to_check"] == []
        assert health.summary_line() == ""


class TestStandingInstructions:
    async def test_an_instruction_is_recorded_and_confirmed(self, agent):
        result = await _tool_remember_instruction(
            agent, {"instruction": "From now on, keep track of which narratives are crowded."}
        )

        assert result["saved"] is True
        assert result["path"] == "core/instructions.md"
        body = service.read("core/instructions.md").body
        assert "keep track of which narratives are crowded" in body

    async def test_the_placeholder_disappears_once_there_is_a_real_one(self, agent):
        assert "_No standing instructions yet._" in service.read("core/instructions.md").body

        await _tool_remember_instruction(agent, {"instruction": "Always include the CA."})

        body = service.read("core/instructions.md").body
        assert "_No standing instructions yet._" not in body
        assert "Always include the CA." in body

    async def test_instructions_accumulate(self, agent):
        await _tool_remember_instruction(agent, {"instruction": "First rule."})
        await _tool_remember_instruction(agent, {"instruction": "Second rule."})

        body = service.read("core/instructions.md").body
        assert "First rule." in body and "Second rule." in body

    async def test_an_instruction_can_name_what_it_applies_to(self, agent):
        await _tool_remember_instruction(
            agent,
            {
                "instruction": "Log every wallet that launches more than ten times a day.",
                "applies_to": "creators/",
            },
        )
        assert "Applies to: creators/" in service.read("core/instructions.md").body

    async def test_empty_instructions_are_refused(self, agent):
        result = await _tool_remember_instruction(agent, {"instruction": "   "})
        assert result["saved"] is False

    def test_a_fresh_deployment_has_no_standing_instructions(self, agent):
        """The seeded placeholder must not read as an instruction."""
        assert bootstrap.standing_instructions() == ""

    async def test_they_reach_every_cycle(self, agent):
        """An instruction that only surfaces on a matching search is not
        standing — it has to be in the prompt whether or not this window
        happened to remind her of it."""
        from app.memory import digest

        await _tool_remember_instruction(
            agent, {"instruction": "Track which launchpad the winners come from."}
        )
        for i in range(25):
            ledger.record_launch(
                mint=f"Mint{i:040d}", creator=f"W{i % 3}", launchpad="pumpfun",
                symbol="CAT", name="Quantum Cat",
            )
            ledger.record_price(
                mint=f"Mint{i:040d}", market_cap=300_000, liquidity=50_000, tier=250_000
            )

        rendered = digest.build(window_hours=24).render()

        assert "Standing instructions from the operator" in rendered
        assert "Track which launchpad the winners come from." in rendered
        # Last in the prompt, where it is read most recently before answering.
        assert rendered.rstrip().endswith("Track which launchpad the winners come from.")

    async def test_they_reach_every_conversation(self, agent):
        from app.annie.agent import _standing_instructions_note

        assert _standing_instructions_note() == ""

        await _tool_remember_instruction(agent, {"instruction": "Always name the creator wallet."})
        note = _standing_instructions_note()

        assert "Always name the creator wallet." in note
        assert "outrank your own judgement" in note

    async def test_the_instructions_file_cannot_be_deleted(self, agent):
        from app.annie.agent import _tool_delete_memory

        result = await _tool_delete_memory(agent, {"path": "core/instructions.md"})

        assert result["deleted"] is False
        assert service.read("core/instructions.md") is not None

    async def test_the_digest_stays_small_with_instructions(self, agent):
        """Instructions are loaded whole rather than retrieved, so they are the
        one section that could grow unbounded. Worth watching."""
        from app.memory import digest

        for n in range(12):
            await _tool_remember_instruction(
                agent, {"instruction": f"Standing rule number {n} about market behaviour."}
            )
        for i in range(25):
            ledger.record_launch(
                mint=f"Mint{i:040d}", creator=f"W{i % 3}", symbol="CAT", name="Quantum Cat"
            )
            ledger.record_price(
                mint=f"Mint{i:040d}", market_cap=300_000, liquidity=50_000, tier=250_000
            )

        rendered = digest.build(window_hours=24).render()
        assert len(rendered) // 4 < 4000, f"cycle prompt grew to ~{len(rendered) // 4} tokens"
