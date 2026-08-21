"""The Trend Engine (§24-§28).

Compares recent windows against baselines and maintains persistent Trend
Memory in Firestore. The statistical core — :mod:`app.analysis.stats` and
:mod:`app.trends.lifecycle` — is completely unchanged from the original
Postgres design; only how cohorts and feature counts are *fetched* changed.
See :mod:`app.db.repo`'s module docstring for what that trade costs (more
reads, no joins, no server-side GROUP BY) and why it is acceptable at this
project's scale.

One structural decision carried over unchanged: trends are keyed by
``(namespace, key, value, cohort_threshold)``. The same characteristic
measured over $100k+ tokens and over $1M+ tokens is two documents, never one
— §27 forbids treating a $100k finding as a $1M finding.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import structlog

from app.analysis.stats import ComparisonResult, compare_proportions
from app.db.enums import Confidence, TrendCategory, TrendMaturity, TrendStatus
from app.db.models.intelligence import Trend, TrendHistory, TrendObservation
from app.db.models.tokens import Token
from app.db.repo import FirestoreRepo
from app.trends.lifecycle import decide_status, is_revival

log = structlog.get_logger(__name__)

#: Success tiers from §4. Configurable at runtime via the settings collection;
#: these are the defaults the engine starts with.
DEFAULT_TIERS: tuple[Decimal, ...] = (
    Decimal("100000"),
    Decimal("250000"),
    Decimal("500000"),
    Decimal("1000000"),
)

#: Comparison windows from §8.
DEFAULT_RECENT_DAYS = 7
DEFAULT_BASELINE_DAYS = 90

#: Feature namespaces that produce trends. `token.theme` is the rollup; the
#: per-namespace word features are counted too, but only `key` values in this
#: map become trend rows, to stop every stopword becoming a "trend".
TRENDABLE: dict[tuple[str, str], TrendCategory] = {
    ("token", "theme"): TrendCategory.NARRATIVE,
    ("name", "theme"): TrendCategory.NAME,
    ("name", "word"): TrendCategory.NAME,
    ("ticker", "shape"): TrendCategory.TICKER,
    ("ticker", "theme"): TrendCategory.TICKER,
    ("description", "theme"): TrendCategory.DESCRIPTION,
    ("description", "present"): TrendCategory.DESCRIPTION,
    ("image", "category"): TrendCategory.IMAGE,
    ("image", "subject"): TrendCategory.IMAGE,
    ("image", "style"): TrendCategory.IMAGE,
    ("launchpad", "slug"): TrendCategory.LAUNCHPAD,
    ("creator", "is_repeat_winner"): TrendCategory.CREATOR,
    ("migration", "destination"): TrendCategory.MIGRATION,
    ("social", "has_twitter"): TrendCategory.SOCIAL,
}

Subject = tuple[str, str, str | None]


@dataclass(slots=True)
class TrendRun:
    """Summary of one engine pass, written to the daily report."""

    started_at: datetime
    finished_at: datetime | None = None
    cohorts_evaluated: int = 0
    features_evaluated: int = 0
    trends_created: int = 0
    trends_updated: int = 0
    status_changes: int = 0
    revivals: int = 0
    skipped_windows: list[str] = field(default_factory=list)


class TrendEngine:
    def __init__(
        self,
        repo: FirestoreRepo,
        *,
        tiers: tuple[Decimal, ...] = DEFAULT_TIERS,
        recent_days: int = DEFAULT_RECENT_DAYS,
        baseline_days: int = DEFAULT_BASELINE_DAYS,
    ) -> None:
        self.repo = repo
        self.tiers = tiers
        self.recent_days = recent_days
        self.baseline_days = baseline_days

    # -- entry point ------------------------------------------------------

    async def run(self, now: datetime | None = None) -> TrendRun:
        now = now or datetime.now(timezone.utc)
        run = TrendRun(started_at=now)

        recent_start = now - timedelta(days=self.recent_days)
        baseline_start = now - timedelta(days=self.baseline_days)

        if not await self._window_is_usable(recent_start, now):
            # §20's data-quality gate. A window with poor enrichment coverage
            # produces frequencies over an unrepresentative denominator, which
            # looks exactly like a real shift. Skipping is the honest response.
            run.skipped_windows.append(
                f"{recent_start.date()}..{now.date()} (insufficient coverage)"
            )
            run.finished_at = datetime.now(timezone.utc)
            log.warning("trend_run_skipped", reason="coverage", window_start=str(recent_start))
            return run

        for tier in self.tiers:
            run.cohorts_evaluated += 1
            await self._evaluate_cohort(
                tier=tier, recent_start=recent_start, baseline_start=baseline_start,
                now=now, run=run,
            )

        run.finished_at = datetime.now(timezone.utc)
        log.info(
            "trend_run_complete",
            cohorts=run.cohorts_evaluated,
            created=run.trends_created,
            updated=run.trends_updated,
            status_changes=run.status_changes,
        )
        return run

    # -- cohort evaluation --------------------------------------------------

    async def _evaluate_cohort(
        self, *, tier: Decimal, recent_start: datetime, baseline_start: datetime,
        now: datetime, run: TrendRun,
    ) -> None:
        recent_tokens = await self.repo.qualified_tokens_in_window(
            start=recent_start, end=now, min_peak=tier
        )
        baseline_tokens = await self.repo.qualified_tokens_in_window(
            start=baseline_start, end=recent_start, min_peak=tier
        )
        recent_total = len(recent_tokens)
        baseline_total = len(baseline_tokens)

        if recent_total == 0:
            log.info("cohort_empty", tier=str(tier))
            return

        recent_counts = await self._feature_counts(recent_tokens)
        baseline_counts = await self._feature_counts(baseline_tokens)

        for subject, recent_count in recent_counts.items():
            namespace, key, value = subject
            category = TRENDABLE.get((namespace, key))
            if category is None:
                continue

            run.features_evaluated += 1
            baseline_count = baseline_counts.get(subject, 0)

            trend = await self._get_or_create_trend(
                namespace=namespace, key=key, value=value, tier=tier,
                category=category, now=now, run=run,
            )

            persistence = await self._persistence_days(trend.slug)
            comparison = compare_proportions(
                recent_count, recent_total, baseline_count, baseline_total,
                persistence_days=persistence,
            )

            await self._record_observation(trend, comparison, now)
            await self._apply(trend, comparison, persistence, now, run)

        # Characteristics that vanished this window still need updating —
        # otherwise a trend that stopped occurring keeps displaying last
        # week's frequency forever, and §25's DEAD state is never reached.
        await self._decay_absent(
            tier=tier, observed=set(recent_counts), recent_total=recent_total,
            baseline_counts=baseline_counts, baseline_total=baseline_total,
            now=now, run=run,
        )

    # -- fetching -------------------------------------------------------------

    async def _feature_counts(self, tokens: list[Token]) -> dict[Subject, int]:
        """Distinct-token counts per (namespace, key, value) across a cohort.

        The SQL version did this with ``count(distinct token_id) ... GROUP BY``
        over an indexed join. This does it with one Firestore read per token's
        ``features`` subcollection, fetched concurrently, tallied in Python.
        """
        if not tokens:
            return {}
        results = await asyncio.gather(
            *(self.repo.token_features(t.mint) for t in tokens)
        )
        counts: dict[Subject, int] = {}
        for features in results:
            seen: set[Subject] = set()
            for f in features:
                subject: Subject = (f.namespace, f.key, f.value)
                if subject in seen:  # a token must not double-count itself
                    continue
                seen.add(subject)
                counts[subject] = counts.get(subject, 0) + 1
        return counts

    async def _window_is_usable(self, start: datetime, end: datetime) -> bool:
        rows = await self.repo.data_quality_since(start, end)
        unusable = sum(1 for r in rows if not r.is_usable_for_trends)
        # A single bad day inside a 7-day window is tolerable; a third of the
        # window being unusable is not.
        return unusable <= max(1, (end - start).days // 3)

    async def _persistence_days(self, slug: str) -> int:
        """Consecutive daily observations where the trend was present."""
        observations = await self.repo.trend_observations(slug, limit=30)
        streak = 0
        for obs in observations:  # already newest-first
            if obs.count and obs.count > 0:
                streak += 1
            else:
                break
        return streak

    async def _recent_series(self, slug: str, limit: int = 14) -> list[float]:
        observations = await self.repo.trend_observations(slug, limit=limit)
        values = [o.frequency for o in observations if o.frequency is not None]
        return list(reversed(values))  # oldest first, for slope fitting

    # -- persistence ----------------------------------------------------------

    async def _get_or_create_trend(
        self, *, namespace: str, key: str, value: str | None, tier: Decimal,
        category: TrendCategory, now: datetime, run: TrendRun,
    ) -> Trend:
        slug = _slugify(namespace, key, value, tier)
        existing = await self.repo.get_trend(slug)
        if existing is not None:
            run.trends_updated += 1
            return existing

        trend = Trend(
            slug=slug,
            name=_readable_name(namespace, key, value),
            description=(
                f"Frequency of {namespace}.{key}"
                + (f" = {value}" if value else "")
                + f" among tokens reaching ${tier:,.0f}."
            ),
            category=category.value,
            cohort_threshold_usd=tier,
            subject_namespace=namespace,
            subject_key=key,
            subject_value=value,
            status=TrendStatus.NEW.value,
            maturity=TrendMaturity.OBSERVATION.value,
            confidence=Confidence.LOW.value,
            first_detected_at=now,
        )
        await self.repo.upsert_trend(trend)
        run.trends_created += 1
        return trend

    async def _record_observation(
        self, trend: Trend, comparison: ComparisonResult, now: datetime
    ) -> None:
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        await self.repo.record_trend_observation(
            TrendObservation(
                trend_slug=trend.slug,
                observed_on=day,
                window_days=self.recent_days,
                count=comparison.recent.count,
                total=comparison.recent.total,
                frequency=comparison.recent.value,
                baseline_frequency=comparison.baseline.value,
                p_value=comparison.p_value,
            )
        )

    async def _apply(
        self, trend: Trend, comparison: ComparisonResult, persistence: int,
        now: datetime, run: TrendRun,
    ) -> None:
        series = await self._recent_series(trend.slug)
        age_days = (now - trend.first_detected_at).days if trend.first_detected_at else 0
        days_silent = (
            (now - trend.last_observed_at).days
            if trend.last_observed_at and comparison.recent.count == 0
            else 0
        )

        current_status = TrendStatus(trend.status) if trend.status else None
        decision = decide_status(
            current_status=current_status,
            comparison=comparison,
            recent_frequencies=series,
            days_since_last_occurrence=days_silent,
            persistence_days=persistence,
            age_days=age_days,
        )
        previous_status = current_status

        trend.recent_window_days = self.recent_days
        trend.recent_count = comparison.recent.count
        trend.recent_total = comparison.recent.total
        trend.recent_frequency = comparison.recent.value
        trend.baseline_window_days = self.baseline_days
        trend.baseline_count = comparison.baseline.count
        trend.baseline_total = comparison.baseline.total
        trend.baseline_frequency = comparison.baseline.value
        trend.change = comparison.change
        trend.relative_change = comparison.relative_change
        trend.lift = comparison.lift
        trend.sample_size = comparison.recent.total
        trend.p_value = comparison.p_value
        trend.effect_size = comparison.effect_size
        trend.ci_low = comparison.ci_low
        trend.ci_high = comparison.ci_high
        trend.persistence_days = persistence
        trend.maturity = comparison.maturity.value
        trend.confidence = comparison.confidence.value
        trend.evidence = comparison.as_dict()
        trend.last_observed_at = now

        if trend.peak_frequency is None or (
            trend.recent_frequency is not None and trend.recent_frequency > trend.peak_frequency
        ):
            trend.peak_frequency = trend.recent_frequency
            trend.peak_frequency_at = now

        if decision.changed:
            if is_revival(previous_status, decision.status):
                trend.revival_count += 1
                run.revivals += 1
            trend.status = decision.status.value
            trend.last_status_change_at = now
            run.status_changes += 1

            await self.repo.record_trend_history(
                TrendHistory(
                    trend_slug=trend.slug,
                    changed_at=now,
                    from_status=previous_status.value if previous_status else None,
                    to_status=decision.status.value,
                    to_maturity=comparison.maturity.value,
                    reason=decision.reason,
                    evidence=comparison.as_dict(),
                )
            )

        await self.repo.upsert_trend(trend)

    async def _decay_absent(
        self, *, tier: Decimal, observed: set[Subject], recent_total: int,
        baseline_counts: dict[Subject, int], baseline_total: int,
        now: datetime, run: TrendRun,
    ) -> None:
        """Update trends whose characteristic did not occur this window."""
        for trend in await self.repo.trends_for_tier(tier):
            if trend.status == TrendStatus.DEAD.value:
                continue
            subject: Subject = (
                trend.subject_namespace or "", trend.subject_key or "", trend.subject_value,
            )
            if subject in observed:
                continue

            comparison = compare_proportions(
                0, recent_total, baseline_counts.get(subject, 0), baseline_total,
                persistence_days=0,
            )
            await self._record_observation(trend, comparison, now)
            await self._apply(trend, comparison, 0, now, run)


# -----------------------------------------------------------------------------
# naming
# -----------------------------------------------------------------------------


def _slugify(namespace: str, key: str, value: str | None, tier: Decimal) -> str:
    tier_label = _tier_label(tier)
    parts = [namespace, key]
    if value:
        parts.append("".join(c if c.isalnum() else "-" for c in value.lower())[:40])
    parts.append(tier_label)
    return "-".join(p.strip("-") for p in parts if p)


def _readable_name(namespace: str, key: str, value: str | None) -> str:
    if value:
        return f"{value.replace('_', ' ').title()} ({namespace})"
    return f"{namespace}.{key}"


def _tier_label(tier: Decimal) -> str:
    if tier >= 1_000_000:
        return f"{int(tier // 1_000_000)}m"
    return f"{int(tier // 1000)}k"
