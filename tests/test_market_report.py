"""What is being launched, what is converting, and where to look.

The brief answered "which coins crossed a tier", which is a list of outcomes.
An operator deciding what to *build* needs the denominator: a thousand AI
coins launching with two winners and two hundred launching with six look
identical in a winner list and mean opposite things.

The categorisation behind it was also broken in a way worth pinning. Every
caller passed `None` for the description, so themes were derived from a
three-to-eight character ticker and a two-word name — which returned
"uncategorised" for 86 of 96 tokens in the $1M cohort.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.memory import db, ledger, market_report

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def _launch(i, description, *, qualified=False, hours_ago=1.0, symbol=None):
    mint = f"Mint{i:040d}"
    ledger.record_launch(mint=mint, creator=f"W{i}", symbol=symbol or f"T{i}", name=f"Token {i}")
    db.execute(
        "UPDATE sightings SET description = ?, first_seen = ? WHERE mint = ?",
        (description, (NOW - timedelta(hours=hours_ago)).isoformat(), mint),
    )
    if qualified:
        db.execute(
            "UPDATE sightings SET qualified_at = ? WHERE mint = ?", (NOW.isoformat(), mint)
        )
    return mint


AI = "An autonomous AI agent swarm that trades on chain for you"
CAT = "A tabby cat in a courtroom objecting on behalf of holders"
POLITICS = "A political meme about last night's election debate"


class TestTheDescriptionIsWhatMakesThisWork:
    def test_a_ticker_alone_categorises_nothing(self):
        """The bug, pinned. This is what every caller was doing."""
        from app.analysis.features import extract_all

        themes = {f.value for f in extract_all("LAWCAT", "LAWCAT", None) if f.key == "theme"}

        assert themes == {"uncategorised"}

    def test_the_description_categorises_it(self):
        from app.analysis.features import extract_all

        themes = {f.value for f in extract_all("LAWCAT", "LAWCAT", CAT) if f.key == "theme"}

        assert "uncategorised" not in themes
        assert "animal" in themes


class TestTheBreakdown:
    def test_it_counts_what_is_being_launched_not_only_winners(self, isolated_memory):
        for i in range(10):
            _launch(i, AI)
        for i in range(10, 15):
            _launch(i, POLITICS, qualified=True)

        report = market_report.build(window_hours=6, now=NOW)

        assert report.total_launches == 15
        assert report.total_qualified == 5
        names = {c.name for c in report.categories}
        assert "ai" in names, "losers must be counted — they are the denominator"

    def test_each_token_is_counted_once(self, isolated_memory):
        """A cat-lawyer coin is animal *and* crypto culture; counting it
        under both gives a breakdown summing to 140%."""
        for i in range(20):
            _launch(i, CAT)

        report = market_report.build(window_hours=6, now=NOW)

        assert sum(c.launches for c in report.categories) == 20

    def test_conversion_is_reported_separately_from_volume(self, isolated_memory):
        """The whole point. Most numerous and best converting are different
        questions, and the winner list cannot tell them apart."""
        for i in range(100):
            _launch(i, AI, qualified=(i < 1))
        for i in range(100, 120):
            _launch(i, POLITICS, qualified=(i < 104))

        report = market_report.build(window_hours=6, now=NOW)
        by_name = {c.name: c for c in report.categories}

        assert by_name["ai"].launches > by_name["politics"].launches
        assert by_name["politics"].conversion > by_name["ai"].conversion

    def test_coverage_is_stated_not_hidden(self, isolated_memory):
        """A breakdown over 12% of launches is a different claim from one
        over 90%, and metadata arrives in batches."""
        for i in range(10):
            _launch(i, AI)
        for i in range(10, 20):
            _launch(i, None)

        report = market_report.build(window_hours=6, now=NOW)

        assert report.total_launches == 20
        assert report.categorised == 10
        assert report.coverage == pytest.approx(0.5)

    def test_an_empty_window_renders_nothing(self, isolated_memory):
        assert market_report.render(market_report.build(now=NOW)) == []


class TestWhatChanged:
    def test_a_rising_category_is_named_with_both_numbers(self, isolated_memory):
        """A category at 20% is information; one that was 6% yesterday and is
        20% today is the thing worth acting on."""
        for i in range(40):
            _launch(i, AI, hours_ago=9)
        for i in range(40, 45):
            _launch(i, CAT, hours_ago=9)

        for i in range(100, 120):
            _launch(i, AI, hours_ago=1)
        for i in range(120, 145):
            _launch(i, CAT, hours_ago=1)

        report = market_report.build(window_hours=6, now=NOW)

        assert report.rising, "a category more than doubling its share was not noticed"
        name, was, now_share = report.rising[0]
        assert name == "animal"
        assert now_share > was

    def test_thin_windows_do_not_produce_comparisons(self, isolated_memory):
        """Three launches becoming six is not a trend."""
        for i in range(3):
            _launch(i, AI, hours_ago=9)
        for i in range(10, 16):
            _launch(i, CAT, hours_ago=1)

        assert market_report.build(window_hours=6, now=NOW).rising == []


class TestLookToward:
    def test_a_crowded_category_that_converts_nothing_is_called_out(
        self, isolated_memory
    ):
        for i in range(60):
            _launch(i, AI)
        for i in range(60, 80):
            _launch(i, CAT, qualified=(i < 63))

        angles = market_report.look_toward(market_report.build(window_hours=6, now=NOW))

        assert any("ai" in a and "converting almost nothing" in a for a in angles)

    def test_a_thin_but_converting_category_is_an_opening(self, isolated_memory):
        for i in range(60):
            _launch(i, AI)
        for i in range(60, 75):
            _launch(i, POLITICS, qualified=(i < 64))

        angles = market_report.look_toward(market_report.build(window_hours=6, now=NOW))

        assert any("politics" in a for a in angles)

    def test_it_never_names_a_coin_to_buy(self, isolated_memory):
        """It reports the shape of the battlefield. Picking the coin is the
        operator's job and the numbers cannot support it anyway."""
        for i in range(60):
            _launch(i, AI, qualified=(i < 3), symbol="AGENT")

        angles = market_report.look_toward(market_report.build(window_hours=6, now=NOW))

        assert not any("AGENT" in a for a in angles)

    def test_a_thin_window_offers_no_angles(self, isolated_memory):
        for i in range(5):
            _launch(i, AI)

        assert market_report.look_toward(market_report.build(window_hours=6, now=NOW)) == []


class TestRendering:
    def test_the_header_names_the_window(self, isolated_memory):
        for i in range(30):
            _launch(i, AI)

        lines = market_report.render(market_report.build(window_hours=6, now=NOW), label="6H")

        assert lines[0] == "**MARKET MEME REPORT | 6H**"
        assert any(line.startswith("Launching:") for line in lines)

    def test_shares_are_readable_percentages(self, isolated_memory):
        for i in range(30):
            _launch(i, AI)
        for i in range(30, 40):
            _launch(i, CAT)

        text = "\n".join(market_report.render(market_report.build(window_hours=6, now=NOW)))

        assert "%" in text
        assert "crossed a tier" in text
