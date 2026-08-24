"""app/trends/lifecycle.py — trend status transitions.

The central rule under test throughout: "a trend should not become RISING
simply because of one unusual day" (§25). Most of these tests exist to prove
that hysteresis and the slope-agreement requirement actually catch that case,
not just that the happy path works.
"""

from __future__ import annotations

from app.analysis.stats import compare_proportions
from app.db.enums import TrendStatus
from app.trends.lifecycle import DEATH_SILENCE_DAYS, decide_status, is_revival


def _comparison(recent_count, recent_total, baseline_count, baseline_total, persistence_days=0):
    return compare_proportions(
        recent_count, recent_total, baseline_count, baseline_total,
        persistence_days=persistence_days,
    )


class TestDecideStatusNew:
    def test_no_previous_status_is_new(self):
        comparison = _comparison(10, 30, 10, 60)
        decision = decide_status(
            current_status=None, comparison=comparison,
            recent_frequencies=[0.3, 0.32, 0.33], days_since_last_occurrence=0,
            persistence_days=1, age_days=1,
        )
        assert decision.status is TrendStatus.NEW
        assert decision.changed is True

    def test_new_stays_new_until_enough_history(self):
        comparison = _comparison(60, 100, 20, 100, persistence_days=1)
        decision = decide_status(
            current_status=TrendStatus.NEW, comparison=comparison,
            recent_frequencies=[0.6], days_since_last_occurrence=0,
            persistence_days=1, age_days=1,
        )
        assert decision.status is TrendStatus.NEW
        assert decision.changed is False


class TestDecideStatusDeath:
    def test_long_silence_is_dead_regardless_of_prior_status(self):
        comparison = _comparison(60, 100, 20, 100, persistence_days=10)
        decision = decide_status(
            current_status=TrendStatus.RISING, comparison=comparison,
            recent_frequencies=[0.6, 0.6, 0.6], days_since_last_occurrence=DEATH_SILENCE_DAYS,
            persistence_days=10, age_days=40,
        )
        assert decision.status is TrendStatus.DEAD

    def test_declining_trend_that_falls_below_floor_dies(self):
        comparison = _comparison(1, 200, 40, 200, persistence_days=10)
        decision = decide_status(
            current_status=TrendStatus.DECLINING, comparison=comparison,
            recent_frequencies=[0.05, 0.03, 0.01], days_since_last_occurrence=1,
            persistence_days=10, age_days=DEATH_SILENCE_DAYS + 5,
        )
        assert decision.status is TrendStatus.DEAD

    def test_new_trend_with_low_frequency_does_not_die_from_the_floor_rule(self):
        # The floor-death rule only applies to DECLINING/STABLE — a brand new
        # trend at low frequency should not be killed by the same check.
        comparison = _comparison(1, 200, 40, 200, persistence_days=1)
        decision = decide_status(
            current_status=TrendStatus.NEW, comparison=comparison,
            recent_frequencies=[0.005], days_since_last_occurrence=1,
            persistence_days=1, age_days=2,
        )
        assert decision.status is not TrendStatus.DEAD


class TestDecideStatusDirection:
    def test_quiet_tuesday_does_not_promote_to_rising(self):
        # The exact case §25 exists to guard against: a big-looking delta
        # from a sample too small to support it must not become RISING.
        # persistence_days=3 clears the separate "still too new" gate, so
        # this specifically exercises the OBSERVATION-maturity guard below it.
        comparison = _comparison(6, 8, 10, 60, persistence_days=3)
        decision = decide_status(
            current_status=TrendStatus.NEW, comparison=comparison,
            recent_frequencies=[0.75], days_since_last_occurrence=0,
            persistence_days=3, age_days=5,
        )
        assert decision.status is not TrendStatus.RISING
        assert "insufficient" in decision.reason

    def test_large_supported_increase_with_agreeing_slope_becomes_rising(self):
        comparison = _comparison(60, 100, 20, 100, persistence_days=5)
        decision = decide_status(
            current_status=TrendStatus.STABLE, comparison=comparison,
            recent_frequencies=[0.20, 0.35, 0.50, 0.60], days_since_last_occurrence=0,
            persistence_days=5, age_days=30,
        )
        assert decision.status is TrendStatus.RISING

    def test_large_delta_with_contradicting_slope_does_not_enter_rising(self):
        # The delta says "up," but the fitted slope over the actual series
        # says down — agreement is required to enter, not just the endpoint.
        comparison = _comparison(60, 100, 20, 100, persistence_days=5)
        decision = decide_status(
            current_status=TrendStatus.STABLE, comparison=comparison,
            recent_frequencies=[0.80, 0.70, 0.65, 0.60], days_since_last_occurrence=0,
            persistence_days=5, age_days=30,
        )
        assert decision.status is not TrendStatus.RISING

    def test_established_rising_trend_sustains_on_a_smaller_change(self):
        # Hysteresis: SUSTAIN_THRESHOLD is half of ENTER_THRESHOLD, so a
        # trend already RISING should not be demoted by one quieter day.
        comparison = _comparison(24, 100, 20, 100, persistence_days=8)  # change=0.04, > sustain, < enter
        decision = decide_status(
            current_status=TrendStatus.RISING, comparison=comparison,
            recent_frequencies=[0.22, 0.23, 0.24], days_since_last_occurrence=0,
            persistence_days=8, age_days=20,
        )
        assert decision.status is TrendStatus.RISING
        assert decision.changed is False

    def test_entering_declining(self):
        comparison = _comparison(5, 100, 30, 100, persistence_days=5)
        decision = decide_status(
            current_status=TrendStatus.STABLE, comparison=comparison,
            recent_frequencies=[0.30, 0.20, 0.10, 0.05], days_since_last_occurrence=0,
            persistence_days=5, age_days=30,
        )
        assert decision.status is TrendStatus.DECLINING

    def test_flat_low_variance_series_is_stable(self):
        comparison = _comparison(21, 100, 20, 100, persistence_days=10)
        decision = decide_status(
            current_status=TrendStatus.STABLE, comparison=comparison,
            recent_frequencies=[0.20, 0.21, 0.20, 0.21], days_since_last_occurrence=0,
            persistence_days=10, age_days=30,
        )
        assert decision.status is TrendStatus.STABLE

    def test_erratic_series_holds_previous_state_instead_of_stable(self):
        comparison = _comparison(21, 100, 20, 100, persistence_days=10)
        decision = decide_status(
            current_status=TrendStatus.RISING, comparison=comparison,
            recent_frequencies=[0.05, 0.45, 0.02, 0.38], days_since_last_occurrence=0,
            persistence_days=10, age_days=30,
        )
        assert decision.status is TrendStatus.RISING  # held, not reclassified as STABLE
        assert "erratic" in decision.reason


class TestIsRevival:
    def test_dead_to_new_is_a_revival(self):
        assert is_revival(TrendStatus.DEAD, TrendStatus.NEW) is True

    def test_dead_to_rising_is_a_revival(self):
        assert is_revival(TrendStatus.DEAD, TrendStatus.RISING) is True

    def test_dead_to_dead_is_not_a_revival(self):
        assert is_revival(TrendStatus.DEAD, TrendStatus.DEAD) is False

    def test_dead_to_declining_is_not_a_revival(self):
        # Coming back only to immediately decline isn't the "it's back" signal.
        assert is_revival(TrendStatus.DEAD, TrendStatus.DECLINING) is False

    def test_non_dead_previous_is_never_a_revival(self):
        assert is_revival(TrendStatus.STABLE, TrendStatus.RISING) is False
        assert is_revival(None, TrendStatus.NEW) is False
