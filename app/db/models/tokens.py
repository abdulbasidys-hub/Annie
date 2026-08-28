"""Tokens and everything observed about them (§6, §7, §15, §16, §20).

Firestore layout:

* ``tokens/{mint}`` — one document per token. §20's staged pipeline lives on
  ``pipeline_stage``, so the system can distinguish "no image analysis because
  there is no image" from "because enrichment never ran" (§15).
* ``tokens/{mint}/milestones/{kind}_{threshold_or_iso}`` — sparse, one per
  threshold crossing (§7), not a time series.
* ``tokens/{mint}/features/{namespace}__{key}__{value}`` — one doc per
  *distinct value*, not per (namespace, key). The original Postgres schema's
  unique constraint was ``(token_id, namespace, key)``, which would have
  silently dropped every word but the last one ``extract_all`` emits for a
  multi-word name — a latent bug in a backend that had never been run. Keying
  on the full triple fixes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.db.enums import PipelineStage


@dataclass(slots=True)
class Token:
    mint: str

    name: str | None = None
    symbol: str | None = None
    description: str | None = None
    image_url: str | None = None
    decimals: int | None = None
    total_supply: Decimal | None = None

    creator_wallet: str | None = None
    launchpad_slug: str | None = None
    ecosystem: str = "solana"

    launched_at: datetime | None = None
    launch_slot: int | None = None
    launch_signature: str | None = None

    migrated_at: datetime | None = None
    migration_platform: str | None = None
    destination_dex_slug: str | None = None
    minutes_launch_to_migration: int | None = None
    pool_address: str | None = None

    is_qualified: bool = False
    qualified_at: datetime | None = None
    qualified_market_cap: Decimal | None = None
    qualified_threshold: Decimal | None = None
    qualification_rule: str | None = None
    qualification_evidence: dict[str, Any] = field(default_factory=dict)

    peak_market_cap: Decimal | None = None
    peak_at: datetime | None = None
    peak_tier: Decimal | None = None

    latest_market_cap: Decimal | None = None
    latest_liquidity_usd: Decimal | None = None
    latest_volume_24h_usd: Decimal | None = None
    latest_holder_count: int | None = None
    market_data_at: datetime | None = None

    website: str | None = None
    twitter: str | None = None
    telegram: str | None = None
    other_links: dict[str, str] = field(default_factory=dict)

    pipeline_stage: str = PipelineStage.DISCOVERY
    enriched_at: datetime | None = None
    analyzed_at: datetime | None = None
    data_sources: list[str] = field(default_factory=list)

    source: str | None = None
    source_type: str | None = None
    source_observed_at: datetime | None = None
    verification_status: str = "pending"
    confidence: float | None = None
    raw_reference: str | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None

    _MONEY_FIELDS = frozenset({
        "total_supply", "qualified_market_cap", "qualified_threshold",
        "peak_market_cap", "peak_tier", "latest_market_cap",
        "latest_liquidity_usd", "latest_volume_24h_usd",
    })


@dataclass(slots=True)
class TokenMilestone:
    token_mint: str
    kind: str
    threshold_usd: Decimal | None = None
    reached_at: datetime | None = None
    market_cap: Decimal | None = None
    liquidity_usd: Decimal | None = None
    volume_usd: Decimal | None = None
    holder_count: int | None = None
    token_age_minutes: int | None = None
    market_context: dict[str, Any] = field(default_factory=dict)

    source: str | None = None
    source_type: str | None = None
    source_observed_at: datetime | None = None
    verification_status: str = "pending"
    confidence: float | None = None

    created_at: datetime | None = None

    _MONEY_FIELDS = frozenset({"threshold_usd", "market_cap", "liquidity_usd", "volume_usd"})

    @property
    def doc_id(self) -> str:
        threshold = str(self.threshold_usd) if self.threshold_usd is not None else "na"
        return f"{self.kind}_{threshold}"


@dataclass(slots=True)
class TokenFeature:
    token_mint: str
    namespace: str
    key: str
    value: str | None = None
    numeric_value: float | None = None
    confidence: float | None = None
    source: str = "deterministic"
    model: str | None = None
    narrative_slug: str | None = None
    #: ``f"{namespace}.{key}"``, stored redundantly (not derivable from a
    #: query result on its own without this) so the trend engine can fetch
    #: only the handful of TRENDABLE fields per token instead of its entire
    #: features subcollection — added 2026-08-28 as a real cost fix: reading
    #: all 13-47 feature docs per qualified token, every trend engine run,
    #: was the single largest driver of a real Firestore billing incident.
    #: See app/trends/engine.py's TRENDABLE and FirestoreRepo.token_features_for_subjects.
    subject: str = ""
    created_at: datetime | None = None

    @property
    def doc_id(self) -> str:
        value_part = self.value if self.value is not None else "_"
        return f"{self.namespace}__{self.key}__{value_part}"[:200]


@dataclass(slots=True)
class ImageFeature:
    token_mint: str
    image_url: str | None = None
    image_hash: str | None = None
    categories: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)
    style: str | None = None
    has_text: bool | None = None
    text_content: str | None = None
    is_ai_generated_style: bool | None = None
    references_existing_meme: bool | None = None
    model: str | None = None
    confidence: float | None = None
    analyzed_at: datetime | None = None
    failure_reason: str | None = None


@dataclass(slots=True)
class SocialData:
    token_mint: str
    platform: str
    handle: str | None = None
    url: str | None = None
    followers: int | None = None
    posts: int | None = None
    engagement_score: float | None = None
    account_created_at: datetime | None = None
    observed_at: datetime | None = None
    exists: bool | None = None

    @property
    def doc_id(self) -> str:
        return self.platform


@dataclass(slots=True)
class MarketSnapshot:
    token_mint: str
    provider: str
    observed_at: datetime
    price_usd: Decimal | None = None
    market_cap: Decimal | None = None
    fully_diluted_valuation: Decimal | None = None
    liquidity_usd: Decimal | None = None
    volume_24h_usd: Decimal | None = None
    holder_count: int | None = None
    dex: str | None = None
    pair_address: str | None = None

    _MONEY_FIELDS = frozenset({
        "price_usd", "market_cap", "fully_diluted_valuation",
        "liquidity_usd", "volume_24h_usd",
    })
