"""Research Memory, the task queue, hypotheses, reports, Annie's work memory,
and Annie's chat (§29, §36, §44, §46, §62).

Firestore layout — all top-level, Firestore auto-generated IDs (no natural
key exists for a task, a note, a memory, a conversation or a message):

* ``research_tasks/{auto_id}``
* ``research_notes/{auto_id}``
* ``research_hypotheses/{slug}`` — slugified from the statement; natural key
* ``reports/{kind}_{period_start_iso}``
* ``memories/{auto_id}`` — Annie's long-term/daily-log work memory; see ``Memory``
* ``conversations/{auto_id}``
* ``conversations/{auto_id}/messages/{auto_id}``

The budget fields on ``ResearchTask`` are the mechanism by which §36's "Annie
must not recursively research forever" is enforced — see ``budget_exhausted``.
Persisted per task rather than held in the agent loop so a worker restart
cannot reset a task's spend to zero.

**"Research Memory" (the operator-facing concept) is ``ResearchTask`` +
``ResearchNote`` together, not a separate collection.** A note's ``task_id``
links it back to the task that produced it; the claim-type/confidence/
evidence vocabulary here is exactly what a "memory" needs and already
exists, so Annie's newer, broader work memory (``Memory``, below) is
deliberately a distinct, narrower concept: durable cross-cutting knowledge
and daily activity logs, not a second copy of research findings. A memory
can *point at* a research note or task (``related_research_ids``) instead of
duplicating what it found.
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
    MemoryStatus,
    MemoryType,
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
class Memory:
    """Annie's durable work memory — long-term knowledge and daily activity
    logs. See this module's docstring for how this differs from
    ``ResearchNote`` (findings) and ``Conversation``/``Message`` (chat
    history), which this is deliberately not a replacement for.

    ``status`` mirrors ``ResearchNote``'s ``is_current``/``superseded_by_id``
    pattern but as an explicit enum (``MemoryStatus``) since a memory has
    more than two states worth distinguishing — see app/scheduling for the
    consolidation job that transitions these.
    """

    id: str = ""
    type: str = MemoryType.LONG_TERM
    title: str = ""
    content: str = ""
    structured_data: dict[str, Any] = field(default_factory=dict)

    source_type: str | None = None  # e.g. "consolidation" | "research_task" | "pipeline" | "manual"
    source_id: str | None = None

    confidence: str = Confidence.LOW
    importance: float | None = None  # 0-1; higher surfaces first in retrieval
    status: str = MemoryStatus.ACTIVE

    tags: list[str] = field(default_factory=list)
    related_memory_ids: list[str] = field(default_factory=list)
    related_research_ids: list[str] = field(default_factory=list)

    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_used_at: datetime | None = None


@dataclass(slots=True)
class PersonalityConfig:
    """Operator-editable personality knobs — ``personality/config``, one
    singleton document. Deliberately narrow: this influences *voice*
    (tone, how skeptical she sounds, how much she pushes back, how she
    explains things), never the hard epistemic rules in
    ``app/annie/persona.py`` (source-of-truth, claim discipline, evidence
    standard, money) — those stay hard-coded and unconditional regardless
    of what's configured here. See ``persona.system_prompt``'s
    ``personality_overrides`` parameter for exactly where this plugs in.
    """

    name: str = "Annie"
    description: str = ""
    tone: str = ""
    communication_style: str = ""
    skepticism_level: str = ""
    pushback_degree: str = ""
    explanation_style: str = ""
    updated_at: datetime | None = None
    updated_by: str | None = None


@dataclass(slots=True)
class ConsolidationRun:
    """One execution of memory consolidation ("Dreams") — the run itself,
    distinct from its effects (the `Memory` records it created/archived,
    already visible on the Long-Term tab via `source_type: "consolidation"`).
    Exists purely so the Memory page's Dreams tab has something to show
    even when a run promoted and archived nothing — "consolidation ran and
    found nothing worth changing" is itself worth being able to see."""

    id: str = ""
    run_at: datetime | None = None
    memories_reviewed: int = 0
    memories_promoted: int = 0
    memories_archived: int = 0
    promoted_memory_ids: list[str] = field(default_factory=list)
    archived_memory_ids: list[str] = field(default_factory=list)
    summary: str | None = None
    model: str | None = None
    error: str | None = None
    created_at: datetime | None = None


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
