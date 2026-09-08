"""What is over-represented among tokens that actually won.

This is the old :mod:`app.trends.engine` with its storage replaced and its
appetite cut. The statistics are unchanged and still live in
:mod:`app.analysis.stats` and :mod:`app.trends.lifecycle` — that code was
never the problem. What was the problem was how it fed itself: one Firestore
read per token per run to fetch a features subcollection, across hundreds of
tokens, four cohorts deep, producing thousands of trend documents of which
~93% were a word seen once. That single job was the largest line on the
Firestore bill.

Three changes fix it, and none of them cost accuracy:

1. **Features are computed, not stored.** A token's name/ticker/description
   features are derived from three short strings by pure functions
   (:mod:`app.analysis.features`). Recomputing them costs microseconds;
   storing them cost a document each. So nothing is persisted per token, and
   the cohort scan reads local SQLite rows instead of remote documents.

2. **Only winners are scanned.** The cohort is tokens that reached a tier —
   tens to low hundreds — not every token ever discovered.

3. **Signals are capped and pruned** (:data:`MAX_SIGNALS`). A characteristic
   that appears once and never returns is dropped rather than kept forever
   as a permanently-dead row to be recomputed each pass.

The output goes two places: a ``signals`` table the website and agent read,
and — for the handful that are actually meaningful — a line in the cycle
digest that the learning step turns into prose in a memory file. A number
nobody ever reads is a cost with no return, which is what most of the old
1,952 trend documents were.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import structlog

from app.analysis.features import extract_all
from app.analysis.stats import (
    MIN_OCCURRENCES,
    MIN_RECENT_SAMPLE,
    ComparisonResult,
    compare_proportions,
    trend_slope,
)
from app.db.enums import TrendStatus
from app.memory import db
from app.memory.ledger import Sighting
from app.trends.lifecycle import decide_status

log = structlog.get_logger(__name__)

#: Cohort floors. Same tiers the qualification rules use — a characteristic
#: over $100k tokens and over $1M tokens is genuinely two different claims
#: and must never be collapsed into one.
TIERS: tuple[float, ...] = (100_000.0, 250_000.0, 1_000_000.0)

RECENT_DAYS = 7
BASELINE_DAYS = 90

#: Which extracted characteristics become signals. Individual words are
#: deliberately absent: tracking every word produced 1,818 of 1,952 trend
#: documents, ~93% of them seen exactly once. Emergent word-level discovery
#: is covered by :func:`emerging_words` below, computed in memory per run and
#: never persisted per word.
TRACKED: dict[tuple[str, str], str] = {
    ("token", "theme"): "narrative",
    ("name", "theme"): "name",
    ("ticker", "shape"): "ticker",
    ("ticker", "theme"): "ticker",
    ("description", "theme"): "description",
    ("description", "present"): "description",
    ("launchpad", "slug"): "launchpad",
    ("social", "has_twitter"): "social",
}

#: Hard ceiling on stored signals. Past this, the weakest are dropped.
#: Without a cap, "keep everything we ever noticed" reasserts itself one row
#: at a time until the table is the old trends collection again.
MAX_SIGNALS = 400

Subject = tuple[str, str, str | None]


@dataclass(slots=True)
class SignalRun:
    started_at: datetime
    finished_at: datetime | None = None
    cohorts: int = 0
    evaluated: int = 0
    created: int = 0
    updated: int = 0
    promoted: list[str] = field(default_factory=list)
    faded: list[str] = field(default_factory=list)
    skipped: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cohorts": self.cohorts,
            "evaluated": self.evaluated,
            "created": self.created,
            "updated": self.updated,
            "promoted": self.promoted[:20],
            "faded": self.faded[:20],
            "skipped": self.skipped,
        }


def _subjects_of(sighting: Sighting) -> set[Subject]:
    """Derive one token's characteristics. Pure, cheap, nothing persisted.

    ``extract_all`` returns a flat ``list[Feature]`` that already includes
    the ``token.theme`` rollup, so this only has to filter to the tracked
    (namespace, key) pairs and add the launchpad, which is ledger state
    rather than anything derivable from the token's text.
    """
    subjects: set[Subject] = set()
    for feature in extract_all(sighting.name, sighting.symbol, None):
        if (feature.namespace, feature.key) in TRACKED:
            subjects.add((feature.namespace, feature.key, feature.value))
    if sighting.launchpad:
        subjects.add(("launchpad", "slug", sighting.launchpad))
    return subjects


def _counts(sightings: Iterable[Sighting]) -> tuple[dict[Subject, int], int]:
    counts: dict[Subject, int] = {}
    total = 0
    for sighting in sightings:
        total += 1
        for subject in _subjects_of(sighting):
            counts[subject] = counts.get(subject, 0) + 1
    return counts, total


def _slug(subject: Subject, tier: float) -> str:
    namespace, key, value = subject
    label = f"{int(tier // 1_000_000)}m" if tier >= 1_000_000 else f"{int(tier // 1000)}k"
    parts = [namespace, key]
    if value:
        parts.append("".join(c if c.isalnum() else "-" for c in str(value).lower())[:40])
    parts.append(label)
    return "-".join(p.strip("-") for p in parts if p)


def _name(subject: Subject) -> str:
    namespace, key, value = subject
    if value:
        return f"{str(value).replace('_', ' ').title()} ({namespace})"
    return f"{namespace}.{key}"


def recompute(now: datetime | None = None) -> SignalRun:
    """Rebuild every signal from the ledger. Runs once per 6-hour cycle.

    Entirely local: no Firestore, no network, no model. On a few hundred
    qualified tokens this is milliseconds, which is why it can afford to
    recompute from scratch rather than maintaining incremental state that
    could drift out of step with the ledger it claims to summarise.
    """
    from app.memory.ledger import qualified_in_window

    now = now or datetime.now(timezone.utc)
    run = SignalRun(started_at=now)

    recent_start = now - timedelta(days=RECENT_DAYS)
    baseline_start = now - timedelta(days=BASELINE_DAYS)
    day = now.date().isoformat()

    for tier in TIERS:
        recent = qualified_in_window(recent_start, now, min_tier=tier)
        if not recent:
            continue
        # Exclusive end so a token qualified exactly at the boundary is
        # counted once — in `recent`, not in both windows.
        baseline = qualified_in_window(
            baseline_start, recent_start, min_tier=tier, end_inclusive=False
        )
        run.cohorts += 1

        recent_counts, recent_total = _counts(recent)
        baseline_counts, baseline_total = _counts(baseline)

        for subject, count in recent_counts.items():
            comparison = compare_proportions(
                count, recent_total,
                baseline_counts.get(subject, 0), baseline_total,
                persistence_days=_persistence(_slug(subject, tier)),
            )
            _upsert(subject, tier, comparison, day=day, now=now, run=run)
            run.evaluated += 1

        # A characteristic that vanished this window still needs updating,
        # or a signal that stopped occurring displays last week's frequency
        # forever and never reaches "dead".
        _decay_absent(tier, set(recent_counts), recent_total, baseline_counts, baseline_total, day, now, run)

    _enforce_cap()
    run.finished_at = datetime.now(timezone.utc)
    log.info("signals_recomputed", **run.to_dict())
    return run


def _persistence(slug: str) -> int:
    """Consecutive recorded days where the characteristic actually occurred."""
    rows = db.query(
        "SELECT count FROM signal_points WHERE slug = ? ORDER BY day DESC LIMIT 30", (slug,)
    )
    streak = 0
    for row in rows:
        if (row["count"] or 0) > 0:
            streak += 1
        else:
            break
    return streak


def _series(slug: str, limit: int = 14) -> list[float]:
    rows = db.query(
        "SELECT freq FROM signal_points WHERE slug = ? ORDER BY day DESC LIMIT ?", (slug, limit)
    )
    return [float(r["freq"]) for r in reversed(rows) if r["freq"] is not None]


def _upsert(
    subject: Subject, tier: float, comparison: ComparisonResult, *,
    day: str, now: datetime, run: SignalRun,
) -> None:
    slug = _slug(subject, tier)
    namespace, key, value = subject
    stamp = now.isoformat(timespec="seconds")

    existing = db.query_one(
        "SELECT status, first_seen, last_seen, peak_freq FROM signals WHERE slug = ?", (slug,)
    )
    db.execute(
        "INSERT INTO signal_points (slug, day, count, total, freq) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(slug, day) DO UPDATE SET count = excluded.count, "
        "total = excluded.total, freq = excluded.freq",
        (slug, day, comparison.recent.count, comparison.recent.total, comparison.recent.value),
    )

    previous = TrendStatus(existing["status"]) if existing and existing["status"] else None
    age_days = 0
    if existing and existing["first_seen"]:
        try:
            age_days = (now - datetime.fromisoformat(existing["first_seen"])).days
        except ValueError:
            age_days = 0
    silent_days = 0
    if existing and comparison.recent.count == 0 and existing["last_seen"]:
        try:
            silent_days = (now - datetime.fromisoformat(existing["last_seen"])).days
        except ValueError:
            silent_days = 0

    decision = decide_status(
        current_status=previous,
        comparison=comparison,
        recent_frequencies=_series(slug),
        days_since_last_occurrence=silent_days,
        persistence_days=_persistence(slug),
        age_days=age_days,
    )

    peak = max(float(existing["peak_freq"] or 0.0) if existing else 0.0, comparison.recent.value)

    db.execute(
        """
        INSERT INTO signals (slug, name, category, namespace, key, value, tier, status,
                             confidence, recent_count, recent_total, recent_freq,
                             baseline_freq, lift, p_value, persistence,
                             first_seen, last_seen, peak_freq)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
            status = excluded.status, confidence = excluded.confidence,
            recent_count = excluded.recent_count, recent_total = excluded.recent_total,
            recent_freq = excluded.recent_freq, baseline_freq = excluded.baseline_freq,
            lift = excluded.lift, p_value = excluded.p_value,
            persistence = excluded.persistence, last_seen = excluded.last_seen,
            peak_freq = excluded.peak_freq
        """,
        (
            slug, _name(subject), TRACKED.get((namespace, key), "other"),
            namespace, key, value, tier, decision.status.value,
            comparison.confidence.value, comparison.recent.count, comparison.recent.total,
            comparison.recent.value, comparison.baseline.value, comparison.lift,
            comparison.p_value, _persistence(slug),
            existing["first_seen"] if existing else stamp, stamp, peak,
        ),
    )

    if existing is None:
        run.created += 1
    else:
        run.updated += 1

    if decision.changed:
        if decision.status is TrendStatus.RISING and comparison.is_meaningful:
            run.promoted.append(slug)
        elif decision.status in (TrendStatus.DECLINING, TrendStatus.DEAD):
            run.faded.append(slug)


def _decay_absent(
    tier: float, observed: set[Subject], recent_total: int,
    baseline_counts: dict[Subject, int], baseline_total: int,
    day: str, now: datetime, run: SignalRun,
) -> None:
    rows = db.query(
        "SELECT slug, namespace, key, value FROM signals WHERE tier = ? AND status != ?",
        (tier, TrendStatus.DEAD.value),
    )
    for row in rows:
        subject: Subject = (row["namespace"] or "", row["key"] or "", row["value"])
        if subject in observed:
            continue
        comparison = compare_proportions(
            0, recent_total, baseline_counts.get(subject, 0), baseline_total, persistence_days=0
        )
        _upsert(subject, tier, comparison, day=day, now=now, run=run)


def _enforce_cap() -> None:
    """Drop the weakest signals once past :data:`MAX_SIGNALS`.

    Dead-and-quiet first, then thin-sample ones. Anything currently rising
    with a real sample is never dropped by this — the cap exists to stop
    accumulation of noise, not to lose findings.
    """
    total = int(db.scalar("SELECT COUNT(*) FROM signals"))
    if total <= MAX_SIGNALS:
        return
    excess = total - MAX_SIGNALS
    db.execute(
        """
        DELETE FROM signals WHERE slug IN (
            SELECT slug FROM signals
             WHERE NOT (status = 'rising' AND recent_total >= ?)
             ORDER BY (status = 'dead') DESC, recent_count ASC, last_seen ASC
             LIMIT ?
        )
        """,
        (MIN_RECENT_SAMPLE, excess),
    )
    db.execute("DELETE FROM signal_points WHERE slug NOT IN (SELECT slug FROM signals)")
    log.info("signals_capped", dropped=excess, kept=MAX_SIGNALS)


# -----------------------------------------------------------------------------
# Reading
# -----------------------------------------------------------------------------


def meaningful(*, limit: int = 12, tier: float | None = None) -> list[dict[str, Any]]:
    """Signals that clear the sample bars — the only ones worth citing.

    Everything else stays queryable through :func:`listing` with
    ``include_thin=True``, labelled for what it is. The distinction is kept
    explicit rather than silently applying one bar or the other, after a
    real inconsistency where a raw count and a filtered list disagreed with
    nothing in either result explaining the gap.
    """
    clauses = ["recent_total >= ?", "recent_count >= ?", "status != 'dead'"]
    params: list[Any] = [MIN_RECENT_SAMPLE, MIN_OCCURRENCES]
    if tier is not None:
        clauses.append("tier = ?")
        params.append(tier)
    params.append(limit)
    rows = db.query(
        f"SELECT * FROM signals WHERE {' AND '.join(clauses)} "
        f"ORDER BY (status = 'rising') DESC, COALESCE(lift, 0) DESC LIMIT ?",
        params,
    )
    return [_row_to_dict(r) for r in rows]


def listing(
    *, status: str | None = None, limit: int = 25, include_thin: bool = False
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if not include_thin:
        clauses.extend(["recent_total >= ?", "recent_count >= ?"])
        params.extend([MIN_RECENT_SAMPLE, MIN_OCCURRENCES])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = db.query(
        f"SELECT * FROM signals {where} ORDER BY COALESCE(lift, 0) DESC, recent_freq DESC LIMIT ?",
        params,
    )
    return [_row_to_dict(r) for r in rows]


def get(slug: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM signals WHERE slug = ?", (slug,))
    if row is None:
        return None
    payload = _row_to_dict(row)
    payload["series"] = [
        dict(r)
        for r in db.query(
            "SELECT day, count, total, freq FROM signal_points WHERE slug = ? "
            "ORDER BY day DESC LIMIT 30",
            (slug,),
        )
    ]
    payload["slope"] = trend_slope(_series(slug))
    return payload


def counts() -> dict[str, int]:
    rows = db.query("SELECT status, COUNT(*) AS n FROM signals GROUP BY status")
    by_status = {r["status"]: int(r["n"]) for r in rows}
    by_status["meaningful"] = int(
        db.scalar(
            "SELECT COUNT(*) FROM signals WHERE recent_total >= ? AND recent_count >= ? "
            "AND status != 'dead'",
            (MIN_RECENT_SAMPLE, MIN_OCCURRENCES),
        )
    )
    return by_status


def _row_to_dict(row: Any) -> dict[str, Any]:
    payload = dict(row)
    payload["thin_sample"] = (payload.get("recent_total") or 0) < MIN_RECENT_SAMPLE
    return payload


def emerging_words(sightings: list[Sighting], *, min_count: int = 3, limit: int = 12) -> list[dict[str, Any]]:
    """Words recurring across winners right now, computed in memory only.

    This is what replaced per-word trend documents. It answers the same
    question — "is a new vocabulary appearing?" — for one run, feeds the
    digest, and is then thrown away. If a word matters it will show up again
    next cycle and eventually earn prose in a memory file, which is a far
    better record than 1,800 rows of words seen once.
    """
    from app.analysis.features import discover_ngrams

    names = [s.name or "" for s in sightings if s.name]
    if not names:
        return []
    # Already ordered most-frequent-first and filtered to >= min_count.
    found = discover_ngrams(names, min_count=min_count, top_k=limit)
    return [{"phrase": phrase, "count": count} for phrase, count in found[:limit]]
