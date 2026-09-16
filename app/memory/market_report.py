"""The cultural battlefield, compressed.

The brief used to answer "which coins crossed a tier", which is a list of
outcomes. This answers the three questions an operator deciding what to build
actually has:

* **What is being launched?** Across everything, not only the winners — the
  shape of what people are making right now is the market, and the ~90 a day
  that clear a tier are a sample of its tail.
* **What is working?** Which of those categories is actually converting
  launches into tier-crossers, which is a different question from which is
  most numerous.
* **Where should I look?** The part worth reading. Not "buy this" — angles
  worth investigating, derived from the gap between what is being made and
  what is succeeding.

**Why the launch mix matters more than the winner list.** A thousand AI coins
launching and two clearing a tier tells you the category is saturated and
converting badly. Two hundred launching with six clearing tells you the
opposite, and the winner list alone cannot distinguish them — both show up as
"a few AI winners". The conversion rate is the finding; the raw count is the
denominator that makes it one.

Everything here is deterministic, computed from rows. No model call, which is
what makes it affordable to put at the top of every brief.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from app.analysis.features import extract_all
from app.memory import db

log = structlog.get_logger(__name__)

#: Categories below this share are rolled into "other" rather than listed.
#: A breakdown with fourteen entries is not a breakdown.
MIN_SHARE = 0.04

#: How many categories to name before the tail.
MAX_LISTED = 5


@dataclass(slots=True)
class CategoryStat:
    name: str
    launches: int
    qualified: int

    @property
    def conversion(self) -> float:
        return (self.qualified / self.launches) if self.launches else 0.0


@dataclass(slots=True)
class MarketReport:
    window_hours: int
    total_launches: int
    total_qualified: int
    categorised: int
    categories: list[CategoryStat] = field(default_factory=list)
    #: Categories whose share grew most since the previous window.
    rising: list[tuple[str, float, float]] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        """How much of the window we could actually categorise.

        Stated rather than hidden. Categories come from metadata that is
        fetched in batches, so a fresh window is always partly unread, and a
        breakdown over 12% of launches is a different claim from one over
        90%.
        """
        return (self.categorised / self.total_launches) if self.total_launches else 0.0


def _categorise(name: str | None, symbol: str | None, description: str | None) -> list[str]:
    themes = {
        f.value for f in extract_all(name, symbol, description)
        if f.key == "theme" and f.value != "uncategorised"
    }
    return sorted(themes)


def _rows(start: datetime, end: datetime) -> list[Any]:
    return db.query(
        """
        SELECT name, symbol, description, qualified_at
          FROM sightings
         WHERE first_seen >= ? AND first_seen < ?
        """,
        (start.isoformat(), end.isoformat()),
    )


def _tally(rows: list[Any]) -> tuple[dict[str, CategoryStat], int, int, int]:
    """Count each token once, under its most specific theme.

    A cat-lawyer coin is both "animal" and "crypto culture", and counting it
    under both gives a breakdown summing to 140% — which is honest and
    unreadable. Each token gets the *rarest* theme it carries, on the
    reasoning that the rare one is the informative one: "animal" describes
    thousands of coins a day and tells you nothing, while the narrower label
    is the thing that distinguishes this launch from the rest of them.
    """
    themed: list[tuple[list[str], bool]] = []
    frequency: dict[str, int] = {}
    qualified = 0

    for row in rows:
        themes = _categorise(row["name"], row["symbol"], row["description"])
        is_q = bool(row["qualified_at"])
        qualified += int(is_q)
        if not themes:
            continue
        themed.append((themes, is_q))
        for theme in themes:
            frequency[theme] = frequency.get(theme, 0) + 1

    stats: dict[str, CategoryStat] = {}
    for themes, is_q in themed:
        primary = min(themes, key=lambda t: (frequency[t], t))
        stat = stats.setdefault(primary, CategoryStat(primary, 0, 0))
        stat.launches += 1
        stat.qualified += int(is_q)

    return stats, len(themed), qualified, len(rows)


def build(*, window_hours: int = 6, now: datetime | None = None) -> MarketReport:
    """What is being launched, what is converting, and what changed."""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(hours=window_hours)

    stats, categorised, qualified, total = _tally(_rows(start, now))

    report = MarketReport(
        window_hours=window_hours,
        total_launches=total,
        total_qualified=qualified,
        categorised=categorised,
    )
    if not stats:
        return report

    report.categories = sorted(
        stats.values(), key=lambda s: (-s.launches, s.name)
    )

    # What changed since the window before this one. A category at 20% is
    # information; a category that was 6% yesterday and is 20% today is the
    # thing worth acting on, and only the comparison shows it.
    previous, prev_categorised, _, _ = _tally(
        _rows(start - timedelta(hours=window_hours), start)
    )
    if prev_categorised >= 20 and categorised >= 20:
        moves: list[tuple[str, float, float]] = []
        for stat in report.categories:
            now_share = stat.launches / categorised
            was = previous.get(stat.name)
            was_share = (was.launches / prev_categorised) if was else 0.0
            if now_share - was_share >= 0.03:
                moves.append((stat.name, was_share, now_share))
        report.rising = sorted(moves, key=lambda m: m[2] - m[1], reverse=True)[:3]

    return report


def _pretty(name: str) -> str:
    return name.replace("_", " ")


def render(report: MarketReport, *, label: str = "6H") -> list[str]:
    """The report as brief lines. Deterministic — no model call."""
    if not report.total_launches:
        return []

    lines = [f"**MARKET MEME REPORT | {label}**", ""]

    if not report.categories:
        lines += [
            f"{report.total_launches:,} launches, none categorised yet — "
            f"metadata is still being fetched.",
            "",
        ]
        return lines

    listed = [c for c in report.categories if c.launches / report.categorised >= MIN_SHARE]
    listed = listed[:MAX_LISTED]
    shown = sum(c.launches for c in listed)
    tail = max(0, report.categorised - shown)

    parts = [
        f"{_pretty(c.name)} {c.launches / report.categorised:.0%}" for c in listed
    ]
    if tail:
        parts.append(f"other {tail / report.categorised:.0%}")
    lines.append("Launching: " + " · ".join(parts))
    lines.append(
        f"_{report.total_launches:,} launches, {report.categorised:,} readable "
        f"({report.coverage:.0%}) · {report.total_qualified} crossed a tier_"
    )
    lines.append("")

    # What is converting, which is not the same as what is numerous.
    converting = [
        c for c in report.categories if c.qualified and c.launches >= 5
    ]
    converting.sort(key=lambda c: (-c.conversion, -c.qualified))
    if converting:
        lines.append("Working:")
        for c in converting[:5]:
            lines.append(
                f"· {_pretty(c.name)} — {c.qualified} of {c.launches} crossed"
            )
        lines.append("")
    elif report.total_qualified == 0:
        lines += ["Working: nothing crossed a tier in this window.", ""]

    if report.rising:
        moves = ", ".join(
            f"{_pretty(name)} {was:.0%} → {now:.0%}" for name, was, now in report.rising
        )
        lines += [f"Changed since last report: {moves}", ""]

    return lines


def look_toward(report: MarketReport, *, limit: int = 3) -> list[str]:
    """Angles worth investigating, from the gap between made and working.

    Deliberately not "launch this". The useful observation is structural: a
    category converting well on few launches is an opening, and one
    converting badly on many is a crowd. Naming which is which is something
    the numbers can support; picking the coin is not.
    """
    if not report.categories or report.categorised < 20:
        return []

    angles: list[str] = []

    crowded = [
        c for c in report.categories
        if c.launches / report.categorised >= 0.15 and c.conversion < 0.01
    ]
    opening = [
        c for c in report.categories
        if c.qualified >= 2 and c.launches >= 5 and c.conversion >= 0.03
    ]
    opening.sort(key=lambda c: -c.conversion)

    for c in opening[:2]:
        angles.append(
            f"{_pretty(c.name)} is converting at {c.qualified} of {c.launches} "
            f"while only {c.launches / report.categorised:.0%} of launches are "
            f"trying it — the thin end of something that works."
        )
    for name, was, now in report.rising[:1]:
        angles.append(
            f"{_pretty(name)} went {was:.0%} → {now:.0%} of launches this "
            f"window. Early enough to be worth a look, crowded if it holds."
        )
    for c in crowded[:1]:
        angles.append(
            f"{_pretty(c.name)} is {c.launches / report.categorised:.0%} of "
            f"launches and converting almost nothing — a copy of the obvious "
            f"version is competing with everyone."
        )

    return angles[:limit]
