"""Operational records: provider health, data quality, settings, audit,
tool calls, data conflicts (§21, §49, §50, §66, §67).

Firestore layout:

* ``provider_health/{provider}`` — one rolled-up document per provider,
  updated on every call via a read-modify-write with an approximate 24h
  rolling window (``window_started_at``, reset when stale). A raw per-call
  event log (the original ``provider_events`` table) is not kept in this
  deployment — see the module docstring in :mod:`app.db.repo` for why.
* ``data_quality/{measured_on_iso}_{stage}``
* ``settings/{key}``
* ``audit_log/{auto_id}``
* ``tool_calls/{auto_id}`` — every tool Annie invokes (§47).
* ``data_conflicts/{auto_id}`` — material provider disagreements (§21).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.db.enums import PipelineStage


@dataclass(slots=True)
class ProviderHealth:
    provider: str
    status: str = "unknown"  # ok | degraded | down | disabled
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error_message: str | None = None

    window_started_at: datetime | None = None
    requests_24h: int = 0
    errors_24h: int = 0
    rate_limited_24h: int = 0
    error_rate_24h: float | None = None
    latency_samples_ms: list[int] = field(default_factory=list)  # capped, see repo
    p50_latency_ms: int | None = None
    p95_latency_ms: int | None = None
    estimated_cost_24h_usd: Decimal | None = None

    data_freshness_seconds: int | None = None
    computed_at: datetime | None = None

    _MONEY_FIELDS = frozenset({"estimated_cost_24h_usd"})


@dataclass(slots=True)
class DataQuality:
    measured_on: datetime
    stage: str = PipelineStage.DISCOVERY
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    coverage: float | None = None
    is_usable_for_trends: bool = True
    notes: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def doc_id(self) -> str:
        return f"{self.measured_on.date().isoformat()}_{self.stage}"


@dataclass(slots=True)
class Setting:
    key: str
    value: Any = None
    description: str | None = None
    updated_by: str | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class AuditLog:
    id: str = ""
    actor: str = "operator"  # operator | annie | scheduler
    action: str = ""
    subject_type: str | None = None
    subject_id: str | None = None
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass(slots=True)
class ToolCall:
    id: str = ""
    tool: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    result_summary: str | None = None
    result_rows: int | None = None

    succeeded: bool = True
    error_message: str | None = None
    duration_ms: int | None = None
    estimated_cost_usd: Decimal | None = None

    conversation_id: str | None = None
    message_id: str | None = None
    research_task_id: str | None = None

    created_at: datetime | None = None

    _MONEY_FIELDS = frozenset({"estimated_cost_usd"})


@dataclass(slots=True)
class DataConflict:
    id: str = ""
    subject_type: str = "token"  # token | creator | launchpad
    subject_id: str = ""
    # Named field_name, not field: a dataclass attribute literally named
    # "field" shadows the `dataclasses.field()` factory used two lines below,
    # in this same class body — Python resolves that call against the
    # now-rebound class-namespace name, not the import.
    field_name: str = ""

    detected_at: datetime | None = None
    values: list[dict[str, Any]] = field(default_factory=list)  # [{provider, value, observed_at}]
    divergence: float | None = None

    resolution: str | None = None  # chosen | unresolved | both_retained
    chosen_provider: str | None = None
    chosen_value: str | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = None

    research_task_id: str | None = None
    created_at: datetime | None = None
