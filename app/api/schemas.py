"""API response contract.

This module is the integration seam. The React frontend and the fixture server
in ``tools/fixture-server`` both target exactly these shapes, so a change here
is a change to three things and should be made deliberately.

Conventions, applied everywhere:

* **Money is a string**, not a float. Market caps are ``Decimal`` in the
  database and JSON floats cannot represent them exactly. The frontend formats
  strings; it never does arithmetic on them.
* **Percentages are floats in [0, 1]**, never pre-multiplied by 100. One
  convention, applied at the formatting layer.
* **Every rate ships with its denominator.** ``frequency`` never travels
  without ``sample_size``. §26 and §33 both require the sample to be visible,
  and the surest way to guarantee that is to make it impossible to fetch one
  without the other.
* **Timestamps are ISO 8601 with offset.**
* **Nullable means unknown, not zero.** A ``null`` market cap means we do not
  have one. It must never be rendered as $0.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_serializer

T = TypeVar("T")


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class Page(ApiModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class MoneyMixin(BaseModel):
    """Serialises every ``Decimal`` on a model as a string. See module docstring."""

    @field_serializer("*", when_used="json")
    def _serialise_decimal(self, value: Any) -> Any:
        return str(value) if isinstance(value, Decimal) else value


# -----------------------------------------------------------------------------
# Evidence — attached to anything the system asserts
# -----------------------------------------------------------------------------


class EvidenceRef(ApiModel):
    """Provenance for one asserted value (§21).

    Present on every model whose value came from outside. The frontend renders
    it as an inspectable badge rather than prose, so "how do you know that"
    is answerable in one click on any screen.
    """

    source: str | None = None
    source_type: str | None = Field(
        None, description="blockchain | indexer | web | model | other"
    )
    observed_at: datetime | None = None
    verification_status: str = "pending"
    confidence: float | None = None


class SampleRef(ApiModel):
    """A rate and the sample it was computed over. Never separate these."""

    count: int
    total: int
    frequency: float | None = None

    @classmethod
    def of(cls, count: int, total: int) -> "SampleRef":
        return cls(count=count, total=total, frequency=(count / total) if total else None)


# -----------------------------------------------------------------------------
# Tokens
# -----------------------------------------------------------------------------


class MilestoneOut(ApiModel, MoneyMixin):
    kind: str
    threshold_usd: Decimal | None = None
    reached_at: datetime | None = None
    market_cap: Decimal | None = None
    liquidity_usd: Decimal | None = None
    volume_usd: Decimal | None = None
    holder_count: int | None = None
    token_age_minutes: int | None = None
    evidence: EvidenceRef | None = None


class FeatureOut(ApiModel):
    namespace: str
    key: str
    value: str | None = None
    numeric_value: float | None = None
    source: str = "deterministic"
    confidence: float | None = None


class ImageFeatureOut(ApiModel):
    image_url: str | None = None
    categories: list[str] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)
    style: str | None = None
    has_text: bool | None = None
    is_ai_generated_style: bool | None = None
    references_existing_meme: bool | None = None
    model: str | None = None
    confidence: float | None = None
    failure_reason: str | None = None


class TokenSummary(ApiModel, MoneyMixin):
    """List-row shape. Kept small — token tables render hundreds of these."""

    id: str
    mint: str
    name: str | None = None
    symbol: str | None = None
    image_url: str | None = None
    launchpad_slug: str | None = None
    creator_wallet: str | None = None
    launched_at: datetime | None = None
    qualified_at: datetime | None = None
    qualified_market_cap: Decimal | None = None
    peak_market_cap: Decimal | None = None
    peak_tier: Decimal | None = None
    is_qualified: bool = False
    verification_status: str = "pending"
    themes: list[str] = Field(default_factory=list)


class TokenDetail(TokenSummary):
    """Full research page for one token (§57)."""

    description: str | None = None
    decimals: int | None = None
    total_supply: Decimal | None = None
    ecosystem: str = "solana"

    migrated_at: datetime | None = None
    migration_platform: str | None = None
    destination_dex_slug: str | None = None
    minutes_launch_to_migration: int | None = None

    latest_market_cap: Decimal | None = None
    latest_liquidity_usd: Decimal | None = None
    latest_volume_24h_usd: Decimal | None = None
    latest_holder_count: int | None = None
    market_data_at: datetime | None = None

    website: str | None = None
    twitter: str | None = None
    telegram: str | None = None

    pipeline_stage: str = "discovery"
    data_sources: list[str] = Field(default_factory=list)
    qualification_evidence: dict[str, Any] = Field(default_factory=dict)

    milestones: list[MilestoneOut] = Field(default_factory=list)
    features: list[FeatureOut] = Field(default_factory=list)
    image_features: ImageFeatureOut | None = None
    related_trends: list["TrendSummary"] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Creators, launchpads, narratives
# -----------------------------------------------------------------------------


class CreatorSummary(ApiModel, MoneyMixin):
    id: str
    wallet: str
    total_launches: int = 0
    wins_100k: int = 0
    wins_250k: int = 0
    wins_500k: int = 0
    wins_1m: int = 0
    success_rate: float | None = None
    best_market_cap: Decimal | None = None
    is_repeat_winner: bool = False
    first_launch_at: datetime | None = None
    last_launch_at: datetime | None = None
    primary_launchpad_slug: str | None = None


class CreatorDetail(CreatorSummary):
    median_hours_between_launches: float | None = None
    launchpad_history: list[dict[str, Any]] = Field(default_factory=list)
    recent_tokens: list[TokenSummary] = Field(default_factory=list)
    sample: SampleRef | None = Field(
        None,
        description="Wins over launches. Shown so a 100% rate on 1 launch reads correctly.",
    )


class LaunchpadSummary(ApiModel):
    id: str
    slug: str
    name: str
    lifecycle: str = "emerging"
    launch_count: int = 0
    qualified_count: int = 0
    success_rate: float | None = None
    market_share: float | None = None
    growth_rate_7d: float | None = None
    growth_rate_30d: float | None = None
    is_known: bool = False
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


class LaunchpadDetail(LaunchpadSummary):
    website: str | None = None
    ecosystem: str = "solana"
    discovered_by: str | None = None
    median_minutes_to_first_milestone: int | None = None
    counts_by_tier: dict[str, int] = Field(default_factory=dict)
    migration_destinations: list[dict[str, Any]] = Field(default_factory=list)
    top_creators: list[CreatorSummary] = Field(default_factory=list)
    recent_tokens: list[TokenSummary] = Field(default_factory=list)
    share_history: list[dict[str, Any]] = Field(default_factory=list)
    notes: str | None = None


class NarrativeSummary(ApiModel):
    id: str
    slug: str
    label: str
    category: str | None = None
    token_count: int = 0
    qualified_count: int = 0
    share_of_qualified: float | None = None
    baseline_share: float | None = None
    is_emergent: bool = True
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


class NarrativeDetail(NarrativeSummary):
    description: str | None = None
    keywords: list[str] = Field(default_factory=list)
    related_trends: list["TrendSummary"] = Field(default_factory=list)
    recent_tokens: list[TokenSummary] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Trends
# -----------------------------------------------------------------------------


class TrendSummary(ApiModel, MoneyMixin):
    id: str
    slug: str
    name: str
    category: str
    status: str
    maturity: str = Field(description="observation | candidate | validated")
    confidence: str

    cohort_threshold_usd: Decimal | None = None
    recent: SampleRef
    baseline: SampleRef
    # The windows the two samples cover. Required for the UI to label a
    # frequency at all — "23.1%" is meaningless without "over 7 days".
    recent_window_days: int | None = None
    baseline_window_days: int | None = None
    change: float | None = None
    relative_change: float | None = None
    lift: float | None = None
    recent_series: list[float] = Field(
        default_factory=list,
        description="Daily frequencies, oldest first, for the list-view sparkline.",
    )

    first_detected_at: datetime | None = None
    last_observed_at: datetime | None = None
    persistence_days: int = 0


class TrendDetail(TrendSummary):
    description: str | None = None
    p_value: float | None = None
    effect_size: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    variance: float | None = None
    revival_count: int = 0
    peak_frequency: float | None = None
    peak_frequency_at: datetime | None = None

    caveats: list[str] = Field(
        default_factory=list,
        description="Why this trend is not stronger than stated. Rendered, never hidden.",
    )
    evidence: dict[str, Any] = Field(default_factory=dict)
    observations: list["TrendObservationOut"] = Field(default_factory=list)
    history: list["TrendHistoryOut"] = Field(default_factory=list)
    example_tokens: list[TokenSummary] = Field(default_factory=list)


class TrendObservationOut(ApiModel):
    observed_on: datetime
    window_days: int
    count: int
    total: int
    frequency: float | None = None
    baseline_frequency: float | None = None
    p_value: float | None = None


class TrendHistoryOut(ApiModel):
    changed_at: datetime
    from_status: str | None = None
    to_status: str
    to_maturity: str | None = None
    reason: str | None = None


# -----------------------------------------------------------------------------
# Research
# -----------------------------------------------------------------------------


class ResearchTaskSummary(ApiModel, MoneyMixin):
    id: str
    question: str
    reason: str | None = None
    origin: str
    status: str
    priority: float | None = None
    confidence: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    cost_usd: Decimal | None = None


class ResearchTaskDetail(ResearchTaskSummary):
    goal: str | None = None
    completion_condition: str | None = None
    evidence_requirements: list[str] = Field(default_factory=list)
    priority_components: dict[str, float] = Field(default_factory=dict)

    max_iterations: int = 0
    max_tool_calls: int = 0
    time_limit_seconds: int = 0
    iterations_used: int = 0
    tool_calls_used: int = 0
    max_cost_usd: Decimal | None = None

    plan: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    result: str | None = None
    result_claim_type: str | None = None
    limitations: str | None = None
    failure_reason: str | None = None
    tool_calls: list["ToolCallOut"] = Field(default_factory=list)


class ResearchNoteOut(ApiModel):
    id: str
    title: str
    body: str
    claim_type: str
    confidence: str
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    sample_size: int | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    created_at: datetime
    is_current: bool = True
    evidence: dict[str, Any] = Field(default_factory=dict)
    counter_evidence: dict[str, Any] = Field(default_factory=dict)


class HypothesisOut(ApiModel):
    id: str
    slug: str
    statement: str
    rationale: str | None = None
    status: str
    confidence: str
    sample_size: int = 0
    supporting_observations: int = 0
    contradicting_observations: int = 0
    p_value: float | None = None
    effect_size: float | None = None
    test_method: str | None = None
    first_tested_at: datetime | None = None
    last_tested_at: datetime | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    counter_evidence: dict[str, Any] = Field(default_factory=dict)


class AnomalyOut(ApiModel):
    id: str
    kind: str
    title: str
    description: str | None = None
    detected_at: datetime
    severity: float | None = None
    magnitude: float | None = None
    sample_size: int = 0
    acknowledged: bool = False
    research_task_id: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


# -----------------------------------------------------------------------------
# Reports
# -----------------------------------------------------------------------------


class ReportSummary(ApiModel):
    id: str
    kind: str
    title: str
    period_start: datetime
    period_end: datetime
    headline_finding: str | None = None
    tokens_qualified: int = 0
    trends_new: int = 0
    trends_rising: int = 0
    trends_declining: int = 0


class ReportDetail(ReportSummary):
    summary: str | None = None
    sections: dict[str, Any] = Field(default_factory=dict)
    markdown: str | None = None
    biggest_change: str | None = None
    limitations: str | None = None
    counts_by_tier: dict[str, int] = Field(default_factory=dict)
    tasks_created: int = 0
    generated_by_model: str | None = None


# -----------------------------------------------------------------------------
# Annie
# -----------------------------------------------------------------------------


class Citation(ApiModel):
    """One database-backed support for something Annie said (§33)."""

    kind: str = Field(description="token | trend | creator | launchpad | note | report")
    id: str | None = None
    label: str
    detail: str | None = None
    sample: SampleRef | None = None


class ToolCallOut(ApiModel, MoneyMixin):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result_summary: str | None = None
    result_rows: int | None = None
    succeeded: bool = True
    error_message: str | None = None
    duration_ms: int | None = None
    estimated_cost_usd: Decimal | None = None


class MessageOut(ApiModel, MoneyMixin):
    id: str
    role: str
    content: str
    claim_type: str | None = None
    confidence: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    tool_calls: list[ToolCallOut] = Field(default_factory=list)
    created_at: datetime
    model: str | None = None
    cost_usd: Decimal | None = None
    latency_ms: int | None = None


class ConversationSummary(ApiModel):
    id: str
    title: str | None = None
    message_count: int = 0
    last_message_at: datetime | None = None
    created_at: datetime


class ConversationDetail(ConversationSummary):
    messages: list[MessageOut] = Field(default_factory=list)


class ChatRequest(ApiModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None


# -----------------------------------------------------------------------------
# System
# -----------------------------------------------------------------------------


class CapabilityOut(ApiModel):
    key: str
    label: str
    tier: str = Field(description="required | primary | optional")
    status: str = Field(description="available | degraded | disabled")
    description: str
    missing_env_vars: list[str] = Field(default_factory=list)


class ProviderHealthOut(ApiModel, MoneyMixin):
    provider: str
    capability_label: str | None = None
    status: str = Field(description="ok | degraded | down | disabled")
    configured: bool = True
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error_message: str | None = None
    requests_24h: int = 0
    errors_24h: int = 0
    rate_limited_24h: int = 0
    error_rate_24h: float | None = None
    p50_latency_ms: int | None = None
    p95_latency_ms: int | None = None
    estimated_cost_24h_usd: Decimal | None = None
    data_freshness_seconds: int | None = None
    missing_env_vars: list[str] = Field(default_factory=list)


class DataQualityOut(ApiModel):
    measured_on: datetime
    stage: str
    attempted: int
    succeeded: int
    failed: int
    coverage: float | None = None
    is_usable_for_trends: bool = True
    notes: str | None = None


class DashboardOut(ApiModel, MoneyMixin):
    """Everything the dashboard needs, in one round trip (§56)."""

    tokens_collected: int = 0
    tokens_qualified: int = 0
    counts_by_tier: dict[str, int] = Field(default_factory=dict)
    counts_by_tier_previous: dict[str, int] = Field(
        default_factory=dict,
        description="Same window, one period earlier. Deltas are computed client-side.",
    )
    window_days: int = 7

    trends_active: int = 0
    trends_new: int = 0
    trends_rising: int = 0
    trends_declining: int = 0

    rising_trends: list[TrendSummary] = Field(default_factory=list)
    new_trends: list[TrendSummary] = Field(default_factory=list)
    declining_trends: list[TrendSummary] = Field(default_factory=list)
    emerging_launchpads: list[LaunchpadSummary] = Field(default_factory=list)
    recent_notes: list[ResearchNoteOut] = Field(default_factory=list)
    pending_tasks: list[ResearchTaskSummary] = Field(default_factory=list)
    open_anomalies: list[AnomalyOut] = Field(default_factory=list)

    data_freshness_seconds: int | None = None
    last_ingestion_at: datetime | None = None
    last_trend_run_at: datetime | None = None
    provider_health: list[ProviderHealthOut] = Field(default_factory=list)
    degraded_capabilities: list[CapabilityOut] = Field(default_factory=list)


class SettingOut(ApiModel):
    key: str
    value: Any
    description: str | None = None
    updated_at: datetime | None = None


class PipelineRunOut(ApiModel):
    id: str
    stage: str
    trigger: str
    status: str
    result: dict[str, Any] = {}
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class MemoryOut(ApiModel):
    id: str
    type: str
    title: str
    content: str
    structured_data: dict[str, Any] = Field(default_factory=dict)
    source_type: str | None = None
    source_id: str | None = None
    confidence: str
    importance: float | None = None
    status: str
    tags: list[str] = Field(default_factory=list)
    related_memory_ids: list[str] = Field(default_factory=list)
    related_research_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime | None = None
    last_used_at: datetime | None = None


class ConsolidationRunOut(ApiModel):
    id: str
    run_at: datetime | None = None
    memories_reviewed: int = 0
    memories_promoted: int = 0
    memories_archived: int = 0
    promoted_memory_ids: list[str] = Field(default_factory=list)
    archived_memory_ids: list[str] = Field(default_factory=list)
    summary: str | None = None
    model: str | None = None
    error: str | None = None
    created_at: datetime


class PersonalityConfigOut(ApiModel):
    name: str
    description: str
    tone: str
    communication_style: str
    skepticism_level: str
    pushback_degree: str
    explanation_style: str
    source_text: str = ""
    updated_at: datetime | None = None
    updated_by: str | None = None


class ErrorOut(ApiModel):
    """Uniform error envelope.

    ``missing_env_vars`` is populated for capability failures so the UI can
    tell the operator exactly what to set instead of showing "503".
    """

    error: str
    detail: str | None = None
    capability: str | None = None
    missing_env_vars: list[str] = Field(default_factory=list)


TokenDetail.model_rebuild()
TrendDetail.model_rebuild()
NarrativeDetail.model_rebuild()
ResearchTaskDetail.model_rebuild()
