"""Trend Memory and anomalies (§24-§28, §45).

Firestore layout:

* ``trends/{slug}`` — one long-lived document per (namespace, key, value,
  cohort_threshold). The slug *is* the uniqueness key, so "get or create"
  is one ``.get()`` instead of a query — a genuine simplification over the
  SQL unique constraint it replaces.
* ``trends/{slug}/observations/{observed_on_iso}_{window_days}``
* ``trends/{slug}/history/{auto_id}``
* ``anomalies/{auto_id}``

Two axes stay separate throughout, exactly as in the original design:
``status`` (direction) and ``maturity`` (evidential standing) — see
:mod:`app.trends.lifecycle` and :mod:`app.analysis.stats`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.db.enums import AnomalyKind, Confidence, TrendCategory, TrendMaturity, TrendStatus


@dataclass(slots=True)
class Trend:
    slug: str
    name: str = ""
    description: str | None = None
    category: str = TrendCategory.OTHER

    cohort_threshold_usd: Decimal | None = None
    subject_namespace: str | None = None
    subject_key: str | None = None
    subject_value: str | None = None
    narrative_slug: str | None = None
    launchpad_slug: str | None = None

    status: str = TrendStatus.NEW
    maturity: str = TrendMaturity.OBSERVATION
    confidence: str = Confidence.LOW

    recent_window_days: int | None = None
    recent_count: int = 0
    recent_total: int = 0
    recent_frequency: float | None = None

    baseline_window_days: int | None = None
    baseline_count: int = 0
    baseline_total: int = 0
    baseline_frequency: float | None = None

    change: float | None = None
    relative_change: float | None = None
    lift: float | None = None

    sample_size: int = 0
    p_value: float | None = None
    effect_size: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    persistence_days: int = 0
    variance: float | None = None

    first_detected_at: datetime | None = None
    last_observed_at: datetime | None = None
    last_status_change_at: datetime | None = None
    peak_frequency: float | None = None
    peak_frequency_at: datetime | None = None

    revival_count: int = 0

    evidence: dict[str, Any] = field(default_factory=dict)
    example_token_mints: list[str] = field(default_factory=list)

    created_at: datetime | None = None
    updated_at: datetime | None = None

    _MONEY_FIELDS = frozenset({"cohort_threshold_usd"})


@dataclass(slots=True)
class TrendObservation:
    trend_slug: str
    observed_on: datetime
    window_days: int
    count: int = 0
    total: int = 0
    frequency: float | None = None
    baseline_frequency: float | None = None
    p_value: float | None = None

    @property
    def doc_id(self) -> str:
        return f"{self.observed_on.date().isoformat()}_{self.window_days}"


@dataclass(slots=True)
class TrendHistory:
    trend_slug: str
    changed_at: datetime
    from_status: str | None = None
    to_status: str = TrendStatus.NEW
    from_maturity: str | None = None
    to_maturity: str | None = None
    reason: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Anomaly:
    id: str = ""
    kind: str = AnomalyKind.NARRATIVE_SPIKE
    title: str = ""
    description: str | None = None

    detected_at: datetime | None = None
    severity: float | None = None
    magnitude: float | None = None
    sample_size: int = 0

    trend_slug: str | None = None
    launchpad_slug: str | None = None
    narrative_slug: str | None = None
    creator_wallet: str | None = None
    research_task_id: str | None = None

    evidence: dict[str, Any] = field(default_factory=dict)
    acknowledged: bool = False
    dismissed_reason: str | None = None

    created_at: datetime | None = None
