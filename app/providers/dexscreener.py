"""DexScreener adapter — secondary market data and cross-validation (§11).

Public endpoints require no key, so this adapter is enabled in every
deployment. That makes it the natural second opinion for §21's cross-validation
requirement: any deployment can corroborate a market cap without extra setup.

Used for verification, discovery and gap-filling — never as the primary basis
for a qualification decision, because its market-cap figures are derived from
pair liquidity rather than read from chain.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.providers.http import HttpProvider
from app.providers.types import MarketQuote, Provenance

BASE_URL = "https://api.dexscreener.com"


def _dec(value: Any) -> Decimal | None:
    """Parse to Decimal, or ``None``.

    Returning ``None`` rather than ``Decimal(0)`` on unparseable input is
    load-bearing: a zero would flow into qualification and milestone maths as a
    real measurement of "worthless", whereas None correctly means "unknown".
    """
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


class DexScreenerAdapter(HttpProvider):
    """Implements ``MarketDataProvider`` and ``DexDataProvider``."""

    name = "dexscreener"
    cost_per_request_usd = 0.0  # free tier

    def __init__(self, api_key: str = "") -> None:
        headers = {"Accept": "application/json"}
        if api_key.strip():
            headers["X-API-KEY"] = api_key.strip()
        super().__init__(
            base_url=BASE_URL,
            headers=headers,
            # Documented public limit is 300 req/min for the token endpoints.
            # Held at 4/s to leave room for bursts from parallel workers.
            rate_per_second=4.0,
            burst=8,
            timeout_seconds=15.0,
        )
        self._api_key = api_key.strip()

    def is_configured(self) -> bool:
        return True  # public endpoints need no credential

    async def _healthcheck(self) -> bool:
        # SOL's mint — always present, cheap, and proves parsing works too.
        data = await self.request(
            "GET",
            "/latest/dex/tokens/So11111111111111111111111111111111111111112",
            operation="healthcheck",
            allow_404=True,
        )
        return bool(data and data.get("pairs"))

    # -- MarketDataProvider ---------------------------------------------------

    async def get_quote(self, mint: str) -> MarketQuote | None:
        quotes = await self.get_pairs(mint)
        if not quotes:
            return None
        return _best_pair(quotes)

    async def get_quotes(self, mints: list[str]) -> dict[str, MarketQuote]:
        """Batch lookup. DexScreener accepts up to 30 comma-separated mints."""
        out: dict[str, MarketQuote] = {}
        for chunk in _chunks(mints, 30):
            data = await self.request(
                "GET",
                f"/latest/dex/tokens/{','.join(chunk)}",
                operation="get_quotes",
                allow_404=True,
            )
            if not data:
                continue
            by_mint: dict[str, list[MarketQuote]] = {}
            for pair in data.get("pairs") or []:
                quote = self._parse_pair(pair)
                if quote is not None:
                    by_mint.setdefault(quote.mint, []).append(quote)
            for mint, quotes in by_mint.items():
                out[mint] = _best_pair(quotes)
        return out

    async def get_ohlcv(self, *_: Any, **__: Any) -> list:
        """Not supported.

        DexScreener has no public OHLCV endpoint. Returning an empty list would
        be indistinguishable from "no trading activity" and would corrupt any
        peak-market-cap calculation that fell back to it, so this refuses
        instead. Bitquery covers OHLCV (§10).
        """
        raise NotImplementedError(
            "DexScreener exposes no public OHLCV endpoint; use the Bitquery adapter."
        )

    async def get_peak_market_cap(self, *_: Any, **__: Any) -> None:
        raise NotImplementedError(
            "DexScreener cannot answer historical peaks; use the Bitquery adapter."
        )

    # -- DexDataProvider ------------------------------------------------------

    async def get_pairs(self, mint: str) -> list[MarketQuote]:
        data = await self.request(
            "GET", f"/latest/dex/tokens/{mint}", operation="get_pairs", allow_404=True
        )
        if not data:
            return []
        parsed = [self._parse_pair(p) for p in (data.get("pairs") or [])]
        return [p for p in parsed if p is not None]

    async def search(self, query: str) -> list[MarketQuote]:
        """Token/pair discovery by free text — used to fill identification gaps."""
        data = await self.request(
            "GET",
            "/latest/dex/search",
            operation="search",
            params={"q": query},
            allow_404=True,
        )
        if not data:
            return []
        parsed = [self._parse_pair(p) for p in (data.get("pairs") or [])]
        return [p for p in parsed if p is not None]

    async def get_new_pairs(self, *_: Any, **__: Any) -> list[MarketQuote]:
        raise NotImplementedError(
            "DexScreener's new-pairs feed is not part of the documented public API; "
            "use the Bitquery adapter for launch discovery."
        )

    # -- parsing --------------------------------------------------------------

    def _parse_pair(self, pair: dict[str, Any]) -> MarketQuote | None:
        base = pair.get("baseToken") or {}
        mint = base.get("address")
        if not mint:
            return None
        if pair.get("chainId") not in (None, "solana"):
            return None

        provenance = Provenance(
            provider=self.name,
            operation="pair",
            observed_at=datetime.now(timezone.utc),
            raw_reference=pair.get("pairAddress"),
        )

        # DexScreener reports both `marketCap` and `fdv`; they differ for tokens
        # with locked or unminted supply. Kept separate per the MarketQuote
        # contract rather than coalesced.
        return MarketQuote(
            mint=mint,
            provenance=provenance,
            price_usd=_dec(pair.get("priceUsd")),
            market_cap=_dec(pair.get("marketCap")),
            fully_diluted_valuation=_dec(pair.get("fdv")),
            liquidity_usd=_dec((pair.get("liquidity") or {}).get("usd")),
            volume_24h_usd=_dec((pair.get("volume") or {}).get("h24")),
            dex_slug=pair.get("dexId"),
            pair_address=pair.get("pairAddress"),
        )


def _best_pair(quotes: list[MarketQuote]) -> MarketQuote:
    """Deepest-liquidity pair wins.

    Not the highest price or newest pair: a thin pool can quote an absurd price
    that would qualify a token that never traded meaningfully. Liquidity depth
    is the most manipulation-resistant of the fields available here.
    """
    return max(quotes, key=lambda q: q.liquidity_usd or Decimal(0))


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]
