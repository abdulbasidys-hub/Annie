"""Trend lifecycle transitions (§25).

Pure functions with no database or network access, so the rules that decide
whether something is RISING are testable in isolation and reviewable without
reading query code.

The central rule from §25 — *a trend should not become RISING simply because of
one unusual day* — is implemented as hysteresis. Entering a state requires more
evidence than staying in it. Without that, a trend hovering near a threshold
flips status nightly, and a status that changes every day tells the operator
nothing while looking like constant discovery.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.analysis.stats import (
    MIN_MEANINGFUL_CHANGE,
    ComparisonResult,
    coefficient_of_variation,
    trend_slope,
)
from app.db.enums import TrendMaturity, TrendStatus

#: Change needed to *enter* RISING or DECLINING.
ENTER_THRESHOLD = MIN_MEANINGFUL_CHANGE  # 3 percentage points

#: Change needed to *remain* there. Lower, so a trend that has established
#: itself is not demoted by a single quiet day.
SUSTAIN_THRESHOLD = MIN_MEANINGFUL_CHANGE / 2

#: Consecutive observations pointing the same way before direction changes.
DIRECTION_CONFIRMATION_DAYS = 2

#: A trend with no occurrences for this long is DEAD.
DEATH_SILENCE_DAYS = 21

#: Frequency floor below which a previously meaningful trend is DEAD rather
#: than merely declining.
DEATH_FREQUENCY = 0.01

#: Above this coefficient of variation, a series is too erratic to call STABLE.
STABILITY_CV_CEILING = 0.35


@dataclass(slots=True)
class LifecycleDecision:
    status: TrendStatus
    reason: str
    changed: bool


def decide_status(
    *,
    current_status: TrendStatus | None,
    comparison: ComparisonResult,
    recent_frequencies: list[float],
    days_since_last_occurrence: int,
    persistence_days: int,
    age_days: int,
) -> LifecycleDecision:
    """Determine a trend's lifecycle state.

    ``recent_frequencies`` is the ordered daily series, oldest first. It is
    required rather than optional because direction is taken from the fitted
    slope over the series, not from the single recent-vs-baseline delta —
    §25's whole concern is that one day should not decide this.
    """
    previous = current_status

    # -- Death ----------------------------------------------------------------
    # Checked first: a trend with no recent occurrences is dead regardless of
    # what the frequency comparison says about the window it last appeared in.
    if days_since_last_occurrence >= DEATH_SILENCE_DAYS:
        return _decide(
            previous,
            TrendStatus.DEAD,
            f"No occurrences in {days_since_last_occurrence} days.",
        )

    if (
        previous in (TrendStatus.DECLINING, TrendStatus.STABLE)
        and comparison.recent.value < DEATH_FREQUENCY
        and age_days > DEATH_SILENCE_DAYS
    ):
        return _decide(
            previous,
            TrendStatus.DEAD,
            f"Frequency fell to {comparison.recent.value:.1%}, "
            f"below the {DEATH_FREQUENCY:.0%} floor.",
        )

    # -- New ------------------------------------------------------------------
    # A trend stays NEW until it has enough history to have a direction at all.
    if previous is None or (
        previous is TrendStatus.NEW and persistence_days < DIRECTION_CONFIRMATION_DAYS
    ):
        return _decide(
            previous,
            TrendStatus.NEW,
            f"Detected {persistence_days} day(s) ago; not enough history for a direction.",
        )

    # -- Direction ------------------------------------------------------------
    slope = trend_slope(recent_frequencies)
    change = comparison.change

    # Entering a directional state needs both a large enough delta against
    # baseline AND a slope agreeing with it. Requiring agreement is what
    # rejects the single-spike case: one high day lifts the delta but barely
    # moves a slope fitted across the window.
    entering_rise = change >= ENTER_THRESHOLD and (slope is None or slope > 0)
    entering_fall = change <= -ENTER_THRESHOLD and (slope is None or slope < 0)

    sustaining_rise = (
        previous is TrendStatus.RISING and change >= SUSTAIN_THRESHOLD
    )
    sustaining_fall = (
        previous is TrendStatus.DECLINING and change <= -SUSTAIN_THRESHOLD
    )

    if entering_rise or sustaining_rise:
        if comparison.maturity is TrendMaturity.OBSERVATION and not sustaining_rise:
            # Big move, but the sample cannot support the claim. Stay put and
            # say why — this is the branch that catches the quiet-Tuesday case.
            return _decide(
                previous,
                previous or TrendStatus.NEW,
                f"Frequency up {change:+.1%} but sample is insufficient: "
                + "; ".join(comparison.caveats),
            )
        return _decide(
            previous,
            TrendStatus.RISING,
            f"Frequency {comparison.recent.value:.1%} vs baseline "
            f"{comparison.baseline.value:.1%} ({change:+.1%}), "
            f"sustained over {persistence_days} day(s).",
        )

    if entering_fall or sustaining_fall:
        return _decide(
            previous,
            TrendStatus.DECLINING,
            f"Frequency {comparison.recent.value:.1%} vs baseline "
            f"{comparison.baseline.value:.1%} ({change:+.1%}).",
        )

    # -- Stable ---------------------------------------------------------------
    cv = coefficient_of_variation(recent_frequencies)
    if cv is not None and cv > STABILITY_CV_CEILING:
        return _decide(
            previous,
            previous or TrendStatus.NEW,
            f"Series too erratic to call stable (CV {cv:.2f}); holding previous state.",
        )

    return _decide(
        previous,
        TrendStatus.STABLE,
        f"Frequency holding near {comparison.recent.value:.1%} "
        f"(baseline {comparison.baseline.value:.1%}).",
    )


def is_revival(previous: TrendStatus | None, new: TrendStatus) -> bool:
    """A DEAD trend coming back — §45 treats this as its own anomaly."""
    return previous is TrendStatus.DEAD and new in (
        TrendStatus.NEW,
        TrendStatus.RISING,
        TrendStatus.STABLE,
    )


def _decide(
    previous: TrendStatus | None, new: TrendStatus, reason: str
) -> LifecycleDecision:
    return LifecycleDecision(status=new, reason=reason, changed=previous != new)
