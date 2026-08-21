"""Birdeye adapter — optional secondary market data (§12).

Build.md §12 ends with "do not make the entire system dependent on Birdeye", so
this adapter is optional everywhere: absence reduces the breadth of
cross-validation and nothing else. No pipeline stage requires it.

Endpoint paths follow Birdeye's public `defi/*` API. Response shapes vary by
plan tier, so the parsers here read defensively and return ``None`` for fields
they cannot find rather than assuming a shape.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.providers.http import HttpProvider
from app.providers.types import HolderStats, MarketQuote, OhlcvBar, Provenance

BASE_URL = "https://public-api.birdeye.so"

#: Birdeye's interval vocabulary. Mapped explicitly so callers use the neutral
#: names from the interface and a vendor rename touches only this dict.
INTERVAL_MAP = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1H",
    "4h": "4H",
    "1d": "1D",
}


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


class BirdeyeAdapter(HttpProvider):
    """Implements ``MarketDataProvider`` (partially) for cross-validation."""

    name = "birdeye"
    cost_per_request_usd = 0.0  # credit-metered, not per-dollar; see note below

    def __init__(self, api_key: str) -> None:
        self._api_key = (api_key or "").strip()
        super().__init__(
            base_url=BASE_URL,
            headers={
                "X-API-KEY": self._api_key,
                "x-chain": "solana",
                "Accept": "application/json",
            },
            rate_per_second=8.0,
            burst=10,
            timeout_seconds=20.0,
        )

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def _healthcheck(self) -> bool:
        data = await self.request(
            "GET",
            "/defi/price",
            operation="healthcheck",
            params={"address": "So11111111111111111111111111111111111111112"},
            allow_404=True,
        )
        return bool(data and data.get("success"))

    # -- MarketDataProvider ---------------------------------------------------

    async def get_quote(self, mint: str) -> MarketQuote | None:
        data = await self.request(
            "GET",
            "/defi/token_overview",
            operation="get_quote",
            params={"address": mint},
            allow_404=True,
        )
        payload = (data or {}).get("data")
        if not payload:
            return None
        return self._parse_overview(mint, payload)

    async def get_quotes(self, mints: list[str]) -> dict[str, MarketQuote]:
        """Multi-price endpoint.

        Returns price only — no liquidity or market cap — so quotes from here
        are intentionally sparse. Callers needing a market cap must use
        :meth:`get_quote` per mint; silently filling the gap with price times a
        guessed supply is exactly the fabrication §21 forbids.
        """
        out: dict[str, MarketQuote] = {}
        for chunk in _chunks(mints, 50):
            data = await self.request(
                "GET",
                "/defi/multi_price",
                operation="get_quotes",
                params={"list_address": ",".join(chunk)},
                allow_404=True,
            )
            payload = (data or {}).get("data") or {}
            now = datetime.now(timezone.utc)
            for mint, entry in payload.items():
                if not isinstance(entry, dict):
                    continue
                out[mint] = MarketQuote(
                    mint=mint,
                    provenance=Provenance(self.name, "multi_price", now),
                    price_usd=_dec(entry.get("value")),
                )
        return out

    async def get_ohlcv(
        self,
        mint: str,
        interval: str,
        since: datetime,
        until: datetime | None = None,
    ) -> list[OhlcvBar]:
        vendor_interval = INTERVAL_MAP.get(interval)
        if vendor_interval is None:
            raise ValueError(
                f"Unsupported interval {interval!r}; expected one of {sorted(INTERVAL_MAP)}"
            )
        until = until or datetime.now(timezone.utc)
        data = await self.request(
            "GET",
            "/defi/ohlcv",
            operation="get_ohlcv",
            params={
                "address": mint,
                "type": vendor_interval,
                "time_from": int(since.timestamp()),
                "time_to": int(until.timestamp()),
            },
            allow_404=True,
        )
        items = ((data or {}).get("data") or {}).get("items") or []
        bars: list[OhlcvBar] = []
        for item in items:
            opened = item.get("unixTime")
            if opened is None:
                continue
            bars.append(
                OhlcvBar(
                    mint=mint,
                    provenance=Provenance(
                        self.name, "get_ohlcv", datetime.now(timezone.utc)
                    ),
                    interval=interval,
                    opened_at=datetime.fromtimestamp(opened, tz=timezone.utc),
                    open=_dec(item.get("o")),
                    high=_dec(item.get("h")),
                    low=_dec(item.get("l")),
                    close=_dec(item.get("c")),
                    volume_usd=_dec(item.get("v")),
                )
            )
        return bars

    async def get_peak_market_cap(
        self, mint: str, since: datetime, until: datetime | None = None
    ) -> tuple[Decimal, datetime] | None:
        """Derive a peak from price bars and current supply.

        This is an *approximation* and is labelled as such wherever it is
        stored: it applies today's circulating supply to historical prices. For
        tokens whose supply changed it will be wrong. Bitquery is the primary
        source for peaks (§10); this exists only to corroborate one.
        """
        overview = await self.get_quote(mint)
        if overview is None or overview.price_usd in (None, Decimal(0)):
            return None
        if overview.market_cap is None:
            return None

        supply_ratio = overview.market_cap / overview.price_usd
        bars = await self.get_ohlcv(mint, "1h", since, until)
        if not bars:
            return None

        best: tuple[Decimal, datetime] | None = None
        for bar in bars:
            if bar.high is None:
                continue
            implied = bar.high * supply_ratio
            if best is None or implied > best[0]:
                best = (implied, bar.opened_at)
        return best

    # -- extras ---------------------------------------------------------------

    async def get_holder_stats(self, mint: str) -> HolderStats | None:
        data = await self.request(
            "GET",
            "/defi/token_overview",
            operation="get_holder_stats",
            params={"address": mint},
            allow_404=True,
        )
        payload = (data or {}).get("data")
        if not payload:
            return None
        holders = payload.get("holder")
        return HolderStats(
            mint=mint,
            provenance=Provenance(
                self.name, "get_holder_stats", datetime.now(timezone.utc)
            ),
            holder_count=int(holders) if isinstance(holders, (int, float)) else None,
        )

    # -- parsing --------------------------------------------------------------

    def _parse_overview(self, mint: str, payload: dict[str, Any]) -> MarketQuote:
        holders = payload.get("holder")
        return MarketQuote(
            mint=mint,
            provenance=Provenance(
                provider=self.name,
                operation="token_overview",
                observed_at=datetime.now(timezone.utc),
            ),
            price_usd=_dec(payload.get("price")),
            market_cap=_dec(payload.get("mc") or payload.get("marketCap")),
            fully_diluted_valuation=_dec(payload.get("fdv")),
            liquidity_usd=_dec(payload.get("liquidity")),
            volume_24h_usd=_dec(
                (payload.get("v24hUSD") or payload.get("volume24hUSD"))
            ),
            holder_count=int(holders) if isinstance(holders, (int, float)) else None,
        )


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]
