"""Controlled vocabularies shared by the database, API and frontend.

These are the words the system is allowed to use about its own certainty. They
are deliberately narrow: Build.md §32 and §43 hinge on Annie never blurring
"observed" into "explained", and a free-text status column is exactly how that
blurring happens.

Stored as strings rather than native PG enums so that adding a value is a code
change, not a migration with a table lock — the spec expects categories to
emerge over time (§15, §16).
"""

from __future__ import annotations

from enum import StrEnum


class VerificationStatus(StrEnum):
    """Build.md §21."""

    PENDING = "pending"
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    CROSS_VERIFIED = "cross_verified"
    DISPUTED = "disputed"


class ClaimType(StrEnum):
    """Build.md §32 — the epistemic ladder Annie must not climb silently."""

    FACT = "fact"
    INFERENCE = "inference"
    HYPOTHESIS = "hypothesis"
    SPECULATION = "speculation"


class ExplanationClass(StrEnum):
    """Build.md §19 — how an explanation for a migration is labelled."""

    OBSERVED = "observed"
    SUPPORTED_HYPOTHESIS = "supported_hypothesis"
    SPECULATION = "speculation"


class MilestoneKind(StrEnum):
    """Build.md §7.

    ``MARKET_CAP`` carries its threshold in a separate column rather than being
    enumerated per dollar value, because §4 requires the qualification ladder
    to be configurable.
    """

    LAUNCH = "launch"
    MIGRATION = "migration"
    MARKET_CAP = "market_cap"
    PEAK = "peak"


class TrendStatus(StrEnum):
    """Build.md §25 — trend lifecycle."""

    NEW = "new"
    RISING = "rising"
    STABLE = "stable"
    DECLINING = "declining"
    DEAD = "dead"


class TrendMaturity(StrEnum):
    """Build.md §26 — evidential standing, orthogonal to direction.

    A trend can be RISING and still only a CANDIDATE. Collapsing these two axes
    into one status is how a two-day fluke gets promoted to a finding.
    """

    OBSERVATION = "observation"
    CANDIDATE = "candidate"
    VALIDATED = "validated"


class TrendCategory(StrEnum):
    NARRATIVE = "narrative"
    IMAGE = "image"
    NAME = "name"
    TICKER = "ticker"
    DESCRIPTION = "description"
    LAUNCHPAD = "launchpad"
    CREATOR = "creator"
    MIGRATION = "migration"
    MARKET_STRUCTURE = "market_structure"
    SOCIAL = "social"
    OTHER = "other"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class HypothesisStatus(StrEnum):
    """Build.md §44."""

    NEW = "new"
    TESTING = "testing"
    SUPPORTED = "supported"
    WEAKENING = "weakening"
    REJECTED = "rejected"
    VALIDATED = "validated"


class ResearchTaskStatus(StrEnum):
    """Build.md §46."""

    QUEUED = "queued"
    RESEARCHING = "researching"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


class ResearchTaskOrigin(StrEnum):
    """Who asked the question. Used to keep autonomous work auditable (§63)."""

    USER = "user"
    ANOMALY = "anomaly"
    TREND_CHANGE = "trend_change"
    SCHEDULED = "scheduled"
    PROVIDER_CONFLICT = "provider_conflict"
    ANNIE = "annie"


class LaunchpadLifecycle(StrEnum):
    """Build.md §18."""

    EMERGING = "emerging"
    GROWING = "growing"
    STABLE = "stable"
    DECLINING = "declining"
    DEAD = "dead"


class PipelineStage(StrEnum):
    """Build.md §20 — staged acquisition, cheap work before expensive work."""

    DISCOVERY = "discovery"
    QUALIFICATION = "qualification"
    ENRICHMENT = "enrichment"
    ANALYSIS = "analysis"
    AI_RESEARCH = "ai_research"


class ProviderEventType(StrEnum):
    REQUEST = "request"
    SUCCESS = "success"
    ERROR = "error"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    FALLBACK = "fallback"
    CONFLICT = "conflict"


class AnomalyKind(StrEnum):
    """Build.md §45."""

    NARRATIVE_SPIKE = "narrative_spike"
    LAUNCHPAD_GROWTH = "launchpad_growth"
    CREATOR_SUCCESS = "creator_success"
    CONCENTRATION = "concentration"
    UNEXPECTED_DECLINE = "unexpected_decline"
    IMAGE_PATTERN = "image_pattern"
    TICKER_PATTERN = "ticker_pattern"
    MIGRATION_SHIFT = "migration_shift"
    PROVIDER_DISCREPANCY = "provider_discrepancy"


class ReportKind(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
