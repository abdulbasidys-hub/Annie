"""Statistical primitives for the trend engine (§26).

Everything the system claims about a pattern passes through this module. Its
job is to make §26's distinction enforceable rather than aspirational:

    Observation  — something happened.
    Candidate    — enough evidence exists to investigate.
    Validated    — the pattern has meaningful persistent evidence.

The recurring failure mode in this domain is a tiny denominator. Nine of the
eleven successful tokens on a quiet Tuesday were animal-themed: 82%, up from a
31% baseline, and it means nothing. Guarding against that is why
:func:`compare_proportions` refuses to report significance below a minimum
sample and why confidence intervals are Wilson rather than normal-approximation
— the normal approximation is badly wrong at exactly the small counts this
system sees most often.

No function here returns a bare number. Each returns a result object carrying
the sample sizes and caveats, so a caller cannot render "23%, RISING" without
also having the denominator in hand.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

from app.db.enums import Confidence, TrendMaturity

# -----------------------------------------------------------------------------
# Thresholds
# -----------------------------------------------------------------------------
# Defaults, overridable per deployment through the settings table. They encode
# a deliberately conservative stance: the cost of a missed trend is that it is
# found next week, while the cost of a false trend is that research time and
# money are spent chasing noise, and that Annie's credibility drops.

#: Below this many qualifying tokens in the recent window, no significance is
#: computed at all. The observation is still recorded.
MIN_RECENT_SAMPLE = 20

#: Below this many occurrences of the characteristic itself, a pattern cannot
#: leave OBSERVATION however extreme the percentage looks.
MIN_OCCURRENCES = 5

#: Baseline needs to be wide enough to mean something.
MIN_BASELINE_SAMPLE = 50

#: Two-sided significance level for promotion to CANDIDATE.
ALPHA_CANDIDATE = 0.05

#: Stricter level for VALIDATED, combined with a persistence requirement.
ALPHA_VALIDATED = 0.01

#: Consecutive days a pattern must hold before it can be VALIDATED (§25 — "a
#: trend should not become RISING simply because of one unusual day").
MIN_PERSISTENCE_DAYS = 5

#: Minimum absolute change in frequency worth reporting. A shift from 20.1% to
#: 20.4% can be statistically significant with enough data and still be of no
#: research interest.
MIN_MEANINGFUL_CHANGE = 0.03


@dataclass(slots=True)
class Proportion:
    """A count over a total, with the interval that count actually supports."""

    count: int
    total: int

    @property
    def value(self) -> float:
        return self.count / self.total if self.total else 0.0

    @property
    def is_usable(self) -> bool:
        return self.total > 0

    def wilson_interval(self, z: float = 1.96) -> tuple[float, float]:
        """Wilson score interval.

        Chosen over the normal approximation because it stays inside [0, 1] and
        remains sane at small counts — the regime this system operates in
        almost all the time. The normal interval on 2/7 produces a lower bound
        below zero, which would render as a nonsense error bar.
        """
        n = self.total
        if n == 0:
            return (0.0, 0.0)
        p = self.value
        denom = 1 + z**2 / n
        centre = (p + z**2 / (2 * n)) / denom
        margin = (z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))) / denom
        return (max(0.0, centre - margin), min(1.0, centre + margin))


@dataclass(slots=True)
class ComparisonResult:
    """The full outcome of comparing a recent window against a baseline."""

    recent: Proportion
    baseline: Proportion

    change: float
    relative_change: float | None
    lift: float | None

    p_value: float | None
    effect_size: float | None  # Cohen's h
    ci_low: float
    ci_high: float

    maturity: TrendMaturity
    confidence: Confidence
    caveats: list[str] = field(default_factory=list)

    @property
    def is_meaningful(self) -> bool:
        """Statistically supported *and* large enough to care about."""
        return (
            self.maturity is not TrendMaturity.OBSERVATION
            and abs(self.change) >= MIN_MEANINGFUL_CHANGE
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "recent_count": self.recent.count,
            "recent_total": self.recent.total,
            "recent_frequency": round(self.recent.value, 6),
            "baseline_count": self.baseline.count,
            "baseline_total": self.baseline.total,
            "baseline_frequency": round(self.baseline.value, 6),
            "change": round(self.change, 6),
            "relative_change": (
                round(self.relative_change, 6)
                if self.relative_change is not None
                else None
            ),
            "lift": round(self.lift, 6) if self.lift is not None else None,
            "p_value": round(self.p_value, 6) if self.p_value is not None else None,
            "effect_size": (
                round(self.effect_size, 6) if self.effect_size is not None else None
            ),
            "ci_low": round(self.ci_low, 6),
            "ci_high": round(self.ci_high, 6),
            "maturity": self.maturity.value,
            "confidence": self.confidence.value,
            "caveats": list(self.caveats),
        }


def compare_proportions(
    recent_count: int,
    recent_total: int,
    baseline_count: int,
    baseline_total: int,
    *,
    persistence_days: int = 0,
    min_recent_sample: int = MIN_RECENT_SAMPLE,
    min_occurrences: int = MIN_OCCURRENCES,
    min_baseline_sample: int = MIN_BASELINE_SAMPLE,
) -> ComparisonResult:
    """Compare a characteristic's recent frequency against its baseline.

    Returns a result in every case, including when the sample is far too small
    — with ``maturity=OBSERVATION`` and an explicit caveat. Refusing to return
    anything would hide small-sample patterns entirely, and §26 wants
    observations recorded; it only forbids *promoting* them.
    """
    recent = Proportion(recent_count, recent_total)
    baseline = Proportion(baseline_count, baseline_total)
    caveats: list[str] = []

    change = recent.value - baseline.value
    relative_change = (change / baseline.value) if baseline.value > 0 else None
    lift = (recent.value / baseline.value) if baseline.value > 0 else None
    ci_low, ci_high = recent.wilson_interval()

    # -- Gating ---------------------------------------------------------------
    # These checks run before any test. A p-value computed on n=6 is not a weak
    # signal, it is a meaningless one, and reporting it invites it to be quoted.

    blocked = False
    if recent_total < min_recent_sample:
        caveats.append(
            f"Recent sample of {recent_total} is below the minimum of "
            f"{min_recent_sample}; no significance computed."
        )
        blocked = True
    if recent_count < min_occurrences:
        caveats.append(
            f"Only {recent_count} occurrences observed (minimum {min_occurrences})."
        )
        blocked = True
    if baseline_total < min_baseline_sample:
        caveats.append(
            f"Baseline sample of {baseline_total} is below the minimum of "
            f"{min_baseline_sample}; comparison is indicative only."
        )
        blocked = True

    if blocked:
        return ComparisonResult(
            recent=recent,
            baseline=baseline,
            change=change,
            relative_change=relative_change,
            lift=lift,
            p_value=None,
            effect_size=None,
            ci_low=ci_low,
            ci_high=ci_high,
            maturity=TrendMaturity.OBSERVATION,
            confidence=Confidence.LOW,
            caveats=caveats,
        )

    p_value = two_proportion_z_test(
        recent_count, recent_total, baseline_count, baseline_total
    )
    effect = cohens_h(recent.value, baseline.value)

    # -- Promotion ------------------------------------------------------------
    maturity = TrendMaturity.OBSERVATION
    confidence = Confidence.LOW

    if p_value is not None and p_value < ALPHA_CANDIDATE:
        maturity = TrendMaturity.CANDIDATE
        confidence = Confidence.MEDIUM

        if (
            p_value < ALPHA_VALIDATED
            and persistence_days >= MIN_PERSISTENCE_DAYS
            and abs(effect) >= 0.2  # conventional small-effect floor
        ):
            maturity = TrendMaturity.VALIDATED
            confidence = Confidence.HIGH

    if abs(change) < MIN_MEANINGFUL_CHANGE:
        caveats.append(
            f"Absolute change of {change:+.1%} is below the "
            f"{MIN_MEANINGFUL_CHANGE:.0%} reporting floor."
        )
        # Statistically real but too small to act on. Confidence is capped so
        # it does not appear in the dashboard's high-confidence list.
        if confidence is Confidence.HIGH:
            confidence = Confidence.MEDIUM

    if maturity is TrendMaturity.CANDIDATE and persistence_days < MIN_PERSISTENCE_DAYS:
        caveats.append(
            f"Observed on {persistence_days} day(s); "
            f"{MIN_PERSISTENCE_DAYS} needed before validation."
        )

    return ComparisonResult(
        recent=recent,
        baseline=baseline,
        change=change,
        relative_change=relative_change,
        lift=lift,
        p_value=p_value,
        effect_size=effect,
        ci_low=ci_low,
        ci_high=ci_high,
        maturity=maturity,
        confidence=confidence,
        caveats=caveats,
    )


def two_proportion_z_test(
    count_a: int, total_a: int, count_b: int, total_b: int
) -> float | None:
    """Two-sided z-test for a difference between two proportions.

    Implemented directly rather than pulled from SciPy so the pooled-variance
    assumption is visible at the point of use, and so the module has no hard
    dependency for its most-called function.
    """
    if total_a == 0 or total_b == 0:
        return None

    p_a = count_a / total_a
    p_b = count_b / total_b
    pooled = (count_a + count_b) / (total_a + total_b)

    if pooled in (0.0, 1.0):
        return None  # no variance; the test is undefined, not "significant"

    se = math.sqrt(pooled * (1 - pooled) * (1 / total_a + 1 / total_b))
    if se == 0:
        return None

    z = (p_a - p_b) / se
    return 2 * (1 - _normal_cdf(abs(z)))


def cohens_h(p_a: float, p_b: float) -> float:
    """Effect size for a difference between proportions.

    Reported alongside the p-value because significance answers "is this
    real?" while effect size answers "is it big?" — and §26 asks for both.
    With a large historical dataset, trivial differences become significant;
    without an effect size, they would all look like findings.
    """
    return 2 * math.asin(math.sqrt(max(0.0, min(1.0, p_a)))) - 2 * math.asin(
        math.sqrt(max(0.0, min(1.0, p_b)))
    )


def _normal_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def coefficient_of_variation(values: list[float]) -> float | None:
    """Relative dispersion of a trend's daily frequencies.

    Used to separate a genuinely stable pattern from one whose average happens
    to sit still while swinging wildly day to day. §26 lists variance as an
    input; this is the scale-free form of it, so a 2%-frequency trend and a
    40%-frequency trend can be compared.
    """
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    if mean == 0:
        return None
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance) / mean


def trend_slope(values: list[float]) -> float | None:
    """Least-squares slope over an evenly spaced series.

    Direction is taken from the fitted slope rather than from comparing the
    first and last points, which would let two outliers at the ends dictate the
    verdict for an otherwise flat series.
    """
    n = len(values)
    if n < 3:
        return None
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values)) / denom


def to_decimal(value: float | None, places: int = 6) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(round(value, places)))
