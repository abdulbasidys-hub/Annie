"""Research Memory, the task queue, hypotheses, reports, and Annie's chat
(§29, §36, §44, §46, §62).

Firestore layout — all top-level, Firestore auto-generated IDs (no natural
key exists for a task, a note, a conversation or a message):

* ``research_tasks/{auto_id}``
* ``research_notes/{auto_id}``
* ``research_hypotheses/{slug}`` — slugified from the statement; natural key
* ``reports/{kind}_{period_start_iso}``
* ``conversations/{auto_id}``
* ``conversations/{auto_id}/messages/{auto_id}``

The budget fields on ``ResearchTask`` are the mechanism by which §36's "Annie
must not recursively research forever" is enforced — see ``budget_exhausted``.
Persisted per task rather than held in the agent loop so a worker restart
cannot reset a task's spend to zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.db.enums import (
    ClaimType,
    Confidence,
    HypothesisStatus,
    ReportKind,
    ResearchTaskOrigin,
    ResearchTaskStatus,
)


@dataclass(slots=True)
class ResearchTask:
    id: str = ""
    question: str = ""
    reason: str | None = None
    goal: str | None = None
    completion_condition: str | None = None
    evidence_requirements: list[str] = field(default_factory=list)

    origin: str = ResearchTaskOrigin.ANNIE
    status: str = ResearchTaskStatus.QUEUED

    priority: float | None = None
    priority_components: dict[str, float] = field(default_factory=dict)
    economic_relevance: float | None = None
    novelty: float | None = None

    max_iterations: int = 6
    max_tool_calls: int = 25
    max_cost_usd: Decimal | None = None
    time_limit_seconds: int = 600

    iterations_used: int = 0
    tool_calls_used: int = 0
    cost_usd: Decimal | None = None

    plan: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    result: str | None = None
    result_claim_type: str | None = None
    confidence: str | None = None
    limitations: str | None = None
    failure_reason: str | None = None

    started_at: datetime | None = None
    completed_at: datetime | None = None

    trend_slug: str | None = None
    hypothesis_slug: str | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None

    _MONEY_FIELDS = frozenset({"max_cost_usd", "cost_usd"})

    @property
    def budget_exhausted(self) -> bool:
        """True when any bound is spent. Checked before every agent iteration."""
        if self.iterations_used >= self.max_iterations:
            return True
        if self.tool_calls_used >= self.max_tool_calls:
            return True
        if (
            self.max_cost_usd is not None
            and self.cost_usd is not None
            and self.cost_usd >= self.max_cost_usd
        ):
            return True
        return False


@dataclass(slots=True)
class ResearchNote:
    id: str = ""
    title: str = ""
    body: str = ""
    claim_type: str = ClaimType.HYPOTHESIS
    confidence: str = Confidence.LOW

    category: str | None = None
    tags: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    counter_evidence: dict[str, Any] = field(default_factory=dict)
    sample_size: int | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None

    task_id: str | None = None
    trend_slug: str | None = None
    hypothesis_slug: str | None = None

    superseded_by_id: str | None = None
    is_current: bool = True

    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class ResearchHypothesis:
    slug: str
    statement: str = ""
    rationale: str | None = None

    status: str = HypothesisStatus.NEW
    confidence: str = Confidence.LOW

    evidence: dict[str, Any] = field(default_factory=dict)
    counter_evidence: dict[str, Any] = field(default_factory=dict)
    sample_size: int = 0
    supporting_observations: int = 0
    contradicting_observations: int = 0

    test_method: str | None = None
    p_value: float | None = None
    effect_size: float | None = None

    first_tested_at: datetime | None = None
    last_tested_at: datetime | None = None
    resolved_at: datetime | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class Report:
    id: str = ""
    kind: str = ReportKind.DAILY
    period_start: datetime | None = None
    period_end: datetime | None = None

    title: str = ""
    summary: str | None = None
    sections: dict[str, Any] = field(default_factory=dict)
    markdown: str | None = None

    headline_finding: str | None = None
    biggest_change: str | None = None
    limitations: str | None = None

    tokens_qualified: int = 0
    counts_by_tier: dict[str, int] = field(default_factory=dict)
    trends_new: int = 0
    trends_rising: int = 0
    trends_declining: int = 0
    tasks_created: int = 0

    generated_by_model: str | None = None
    generation_cost_usd: Decimal | None = None

    created_at: datetime | None = None

    _MONEY_FIELDS = frozenset({"generation_cost_usd"})

    @property
    def doc_id(self) -> str:
        period = self.period_start.date().isoformat() if self.period_start else "unknown"
        return f"{self.kind}_{period}"


@dataclass(slots=True)
class Conversation:
    id: str = ""
    title: str | None = None
    last_message_at: datetime | None = None
    message_count: int = 0
    total_cost_usd: Decimal | None = None
    archived: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None

    _MONEY_FIELDS = frozenset({"total_cost_usd"})


@dataclass(slots=True)
class Message:
    id: str = ""
    conversation_id: str = ""
    role: str = "user"  # user | annie | system
    content: str = ""

    claim_type: str | None = None
    confidence: str | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: Decimal | None = None
    latency_ms: int | None = None

    created_at: datetime | None = None

    _MONEY_FIELDS = frozenset({"cost_usd"})
