"""Provider registry, failover and cross-validation (§49, §21, §70).

This module is the only place in the application that knows which vendor
implements which capability. Everything downstream asks the registry for a
``MarketDataProvider`` and receives one; swapping Bitquery for something else
is a change to :meth:`ProviderRegistry._build`, not to the trend engine, the
research engine, Annie, the frontend or the schema.

Two spec rules are enforced here rather than left to callers:

§49 — failover order is primary, retry, secondary, then *queue for
verification*. Critical information is never silently replaced with less
reliable information: a value obtained from a fallback is tagged with the
provider that produced it and a lower verification status, so a market cap
sourced from a secondary during a Bitquery outage can be found later.

§21 — when two providers disagree materially, neither is silently chosen. The
disagreement is returned alongside the values and persisted as a
``DataConflict``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import structlog

from app.config import CapabilityStatus, Settings, get_settings
from app.db.enums import VerificationStatus
from app.providers.birdeye import BirdeyeAdapter
from app.providers.bitquery import BitqueryAdapter
from app.providers.dexscreener import DexScreenerAdapter
from app.providers.helius import HeliusAdapter
from app.providers.interfaces import ProviderError, ProviderUnavailable
from app.providers.openai_provider import OpenAIImageAnalyst, OpenAIReasoner
from app.providers.tavily import TavilyAdapter
from app.providers.types import MarketQuote

log = structlog.get_logger(__name__)

#: Relative difference above which two market caps are a *material*
#: disagreement rather than ordinary timing noise between indexers.
#:
#: 15% is chosen against the qualification ladder: the tiers in §4 are 2.5x
#: apart at the narrowest, so a threshold well below that cannot cause tokens
#: to be mis-tiered, while still being wide enough that two providers sampling
#: a volatile token seconds apart do not constantly trip it.
MATERIAL_DIVERGENCE = Decimal("0.15")


@dataclass(slots=True)
class ResolvedQuote:
    """A market quote plus the story of how it was obtained.

    The ``conflict`` and ``used_fallback`` fields are why this type exists
    instead of returning a bare ``MarketQuote``. A caller that writes a
    qualification decision needs to record *which* provider it trusted and
    whether anything disagreed — that is the evidence §4 requires it to store.
    """

    quote: MarketQuote | None
    provider: str | None
    verification_status: str
    used_fallback: bool = False
    conflict: dict[str, Any] | None = None
    alternatives: list[MarketQuote] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class ProviderRegistry:
    """Lazily constructs and hands out adapters."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._instances: dict[str, Any] = {}

    @property
    def settings(self) -> Settings:
        return self._settings

    # -- construction ---------------------------------------------------------

    def _build(self, key: str) -> Any:
        s = self._settings
        if key == "helius":
            return HeliusAdapter(s.helius_api_key, s.helius_rpc_url)
        if key == "bitquery":
            return BitqueryAdapter(s.bitquery_api_key)
        if key == "dexscreener":
            return DexScreenerAdapter(s.dexscreener_api_key)
        if key == "birdeye":
            return BirdeyeAdapter(s.birdeye_api_key)
        if key == "tavily":
            return TavilyAdapter(s.tavily_api_key)
        if key == "openai_images":
            return OpenAIImageAnalyst(s.openai_api_key, s.openai_vision_model)
        if key == "openai_reasoning":
            return OpenAIReasoner(
                s.openai_api_key,
                reasoning_model=s.openai_reasoning_model,
                cheap_model=s.openai_cheap_model,
            )
        raise KeyError(f"Unknown provider {key!r}")

    def get(self, key: str) -> Any:
        if key not in self._instances:
            self._instances[key] = self._build(key)
        return self._instances[key]

    # -- named capabilities ---------------------------------------------------
    # Callers use these, not `get`, so the mapping from capability to vendor
    # stays in one place.
    #
    # This deployment (Build.md §9.2 amendment) runs discovery and market data
    # off Helius + DexScreener rather than Bitquery: DexScreener needs no
    # credential and Helius is already the highest-trust chain source, so
    # between them nothing paid is required to boot. BitqueryAdapter is still
    # built and still usable — set BITQUERY_API_KEY and it joins
    # ``market_secondaries`` automatically for extra cross-validation — but it
    # is not what either property below returns by default.

    @property
    def blockchain(self) -> HeliusAdapter:
        return self.get("helius")

    @property
    def launches(self) -> HeliusAdapter:
        """Stage-1 discovery (§5, §20).

        Helius here means scanning known launchpad program IDs via
        ``getSignaturesForAddress`` + parsed-transaction inspection, not a
        full indexed launch feed — see
        :meth:`app.providers.helius.HeliusAdapter.discover_launches` for
        exactly what this can and cannot see.
        """
        return self.get("helius")

    @property
    def market_primary(self) -> DexScreenerAdapter:
        return self.get("dexscreener")

    @property
    def market_secondaries(self) -> list[Any]:
        """Ordered fallbacks / cross-validators, each skipped if unconfigured."""
        return [self.get("birdeye"), self.get("bitquery")]

    @property
    def web_research(self) -> TavilyAdapter:
        return self.get("tavily")

    @property
    def image_analysis(self) -> OpenAIImageAnalyst:
        return self.get("openai_images")

    @property
    def reasoning(self) -> OpenAIReasoner:
        return self.get("openai_reasoning")

    # -- resolution -----------------------------------------------------------

    async def resolve_market_cap(
        self, mint: str, *, cross_validate: bool = True
    ) -> ResolvedQuote:
        """Get a market cap, with failover and disagreement detection.

        The order is deliberate and matches §49. Note what this method never
        does: it never averages two providers, and it never returns a value
        without saying where it came from. Averaging would produce a number no
        provider actually reported, which cannot be verified afterwards.
        """
        errors: list[str] = []
        collected: list[MarketQuote] = []
        primary_quote: MarketQuote | None = None

        # 1. Primary.
        if self._settings.is_available("market_primary"):
            try:
                primary_quote = await self.market_primary.get_quote(mint)
                if primary_quote is not None:
                    collected.append(primary_quote)
            except ProviderError as exc:
                errors.append(str(exc))
                log.info("primary_market_failed", mint=mint, error=str(exc))

        # 2. Secondaries — always consulted when cross-validating, not only on
        #    primary failure. §21's cross-verification is only meaningful if a
        #    second opinion is sought while the first one is working.
        if cross_validate or primary_quote is None:
            for secondary in self.market_secondaries:
                if not secondary.is_configured():
                    continue
                try:
                    quote = await secondary.get_quote(mint)
                    if quote is not None:
                        collected.append(quote)
                except (ProviderError, ProviderUnavailable) as exc:
                    errors.append(str(exc))
                except NotImplementedError:
                    continue

        with_market_cap = [q for q in collected if q.market_cap is not None]
        if not with_market_cap:
            return ResolvedQuote(
                quote=None,
                provider=None,
                verification_status=VerificationStatus.PENDING,
                errors=errors,
            )

        chosen = _prefer_primary(with_market_cap, self.market_primary.name)
        used_fallback = chosen.provenance.provider != self.market_primary.name
        conflict = _detect_conflict(with_market_cap)

        if conflict is not None:
            status = VerificationStatus.DISPUTED
        elif len(with_market_cap) > 1:
            status = VerificationStatus.CROSS_VERIFIED
        elif used_fallback:
            # A lone secondary reading is usable but explicitly not verified —
            # §49's "queue for verification" state.
            status = VerificationStatus.UNVERIFIED
        else:
            status = VerificationStatus.VERIFIED

        return ResolvedQuote(
            quote=chosen,
            provider=chosen.provenance.provider,
            verification_status=status,
            used_fallback=used_fallback,
            conflict=conflict,
            alternatives=[q for q in with_market_cap if q is not chosen],
            errors=errors,
        )

    # -- health ---------------------------------------------------------------

    async def health_snapshot(self) -> list[dict[str, Any]]:
        """Live probe of every configured adapter, for the System Health page."""
        out: list[dict[str, Any]] = []
        checks = {
            "helius": "blockchain",
            "dexscreener": "market_primary",
            "bitquery": "market_bitquery",
            "birdeye": "market_birdeye",
            "tavily": "web_research",
            "openai_images": "ai",
        }
        for key, capability_key in checks.items():
            adapter = self.get(key)
            capability = self._settings.capability(capability_key)
            configured = adapter.is_configured()
            reachable = await adapter.healthcheck() if configured else False
            out.append(
                {
                    "provider": adapter.name if key != "openai_images" else "openai",
                    "adapter": key,
                    "capability": capability_key,
                    "capability_label": capability.label,
                    "tier": capability.tier.value,
                    "configured": configured,
                    "reachable": reachable,
                    "status": _status_word(configured, reachable),
                    "missing_env_vars": list(capability.missing(self._settings)),
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        return out

    async def aclose(self) -> None:
        for adapter in self._instances.values():
            try:
                await adapter.aclose()
            except Exception:
                log.warning("adapter_close_failed", exc_info=True)
        self._instances.clear()


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------


def _prefer_primary(quotes: list[MarketQuote], primary_name: str) -> MarketQuote:
    """Pick the primary's reading when present, else the deepest liquidity.

    Not the median and not the mean — see :meth:`resolve_market_cap`. When the
    primary is absent, liquidity depth is the tiebreaker for the same reason
    DexScreener uses it to pick a pair: it is the hardest field to manipulate.
    """
    for quote in quotes:
        if quote.provenance.provider == primary_name:
            return quote
    return max(quotes, key=lambda q: q.liquidity_usd or Decimal(0))


def _detect_conflict(quotes: list[MarketQuote]) -> dict[str, Any] | None:
    """Return a conflict record when providers disagree materially (§21)."""
    values = [
        (q.provenance.provider, q.market_cap)
        for q in quotes
        if q.market_cap is not None and q.market_cap > 0
    ]
    if len(values) < 2:
        return None

    caps = [v for _, v in values]
    lowest, highest = min(caps), max(caps)
    if lowest == 0:
        return None

    divergence = (highest - lowest) / lowest
    if divergence < MATERIAL_DIVERGENCE:
        return None

    return {
        "field": "market_cap",
        "divergence": str(divergence),
        "threshold": str(MATERIAL_DIVERGENCE),
        "values": [
            {"provider": provider, "value": str(value)} for provider, value in values
        ],
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }


def _status_word(configured: bool, reachable: bool) -> str:
    if not configured:
        return "disabled"
    return "ok" if reachable else "down"


_registry: ProviderRegistry | None = None


def get_registry() -> ProviderRegistry:
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry


async def close_registry() -> None:
    global _registry
    if _registry is not None:
        await _registry.aclose()
    _registry = None
