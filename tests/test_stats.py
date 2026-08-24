"""app/analysis/stats.py — the trend engine's statistical core.

These are the "picking up the unfinished work" candidates the README
pointed at first: pure functions, no database or network, and where a
subtle error would silently misclassify a trend for everyone.
"""

from __future__ import annotations

from app.analysis.stats import (
    MIN_BASELINE_SAMPLE,
    MIN_MEANINGFUL_CHANGE,
    MIN_OCCURRENCES,
    MIN_RECENT_SAMPLE,
    Proportion,
    cohens_h,
    coefficient_of_variation,
    compare_proportions,
    trend_slope,
    two_proportion_z_test,
)
from app.db.enums import Confidence, TrendMaturity


class TestProportion:
    def test_value_is_count_over_total(self):
        assert Proportion(3, 12).value == 0.25

    def test_value_is_zero_when_total_is_zero(self):
        assert Proportion(0, 0).value == 0.0

    def test_is_usable_requires_nonzero_total(self):
        assert Proportion(0, 0).is_usable is False
        assert Proportion(0, 5).is_usable is True

    def test_wilson_interval_stays_within_unit_range(self):
        # The whole reason Wilson was chosen over the normal approximation:
        # 2/7 produces a negative lower bound under the normal interval.
        low, high = Proportion(2, 7).wilson_interval()
        assert 0.0 <= low <= high <= 1.0

    def test_wilson_interval_widens_with_smaller_sample(self):
        wide_low, wide_high = Proportion(2, 7).wilson_interval()
        narrow_low, narrow_high = Proportion(200, 700).wilson_interval()
        assert (wide_high - wide_low) > (narrow_high - narrow_low)

    def test_wilson_interval_on_empty_sample(self):
        assert Proportion(0, 0).wilson_interval() == (0.0, 0.0)


class TestTwoProportionZTest:
    def test_returns_none_for_empty_group(self):
        assert two_proportion_z_test(5, 0, 5, 10) is None
        assert two_proportion_z_test(5, 10, 5, 0) is None

    def test_identical_proportions_are_not_significant(self):
        p = two_proportion_z_test(50, 100, 50, 100)
        assert p is not None
        assert p > 0.9  # z is ~0, p should be ~1

    def test_large_real_difference_is_significant(self):
        # 80/100 vs 20/100 — an enormous, unmistakable difference.
        p = two_proportion_z_test(80, 100, 20, 100)
        assert p is not None
        assert p < 0.001

    def test_all_zero_or_all_one_pooled_proportion_is_undefined(self):
        assert two_proportion_z_test(0, 50, 0, 50) is None
        assert two_proportion_z_test(50, 50, 50, 50) is None


class TestCohensH:
    def test_equal_proportions_have_zero_effect_size(self):
        assert abs(cohens_h(0.3, 0.3)) < 1e-9

    def test_is_antisymmetric(self):
        h_ab = cohens_h(0.7, 0.3)
        h_ba = cohens_h(0.3, 0.7)
        assert abs(h_ab + h_ba) < 1e-9

    def test_clamps_out_of_range_inputs(self):
        # Defensive: a caller passing a bad proportion must not crash asin().
        cohens_h(-0.5, 1.5)


class TestCoefficientOfVariation:
    def test_needs_at_least_two_values(self):
        assert coefficient_of_variation([0.1]) is None
        assert coefficient_of_variation([]) is None

    def test_zero_mean_is_undefined(self):
        assert coefficient_of_variation([0.0, 0.0, 0.0]) is None

    def test_constant_series_has_zero_variation(self):
        assert coefficient_of_variation([0.2, 0.2, 0.2]) < 1e-9

    def test_erratic_series_has_higher_cv_than_stable_one(self):
        stable = coefficient_of_variation([0.20, 0.21, 0.19, 0.20])
        erratic = coefficient_of_variation([0.05, 0.40, 0.02, 0.35])
        assert erratic > stable


