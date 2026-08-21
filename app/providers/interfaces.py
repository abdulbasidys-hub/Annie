"""Provider interfaces (Build.md §70).

Structural ``Protocol`` classes rather than inheritance, so an adapter is
conformant by shape. That matters for the stated goal — providers must be
replaceable without rewriting the trend engine, research engine, Annie, the
frontend or the database — because it means a replacement adapter needs no
import from, or knowledge of, the thing it replaces.

Each protocol is narrow on purpose. Bitquery could plausibly satisfy four of
these at once; splitting them means the day it stops being the right choice for
OHLCV, only that one capability moves.

Every method is permitted to raise :class:`ProviderError`. No method may return
a fabricated or default-valued result to paper over a failure — a missing
market cap must arrive as ``None`` so that qualification can decline to run,
not as ``0`` which reads as a real measurement.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from app.providers.types import (
    HolderStats,
    ImageAnalysis,
    LaunchpadSignal,
    MarketQuote,
    MigrationEvent,
    OhlcvBar,
    TokenLaunch,
    TokenMetadata,
    WebResult,
)


class ProviderError(RuntimeError):
    """A provider call failed.

    ``retryable`` distinguishes transport hiccups from answers. A 404 for an
    unknown mint is not retryable and must not trigger failover — failing over
    on it would let a second provider invent data the first correctly said it
    did not have.
    """

    def __init__(
        self,
        provider: str,
        operation: str,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        self.provider = provider
        self.operation = operation
        self.retryable = retryable
        self.status_code = status_code
        super().__init__(f"[{provider}.{operation}] {message}")


class ProviderRateLimited(ProviderError):
    def __init__(
        self, provider: str, operation: str, retry_after_seconds: float | None = None
    ) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            provider, operation, "rate limited", retryable=True, status_code=429
        )


class ProviderUnavailable(ProviderError):
    """The adapter is not configured in this deployment.

    Distinct from a failed call. Callers check capability first; this exists to
    catch the case where they forgot.
    """


@runtime_checkable
class BaseProvider(Protocol):
    """Common surface every adapter exposes."""

    name: str

    def is_configured(self) -> bool:
        """True when this deployment has the credentials this adapter needs."""
        ...

    async def healthcheck(self) -> bool:
        """Cheapest call that proves the credential works. Never raises."""
        ...

    async def aclose(self) -> None: ...


@runtime_checkable
class BlockchainProvider(BaseProvider, Protocol):
    """Direct chain access — the highest-trust source (§9.1)."""

    async def get_token_metadata(self, mint: str) -> TokenMetadata | None: ...

    async def get_token_metadata_batch(
        self, mints: list[str]
    ) -> dict[str, TokenMetadata]: ...

    async def get_holder_stats(self, mint: str) -> HolderStats | None: ...

    async def get_creator_wallet(self, mint: str) -> str | None:
        """Authoritative deployer of a mint.

        Indexers frequently report the fee payer or a proxy instead. §17's
        entire creator model rests on this being the real deployer, so it is
        deliberately a chain call rather than an indexer field.
        """
        ...

    async def verify_token_exists(self, mint: str) -> bool: ...


@runtime_checkable
class LaunchDataProvider(BaseProvider, Protocol):
    """Stage-1 discovery across the whole launch ecosystem (§5, §20)."""

    async def discover_launches(
        self,
        since: datetime,
        until: datetime | None = None,
        limit: int = 1000,
        launchpad_slug: str | None = None,
    ) -> list[TokenLaunch]:
        """Launches in a window.

        ``launchpad_slug=None`` means *every* platform, including ones the
        system has never seen. §5 forbids hardcoding Pump.fun as the origin of
        success, so the unfiltered call is the normal path and filtering is the
        exception.
        """
        ...

    async def discover_launchpads(
        self, since: datetime, until: datetime | None = None
    ) -> list[LaunchpadSignal]:
        """Launch programs active in a window, including uncatalogued ones."""
        ...

    async def get_migrations(
        self, since: datetime, until: datetime | None = None, limit: int = 1000
    ) -> list[MigrationEvent]: ...


@runtime_checkable
class MarketDataProvider(BaseProvider, Protocol):
    """Prices, market caps, liquidity, volume."""

    async def get_quote(self, mint: str) -> MarketQuote | None: ...

    async def get_quotes(self, mints: list[str]) -> dict[str, MarketQuote]: ...

    async def get_ohlcv(
        self,
        mint: str,
        interval: str,
        since: datetime,
        until: datetime | None = None,
    ) -> list[OhlcvBar]: ...

    async def get_peak_market_cap(
        self, mint: str, since: datetime, until: datetime | None = None
    ) -> tuple[Decimal, datetime] | None:
        """Highest market cap in a window, with when it happened.

        A dedicated method because §7 wants the peak milestone without storing
        a full time series — the provider can answer this with an aggregate far
        more cheaply than we can by fetching and scanning every bar.
        """
        ...


@runtime_checkable
class DexDataProvider(BaseProvider, Protocol):
    """Pool and pair level information (§19)."""

    async def get_pairs(self, mint: str) -> list[MarketQuote]: ...

    async def get_new_pairs(
        self, since: datetime, dex_slug: str | None = None, limit: int = 500
    ) -> list[MarketQuote]: ...


@runtime_checkable
class TokenMetadataProvider(BaseProvider, Protocol):
    async def get_metadata(self, mint: str) -> TokenMetadata | None: ...


@runtime_checkable
class SocialDataProvider(BaseProvider, Protocol):
    """Supplementary only (§14). Absence never disqualifies a token."""

    async def get_profile(self, platform: str, handle: str) -> dict | None: ...

    async def check_exists(self, platform: str, handle: str) -> bool | None: ...


@runtime_checkable
class WebResearchProvider(BaseProvider, Protocol):
    """External context (§13). Never a substitute for chain data."""

    async def search(
        self,
        query: str,
        max_results: int = 5,
        recency_days: int | None = None,
    ) -> list[WebResult]: ...


@runtime_checkable
class ImageAnalysisProvider(BaseProvider, Protocol):
    """Structured characteristics from token images (§15)."""

    async def analyze(self, mint: str, image_url: str) -> ImageAnalysis | None: ...
