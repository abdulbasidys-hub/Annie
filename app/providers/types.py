"""Provider-neutral data transfer objects.

Build.md §10 and §70 require that the intelligence engine never depend on a
provider's schema. These dataclasses are that boundary: adapters translate
vendor payloads into these, and nothing downstream of an adapter ever sees a
Bitquery GraphQL node, a Helius asset object or a DexScreener pair dict.

Every DTO carries :class:`Provenance`. That is not decoration — §21 requires
important facts to have a source, and attaching it at the boundary is the only
place where the origin is still known for certain. Once a value has been
merged, averaged or cached, its provenance can no longer be reconstructed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a piece of data came from and when."""

    provider: str
    operation: str
    observed_at: datetime
    raw_reference: str | None = None
    confidence: Decimal | None = None

    @property
    def source_type(self) -> str:
        """Coarse classification used in evidence display.

        Chain-derived facts outrank indexer aggregates, which outrank web
        search results. The UI shades evidence by this, so a conclusion resting
        on a blog post does not look like one resting on a transaction.
        """
        if self.provider in {"helius"}:
            return "blockchain"
        if self.provider in {"bitquery", "dexscreener", "birdeye"}:
            return "indexer"
        if self.provider in {"tavily"}:
            return "web"
        if self.provider in {"openai"}:
            return "model"
        return "other"


@dataclass(frozen=True, slots=True)
class TokenLaunch:
    """Stage-1 discovery record (§20). Deliberately minimal.

    Discovery runs over every launch on the network, so this must stay cheap.
    Anything requiring a second network call belongs in enrichment.
    """

    mint: str
    provenance: Provenance
    creator_wallet: str | None = None
    launchpad_slug: str | None = None
    launched_at: datetime | None = None
    slot: int | None = None
    signature: str | None = None
    name: str | None = None
    symbol: str | None = None


@dataclass(frozen=True, slots=True)
class MigrationEvent:
    """A token graduating from a launchpad to a DEX (§19)."""

    mint: str
    provenance: Provenance
    migrated_at: datetime | None = None
    from_platform: str | None = None
    to_dex_slug: str | None = None
    pool_address: str | None = None
    initial_liquidity_usd: Decimal | None = None


@dataclass(frozen=True, slots=True)
class MarketQuote:
    """A point-in-time market reading.

    ``market_cap`` and ``fully_diluted_valuation`` are kept apart because
    providers disagree about which one they report under the name "market cap".
    Conflating them is a common way to manufacture a token that never actually
    qualified.
    """

    mint: str
    provenance: Provenance
    price_usd: Decimal | None = None
    market_cap: Decimal | None = None
    fully_diluted_valuation: Decimal | None = None
    liquidity_usd: Decimal | None = None
    volume_24h_usd: Decimal | None = None
    holder_count: int | None = None
    dex_slug: str | None = None
    pair_address: str | None = None


@dataclass(frozen=True, slots=True)
class OhlcvBar:
    mint: str
    provenance: Provenance
    interval: str
    opened_at: datetime
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    volume_usd: Decimal | None = None
    market_cap_high: Decimal | None = None


@dataclass(frozen=True, slots=True)
class TokenMetadata:
    """Identity and links (§6)."""

    mint: str
    provenance: Provenance
    name: str | None = None
    symbol: str | None = None
    description: str | None = None
    image_url: str | None = None
    decimals: int | None = None
    total_supply: Decimal | None = None
    creator_wallet: str | None = None
    website: str | None = None
    twitter: str | None = None
    telegram: str | None = None
    other_links: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LaunchpadSignal:
    """Evidence that a launch platform exists and is active (§5, §18).

    Emitted by discovery whenever a launch is attributed to a program the
    system has not catalogued. This is the mechanism by which a new launchpad
    becomes a research subject without anyone editing code.
    """

    slug: str
    provenance: Provenance
    name: str | None = None
    program_id: str | None = None
    launches_observed: int = 0
    window_start: datetime | None = None
    window_end: datetime | None = None


@dataclass(frozen=True, slots=True)
class WebResult:
    """One external web finding (§13)."""

    title: str
    url: str
    provenance: Provenance
    snippet: str | None = None
    published_at: datetime | None = None
    score: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ImageAnalysis:
    """Structured characteristics of a token image (§15)."""

    mint: str
    provenance: Provenance
    categories: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)
    style: str | None = None
    has_text: bool | None = None
    text_content: str | None = None
    is_ai_generated_style: bool | None = None
    references_existing_meme: bool | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class HolderStats:
    mint: str
    provenance: Provenance
    holder_count: int | None = None
    top_10_share: Decimal | None = None
    creator_share: Decimal | None = None