class TestTrendSlope:
    def test_needs_at_least_three_points(self):
        assert trend_slope([0.1, 0.2]) is None

    def test_increasing_series_has_positive_slope(self):
        assert trend_slope([0.1, 0.2, 0.3, 0.4]) > 0

    def test_decreasing_series_has_negative_slope(self):
        assert trend_slope([0.4, 0.3, 0.2, 0.1]) < 0

    def test_flat_series_has_zero_slope(self):
        assert trend_slope([0.2, 0.2, 0.2, 0.2]) == 0.0

    def test_two_outlier_endpoints_dont_dominate_an_otherwise_flat_series(self):
        # The documented reason least-squares was chosen over first-vs-last.
        endpoints_only_up = trend_slope([0.5, 0.2, 0.2, 0.2, 0.2, 0.9])
        clearly_up = trend_slope([0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
        assert endpoints_only_up < clearly_up


class TestCompareProportions:
    def test_below_minimum_recent_sample_is_blocked(self):
        result = compare_proportions(3, 5, 30, 100)
        assert result.maturity is TrendMaturity.OBSERVATION
        assert result.confidence is Confidence.LOW
        assert result.p_value is None
        assert any("Recent sample" in c for c in result.caveats)

    def test_below_minimum_occurrences_is_blocked_even_with_large_total(self):
        # Small recent_count but a padded total — the count floor exists
        # specifically so this can't sneak past the sample-size floor alone.
        result = compare_proportions(2, MIN_RECENT_SAMPLE, 30, MIN_BASELINE_SAMPLE)
        assert result.maturity is TrendMaturity.OBSERVATION
        assert any("occurrences" in c for c in result.caveats)

    def test_below_minimum_baseline_sample_is_blocked(self):
        result = compare_proportions(10, MIN_RECENT_SAMPLE, 5, 10)
        assert result.maturity is TrendMaturity.OBSERVATION
        assert any("Baseline sample" in c for c in result.caveats)

    def test_still_records_an_observation_when_blocked(self):
        # §26: refusing to promote is not the same as refusing to record.
        result = compare_proportions(1, 2, 1, 2)
        assert result.recent.count == 1
        assert result.recent.total == 2

    def test_large_significant_difference_promotes_to_candidate(self):
        result = compare_proportions(
            recent_count=60, recent_total=100, baseline_count=20, baseline_total=100
        )
        assert result.maturity is TrendMaturity.CANDIDATE
        assert result.confidence is Confidence.MEDIUM
        assert result.p_value is not None and result.p_value < 0.05

    def test_promotes_to_validated_only_with_persistence_and_large_effect(self):
        result = compare_proportions(
            recent_count=60, recent_total=100, baseline_count=20, baseline_total=100,
            persistence_days=5,
        )
        assert result.maturity is TrendMaturity.VALIDATED
        assert result.confidence is Confidence.HIGH

    def test_significant_but_insufficient_persistence_stays_candidate(self):
        result = compare_proportions(
            recent_count=60, recent_total=100, baseline_count=20, baseline_total=100,
            persistence_days=1,
        )
        assert result.maturity is TrendMaturity.CANDIDATE
        assert any("needed before validation" in c for c in result.caveats)

    def test_tiny_change_caps_confidence_even_if_technically_significant(self):
        # A large-n comparison where the delta itself is below the
        # meaningful-change floor should not read as a high-confidence find.
        result = compare_proportions(
            recent_count=2100, recent_total=10000,
            baseline_count=2000, baseline_total=10000,
            persistence_days=10,
        )
        assert abs(result.change) < MIN_MEANINGFUL_CHANGE
        assert result.confidence is not Confidence.HIGH

    def test_is_meaningful_requires_both_promotion_and_size(self):
        blocked = compare_proportions(1, 2, 1, 2)
        assert blocked.is_meaningful is False

        small_but_significant = compare_proportions(
            recent_count=2100, recent_total=10000,
            baseline_count=2000, baseline_total=10000,
            persistence_days=10,
        )
        assert small_but_significant.is_meaningful is False

        real_finding = compare_proportions(
            recent_count=60, recent_total=100, baseline_count=20, baseline_total=100,
            persistence_days=5,
        )
        assert real_finding.is_meaningful is True

    def test_as_dict_never_omits_the_denominator(self):
        result = compare_proportions(6, 30, 20, 100)
        payload = result.as_dict()
        assert payload["recent_count"] == 6
        assert payload["recent_total"] == 30
        assert "recent_frequency" in payload
