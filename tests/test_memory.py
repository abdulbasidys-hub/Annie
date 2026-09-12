"""The memory substrate: files, retrieval, the ledger, and forgetting.

These lock in the properties the 2026-09-08 rewrite is actually claiming.
Three of them matter more than the rest, because they are the ones that would
quietly stop being true as the code changes:

* A contract address or creator wallet written into memory is retrievable by
  that exact string, in one index probe. This is what makes "what do you know
  about this CA" work from chat.
* A cycle's prompt stays small no matter how much memory accumulates.
* Forgetting actually deletes, and never deletes evidence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.memory import index, ledger, service
from app.memory.files import MemoryFile, MemoryStore, parse
from app.memory.paths import MemoryPathError, safe_relpath


class TestPathSafety:
    """Memory paths come from an LLM and from query strings. Both untrusted."""

    @pytest.mark.parametrize(
        "bad",
        [
            "../../etc/passwd",
            "core/../../../secrets.md",
            "/etc/passwd",
            "unknown_section/file.md",
            "toplevel.md",
            "core/sub/deeper/too-far.md",
            "core/..md/../x.md",
        ],
    )
    def test_rejects_paths_that_escape_or_invent_sections(self, bad):
        with pytest.raises(MemoryPathError):
            safe_relpath(bad)

    def test_accepts_and_normalises_a_good_path(self):
        assert safe_relpath("core/market-model") == "core/market-model.md"
        assert safe_relpath("  creators/ABC123.md  ") == "creators/ABC123.md"
        assert safe_relpath("daily\\2026-09-08.md") == "daily/2026-09-08.md"

    async def test_a_malicious_path_cannot_write_outside_the_root(self, isolated_memory):
        with pytest.raises(MemoryPathError):
            await service.write("../escaped.md", body="should not exist")
        assert not (isolated_memory.parent / "escaped.md").exists()


class TestFileFormat:
    def test_roundtrips_through_frontmatter(self):
        original = MemoryFile(
            path="notes/x.md", title="A note", body="Some prose.",
            tags=["a", "b"], keys=["MINT123"], importance=0.75, confidence="high",
        )
        parsed = parse("notes/x.md", original.render())
        assert parsed.title == "A note"
        assert parsed.tags == ["a", "b"]
        assert parsed.keys == ["MINT123"]
        assert parsed.importance == 0.75
        assert parsed.body == "Some prose."

    def test_a_broken_header_still_yields_the_body(self):
        """Losing a memory to a stray colon would be far worse than losing
        its metadata, so the parser degrades rather than raising."""
        broken = "---\nthis line has no separator\ntitle: Kept\n---\n\nThe prose survives."
        parsed = parse("notes/x.md", broken)
        assert parsed.title == "Kept"
        assert "The prose survives." in parsed.body

    def test_a_file_with_no_header_is_still_a_memory(self):
        parsed = parse("notes/hand-written.md", "Just some markdown someone dropped in.")
        assert parsed.body.startswith("Just some markdown")
        assert parsed.title  # derived from the filename

    def test_writes_are_atomic_and_leave_no_temp_files(self, isolated_memory):
        store = MemoryStore()
        store.write(MemoryFile(path="notes/a.md", title="A", body="one"))
        store.write(MemoryFile(path="notes/a.md", title="A", body="two"))
        assert store.read("notes/a.md").body == "two"
        assert list((isolated_memory / "notes").glob("*.tmp")) == []

    def test_created_is_preserved_across_rewrites(self, isolated_memory):
        store = MemoryStore()
        first = store.write(MemoryFile(path="notes/a.md", title="A", body="one"))
        second = store.write(MemoryFile(path="notes/a.md", title="A", body="two"))
        assert second.created == first.created
        assert second.updated >= first.updated


class TestRetrieval:
    async def test_a_contract_address_resolves_in_one_key_lookup(self, isolated_memory):
        mint = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
        await service.write(
            "tokens/token.md",
            body=f"A token that ran. CA: `{mint}`.",
            title="A token",
            keys=[mint],
        )
        hits = index.by_key(mint)
        assert [h.path for h in hits] == ["tokens/token.md"]
        assert hits[0].matched_key == mint.lower()

    async def test_an_address_only_mentioned_in_prose_is_still_findable(self, isolated_memory):
        """The operator's requirement — CA and creator retrievable from
        memory — must not depend on the model remembering to fill in a
        header field, so addresses are harvested from the body too."""
        mint = "9zAbtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgXYZ"
        await service.write(
            "notes/observation.md",
            body=f"The interesting one this week was {mint}, which round-tripped.",
            title="Observation",
        )
        assert [h.path for h in index.by_key(mint)] == ["notes/observation.md"]

    async def test_search_is_case_insensitive_for_handles(self, isolated_memory):
        wallet = "Wa11etABCDEF"
        await service.write("creators/w.md", body="Busy wallet.", title="W", keys=[wallet])
        assert index.by_key(wallet.lower())
        assert index.by_key(wallet.upper())

    async def test_full_text_finds_a_memory_by_idea_not_exact_words(self, isolated_memory):
        await service.write(
            "notes/cats.md",
            body="Cat-themed tickers are outperforming dog-themed ones this week.",
            title="Cats over dogs",
        )
        hits = index.search("cats outperforming")
        assert "notes/cats.md" in [h.path for h in hits]

    async def test_recall_respects_its_budget(self, isolated_memory):
        """The guard against a cycle quietly widening until it feeds the
        whole notebook to the model again."""
        for n in range(30):
            await service.write(
                f"notes/note-{n}.md",
                body=f"Observation number {n} about market behaviour and tokens.",
                title=f"Note {n}",
                keys=[f"key{n}"],
            )
        hits = index.recall(keys=[f"key{n}" for n in range(30)], budget=8)
        assert len(hits) == 8

    async def test_deleting_a_memory_removes_it_from_the_index(self, isolated_memory):
        await service.write("notes/gone.md", body="Temporary.", title="Gone", keys=["ghost"])
        assert index.by_key("ghost")
        await service.forget("notes/gone.md")
        assert index.by_key("ghost") == []
        assert index.search("Temporary") == []

    async def test_reindex_rebuilds_everything_from_the_files(self, isolated_memory):
        """The index must be safe to treat as disposable."""
        await service.write("notes/a.md", body="Alpha content.", title="A", keys=["alpha"])
        from app.memory import db

        db.execute("DELETE FROM docs")
        db.execute("DELETE FROM doc_keys")
        assert index.by_key("alpha") == []

        index.reindex_all()
        assert [h.path for h in index.by_key("alpha")] == ["notes/a.md"]


class TestLedger:
    def _launch(self, i: int, creator: str = "Wa11etAAA") -> None:
        ledger.record_launch(
            mint=f"Mint{i:040d}",
            creator=creator,
            launchpad="pumpfun",
            symbol=f"TKN{i}",
            name=f"Token {i}",
        )

    def test_a_launch_costs_one_sighting_and_one_creator_move(self, isolated_memory):
        self._launch(1)
        assert ledger.stats()["sightings_total"] == 1
        assert ledger.get_creator("Wa11etAAA")["launches"] == 1
        assert len(ledger.creator_moves("Wa11etAAA")) == 1

    def test_a_repeat_sighting_does_not_inflate_launch_counts(self, isolated_memory):
        """A redelivered webhook event must not make a wallet look busier
        than it is — the tracking decision is based on these counts."""
        self._launch(1)
        self._launch(1)
        self._launch(1)
        assert ledger.get_creator("Wa11etAAA")["launches"] == 1
        assert ledger.stats()["sightings_total"] == 1

    def test_volume_alone_does_not_make_a_wallet_worth_following(self, isolated_memory):
        """It used to. Measured in production, of the 200 highest-volume
        wallets every single one with zero winners had 25+ launches — so a
        launch-count threshold selects almost perfectly for spam bots, and
        six hundred of them were tracked with dossiers being written."""
        for i in range(ledger.TRACK_AFTER_LAUNCHES + 20):
            self._launch(i)

        creator = ledger.get_creator("Wa11etAAA")
        assert creator["launches"] == ledger.TRACK_AFTER_LAUNCHES + 20
        assert creator["tracked"] == 0, "a wallet that has produced nothing was tracked"

    def test_a_prolific_wallet_is_tracked_the_moment_it_finally_lands_one(
        self, isolated_memory
    ):
        """Nothing is lost by waiting. The launches and the movement history
        were being recorded the whole time."""
        for i in range(ledger.TRACK_AFTER_LAUNCHES + 5):
            self._launch(i)
        assert ledger.get_creator("Wa11etAAA")["tracked"] == 0

        ledger.record_price(
            mint="Mint" + "0" * 39 + "1", market_cap=500_000, liquidity=50_000, tier=250_000
        )

        creator = ledger.get_creator("Wa11etAAA")
        assert creator["tracked"] == 1
        assert creator["launches"] == ledger.TRACK_AFTER_LAUNCHES + 5, "history was lost"

    def test_a_single_winner_is_enough_to_track_a_wallet(self, isolated_memory):
        self._launch(1, creator="Wa11etLucky")
        ledger.record_price(
            mint="Mint" + "0" * 39 + "1", market_cap=500_000, liquidity=50_000, tier=250_000
        )
        assert ledger.get_creator("Wa11etLucky")["tracked"] == 1
        assert ledger.get_creator("Wa11etLucky")["winners"] == 1

    def test_qualifying_is_recorded_once_not_on_every_recheck(self, isolated_memory):
        self._launch(1)
        mint = "Mint" + "0" * 39 + "1"
        first = ledger.record_price(mint=mint, market_cap=500_000, liquidity=50_000, tier=250_000)
        second = ledger.record_price(mint=mint, market_cap=600_000, liquidity=50_000, tier=250_000)
        assert first["newly_qualified"] is True
        assert second["newly_qualified"] is False
        assert ledger.get_creator("Wa11etAAA")["winners"] == 1

    def test_peak_is_kept_after_a_round_trip(self, isolated_memory):
        self._launch(1)
        mint = "Mint" + "0" * 39 + "1"
        ledger.record_price(mint=mint, market_cap=800_000, liquidity=50_000, tier=250_000)
        ledger.record_price(mint=mint, market_cap=20_000, liquidity=50_000)
        sighting = ledger.get_sighting(mint)
        assert sighting.peak_market_cap == 800_000
        assert sighting.market_cap == 20_000

    def test_unpriced_mints_are_stamped_so_they_stop_being_reselected(self, isolated_memory):
        """Without this, mints with no trading pair — most of them — would
        be handed back by due_for_check on every single pass forever."""
        self._launch(1)
        mint = "Mint" + "0" * 39 + "1"
        assert ledger.get_sighting(mint).last_checked is None
        ledger.mark_checked([mint])
        assert ledger.get_sighting(mint).last_checked is not None
        assert ledger.get_sighting(mint).checks == 1


class TestForgetting:
    def test_prune_deletes_the_dead_and_keeps_the_evidence(self, isolated_memory):
        old = datetime.now(timezone.utc) - timedelta(days=5)
        for i in range(50):
            ledger.record_launch(
                mint=f"Mint{i:040d}", creator="Wa11etAAA", launchpad="pumpfun", launched_at=old
            )
        # One of them actually did something.
        winner = "Mint" + "0" * 39 + "1"
        ledger.record_price(mint=winner, market_cap=500_000, liquidity=50_000, tier=250_000)

        from app.memory import db

        db.execute("UPDATE sightings SET last_seen = ? WHERE qualified_at IS NULL",
                   (old.isoformat(timespec="seconds"),))

        result = ledger.prune(ttl_hours=48)

        assert result["sightings_dropped"] == 49
        assert ledger.get_sighting(winner) is not None
        assert ledger.get_creator("Wa11etAAA")["launches"] == 50, "creator history was lost"
        assert len(ledger.creator_moves("Wa11etAAA")) == 51, "movement history was lost"

    def test_a_tracked_creators_dead_tokens_are_pruned_too(self, isolated_memory):
        """Exempting them was the obvious-looking rule and it is wrong: a
        tracked wallet launches a lot, so the exemption would spare the
        largest share of the junk."""
        old = datetime.now(timezone.utc) - timedelta(days=5)
        for i in range(ledger.TRACK_AFTER_LAUNCHES + 5):
            ledger.record_launch(mint=f"Mint{i:040d}", creator="Wa11etBusy", launched_at=old)
        # Tracking is earned by a winner now, not by volume.
        ledger.record_price(
            mint=f"Mint{0:040d}", market_cap=500_000, liquidity=50_000, tier=250_000
        )
        assert ledger.get_creator("Wa11etBusy")["tracked"] == 1

        from app.memory import db

        db.execute("UPDATE sightings SET last_seen = ?", (old.isoformat(timespec="seconds"),))
        result = ledger.prune(ttl_hours=48)

        # Every dead one goes; the single winner stays, because it qualified.
        assert result["sightings_dropped"] == ledger.TRACK_AFTER_LAUNCHES + 4
        assert ledger.get_sighting(f"Mint{0:040d}") is not None
        assert ledger.get_creator("Wa11etBusy") is not None

    def test_something_that_traded_survives_even_without_qualifying(self, isolated_memory):
        old = datetime.now(timezone.utc) - timedelta(days=5)
        ledger.record_launch(mint="Mint" + "0" * 40, creator="W", launched_at=old)
        mint = "Mint" + "0" * 40
        ledger.record_price(mint=mint, market_cap=60_000, liquidity=20_000)

        from app.memory import db

        db.execute("UPDATE sightings SET last_seen = ?", (old.isoformat(timespec="seconds"),))
        ledger.prune(ttl_hours=48)

        assert ledger.get_sighting(mint) is not None

    def test_a_qualifier_older_than_every_window_is_eventually_dropped(self, isolated_memory):
        """"Kept forever" was written when a qualifier was rare.

        At real volume roughly one token a minute clears the $100k floor —
        about half a million rows a year that no window reads, since signals
        compare a 7-day slice against a 90-day baseline.
        """
        from app.memory import db

        ancient = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        recent = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        for i, when in ((1, ancient), (2, recent)):
            mint = f"Mint{i:040d}"
            ledger.record_launch(mint=mint, creator="W")
            ledger.record_price(mint=mint, market_cap=300_000, liquidity=50_000, tier=250_000)
            db.execute("UPDATE sightings SET qualified_at = ? WHERE mint = ?", (when, mint))

        result = ledger.prune(ttl_hours=48, keep_qualified_days=150)

        assert result["expired_qualifiers"] == 1
        assert ledger.get_sighting(f"Mint{1:040d}") is None
        assert ledger.get_sighting(f"Mint{2:040d}") is not None, "a live qualifier was dropped"

    def test_a_qualifier_annie_wrote_about_is_kept_however_old(self, isolated_memory):
        """A memory file carries a contract address and is looked up by it.
        Expiring the row underneath would leave a page pointing at nothing."""
        from app.memory import db

        mint = f"Mint{7:040d}"
        ledger.record_launch(mint=mint, creator="W")
        ledger.record_price(mint=mint, market_cap=900_000, liquidity=90_000, tier=250_000)
        db.execute(
            "UPDATE sightings SET qualified_at = ? WHERE mint = ?",
            ((datetime.now(timezone.utc) - timedelta(days=400)).isoformat(), mint),
        )
        db.execute(
            "INSERT INTO doc_keys(key, path) VALUES(?, ?)", (mint, f"tokens/{mint}.md")
        )

        result = ledger.prune(ttl_hours=48, keep_qualified_days=150)

        assert result["expired_qualifiers"] == 0
        assert ledger.get_sighting(mint) is not None


class TestWindowBoundaries:
    """The clock-resolution bug, pinned.

    `datetime.now()` has finite resolution — coarse on Windows — so a token
    qualified moments before a cycle starts can carry a timestamp identical
    to that cycle's `now`. With an exclusive end bound it was dropped from
    that window, and because the next window begins at the same instant, it
    was dropped from that one too: permanently invisible to signals. Observed
    as the same code returning 2, 6, 9 and 10 out of 10 on successive runs.
    """

    def test_a_token_qualified_at_the_exact_end_bound_is_included(self, isolated_memory):
        ledger.record_launch(mint="Mint" + "0" * 40, creator="W", symbol="T", name="Token")
        mint = "Mint" + "0" * 40
        ledger.record_price(mint=mint, market_cap=300_000, liquidity=50_000, tier=250_000)

        exact = datetime.fromisoformat(ledger.get_sighting(mint).qualified_at)
        found = ledger.qualified_in_window(exact - timedelta(days=1), exact)

        assert [s.mint for s in found] == [mint]

    def test_adjacent_windows_do_not_double_count_the_boundary(self, isolated_memory):
        """The other half: baseline and recent windows share an edge, and a
        token sitting exactly on it must be counted once, in the later one."""
        ledger.record_launch(mint="Mint" + "0" * 40, creator="W", symbol="T", name="Token")
        mint = "Mint" + "0" * 40
        ledger.record_price(mint=mint, market_cap=300_000, liquidity=50_000, tier=250_000)

        boundary = datetime.fromisoformat(ledger.get_sighting(mint).qualified_at)
        earlier = ledger.qualified_in_window(
            boundary - timedelta(days=90), boundary, end_inclusive=False
        )
        later = ledger.qualified_in_window(boundary, boundary + timedelta(days=1))

        assert earlier == []
        assert [s.mint for s in later] == [mint]

    def test_every_qualified_token_reaches_the_signal_engine(self, isolated_memory):
        """The end-to-end version of the same thing — the failure that
        actually surfaced the bug."""
        from app.memory import signals

        for i in range(10):
            ledger.record_launch(
                mint=f"Mint{i:040d}", creator="W", symbol="CAT", name="Quantum Cat"
            )
            ledger.record_price(
                mint=f"Mint{i:040d}", market_cap=300_000, liquidity=50_000, tier=250_000
            )

        signals.recompute()
        rows = signals.listing(limit=50, include_thin=True)

        assert rows, "no signals were produced at all"
        assert max(r["recent_total"] for r in rows) == 10, (
            "the signal engine did not see every qualified token"
        )
