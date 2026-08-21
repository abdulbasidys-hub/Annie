"""Bitquery adapter — primary indexed market, launch and DEX data (§10).

Implements ``LaunchDataProvider``, ``MarketDataProvider`` and
``DexDataProvider``. Bitquery satisfies three interfaces today; they are kept
separate so that replacing it for any one of them is a local change (§70).

The GraphQL documents live in :mod:`app.providers.bitquery_queries` and need
verification against a live account — see that module's docstring. This file
contains only transport and the mapping from vendor payloads to the neutral
DTOs, which is the part that must not change when the schema does.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

from app.providers.bitquery_queries import (
    HEALTHCHECK_QUERY,
    KNOWN_DEX_PROGRAMS,
    KNOWN_LAUNCH_PROGRAMS,
    LATEST_QUOTE_QUERY,
    LAUNCHES_QUERY,
    LAUNCHPAD_ACTIVITY_QUERY,
    MIGRATIONS_QUERY,
    NEW_POOLS_QUERY,
    OHLCV_QUERY,
    PEAK_MARKETCAP_QUERY,
)
from app.providers.http import HttpProvider
from app.providers.interfaces import ProviderError
from app.providers.types import (
    LaunchpadSignal,
    MarketQuote,
    MigrationEvent,
    OhlcvBar,
    Provenance,
    TokenLaunch,
)

log = structlog.get_logger(__name__)

BASE_URL = "https://streaming.bitquery.io"

#: Interval names to minute counts for the OHLCV query.
INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}

#: Quote mints whose pools we treat as real USD-denominated markets. A pool
#: quoted in an obscure token can imply an enormous market cap that does not
#: correspond to any liquidity a buyer could access.
TRUSTED_QUOTE_MINTS = {
    "So11111111111111111111111111111111111111112",  # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


class BitqueryAdapter(HttpProvider):
    name = "bitquery"
    #: Bitquery bills in points, not per request. A per-request dollar figure
    #: would be fiction, so cost is reported as request volume on System Health
    #: and this stays zero rather than inventing a rate.
    cost_per_request_usd = 0.0

    def __init__(self, api_key: str) -> None:
        self._api_key = (api_key or "").strip()
        super().__init__(
            base_url=BASE_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            rate_per_second=2.0,
            burst=4,
            timeout_seconds=60.0,  # aggregate queries over wide windows are slow
            max_retries=2,
        )

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def _healthcheck(self) -> bool:
        data = await self._graphql(HEALTHCHECK_QUERY, {}, operation="healthcheck")
        return bool(data)

    # -- transport ------------------------------------------------------------

    async def _graphql(
        self, query: str, variables: dict[str, Any], *, operation: str
    ) -> dict[str, Any] | None:
        data = await self.request(
            "POST",
            "/eap",
            operation=operation,
            json={"query": query, "variables": variables},
        )
        if data is None:
            return None

        # GraphQL returns HTTP 200 with an `errors` array. Treating that as
        # success is how a broken query becomes a silently empty dataset — and
        # an empty launch feed looks exactly like a quiet day on chain.
        if data.get("errors"):
            messages = "; ".join(
                str(e.get("message", e)) for e in data["errors"][:3]
            )
            raise ProviderError(
                self.name,
                operation,
                f"GraphQL errors: {messages}",
                retryable=False,
            )
        return data.get("data")

    # -- LaunchDataProvider ---------------------------------------------------

    async def discover_launches(
        self,
        since: datetime,
        until: datetime | None = None,
        limit: int = 1000,
        launchpad_slug: str | None = None,
    ) -> list[TokenLaunch]:
        until = until or datetime.now(timezone.utc)
        data = await self._graphql(
            LAUNCHES_QUERY,
            {
                "since": _iso(since),
                "until": _iso(until),
                "limit": min(limit, 5000),
            },
            operation="discover_launches",
        )
        rows = _path(data, "Solana", "TokenSupplyUpdates") or []
        now = datetime.now(timezone.utc)

        launches: list[TokenLaunch] = []
        for row in rows:
            update = row.get("TokenSupplyUpdate") or {}
            currency = update.get("Currency") or {}
            mint = currency.get("MintAddress")
            if not mint:
                continue

            program = (row.get("Instruction") or {}).get("Program") or {}
            program_address = program.get("Address")
            slug = _launchpad_slug(program_address, program.get("Name"))

            if launchpad_slug is not None and slug != launchpad_slug:
                continue

            block = row.get("Block") or {}
            transaction = row.get("Transaction") or {}

            launches.append(
                TokenLaunch(
                    mint=mint,
                    provenance=Provenance(
                        provider=self.name,
                        operation="discover_launches",
                        observed_at=now,
                        raw_reference=transaction.get("Signature"),
                    ),
                    creator_wallet=transaction.get("Signer"),
                    launchpad_slug=slug,
                    launched_at=_parse_time(block.get("Time")),
                    slot=_int(block.get("Slot")),
                    signature=transaction.get("Signature"),
                    name=currency.get("Name"),
                    symbol=currency.get("Symbol"),
                )
            )
        return launches

    async def discover_launchpads(
        self, since: datetime, until: datetime | None = None
    ) -> list[LaunchpadSignal]:
        until = until or datetime.now(timezone.utc)
        data = await self._graphql(
            LAUNCHPAD_ACTIVITY_QUERY,
            {"since": _iso(since), "until": _iso(until)},
            operation="discover_launchpads",
        )
        rows = _path(data, "Solana", "Instructions") or []
        now = datetime.now(timezone.utc)

        signals: list[LaunchpadSignal] = []
        for row in rows:
            program = (row.get("Instruction") or {}).get("Program") or {}
            address = program.get("Address")
            if not address:
                continue
            block = row.get("Block") or {}
            signals.append(
                LaunchpadSignal(
                    slug=_launchpad_slug(address, program.get("Name")),
                    name=program.get("Name"),
                    program_id=address,
                    launches_observed=_int(row.get("count")) or 0,
                    window_start=_parse_time(block.get("first_seen")) or since,
                    window_end=_parse_time(block.get("last_seen")) or until,
                    provenance=Provenance(
                        provider=self.name,
                        operation="discover_launchpads",
                        observed_at=now,
                        raw_reference=address,
                    ),
                )
            )
        return signals

    async def get_migrations(
        self, since: datetime, until: datetime | None = None, limit: int = 1000
    ) -> list[MigrationEvent]:
        until = until or datetime.now(timezone.utc)
        data = await self._graphql(
            MIGRATIONS_QUERY,
            {"since": _iso(since), "until": _iso(until), "limit": min(limit, 5000)},
            operation="get_migrations",
        )
        rows = _path(data, "Solana", "DEXPools") or []
        now = datetime.now(timezone.utc)

        events: list[MigrationEvent] = []
        for row in rows:
            pool = row.get("Pool") or {}
            market = pool.get("Market") or {}
            base = market.get("BaseCurrency") or {}
            mint = base.get("MintAddress")
            if not mint:
                continue

            dex = pool.get("Dex") or {}
            quote = pool.get("Quote") or {}

            events.append(
                MigrationEvent(
                    mint=mint,
                    provenance=Provenance(
                        provider=self.name,
                        operation="get_migrations",
                        observed_at=now,
                        raw_reference=(row.get("Transaction") or {}).get("Signature"),
                    ),
                    migrated_at=_parse_time((row.get("Block") or {}).get("Time")),
                    from_platform=None,  # resolved by joining the token's launchpad
                    to_dex_slug=_dex_slug(
                        dex.get("ProgramAddress"), dex.get("ProtocolName")
                    ),
                    pool_address=market.get("MarketAddress"),
                    initial_liquidity_usd=_dec(quote.get("PostAmountInUSD")),
                )
            )
        return events

    # -- MarketDataProvider ---------------------------------------------------

    async def get_quote(self, mint: str) -> MarketQuote | None:
        quotes = await self.get_quotes([mint])
        return quotes.get(mint)

    async def get_quotes(self, mints: list[str]) -> dict[str, MarketQuote]:
        if not mints:
            return {}
        # A 24h floor keeps the scan bounded. A token with no trade in 24h has
        # no current quote, which is a true and useful answer.
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        out: dict[str, MarketQuote] = {}

        for chunk in _chunks(mints, 50):
            data = await self._graphql(
                LATEST_QUOTE_QUERY,
                {"mints": chunk, "since": _iso(since)},
                operation="get_quotes",
            )
            rows = _path(data, "Solana", "DEXTradeByTokens") or []
            now = datetime.now(timezone.utc)

            for row in rows:
                trade = row.get("Trade") or {}
                currency = trade.get("Currency") or {}
                mint = currency.get("MintAddress")
                if not mint or mint in out:
                    continue  # rows are newest-first, so the first is current

                dex = trade.get("Dex") or {}
                out[mint] = MarketQuote(
                    mint=mint,
                    provenance=Provenance(
                        provider=self.name,
                        operation="get_quotes",
                        observed_at=now,
                    ),
                    price_usd=_dec(trade.get("PriceInUSD")),
                    dex_slug=_dex_slug(
                        dex.get("ProgramAddress"), dex.get("ProtocolName")
                    ),
                    pair_address=(trade.get("Market") or {}).get("MarketAddress"),
                )
        return out

    async def get_ohlcv(
        self,
        mint: str,
        interval: str,
        since: datetime,
        until: datetime | None = None,
    ) -> list[OhlcvBar]:
        minutes = INTERVAL_MINUTES.get(interval)
        if minutes is None:
            raise ValueError(
                f"Unsupported interval {interval!r}; expected one of "
                f"{sorted(INTERVAL_MINUTES)}"
            )
        until = until or datetime.now(timezone.utc)
        data = await self._graphql(
            OHLCV_QUERY,
            {
                "mint": mint,
                "since": _iso(since),
                "until": _iso(until),
                "interval": minutes,
            },
            operation="get_ohlcv",
        )
        rows = _path(data, "Solana", "DEXTradeByTokens") or []
        now = datetime.now(timezone.utc)

        bars: list[OhlcvBar] = []
        for row in rows:
            opened = _parse_time((row.get("Block") or {}).get("Timefield"))
            if opened is None:
                continue
            trade = row.get("Trade") or {}
            bars.append(
                OhlcvBar(
                    mint=mint,
                    provenance=Provenance(self.name, "get_ohlcv", now),
                    interval=interval,
                    opened_at=opened,
                    open=_dec(trade.get("open")),
                    high=_dec(trade.get("high")),
                    low=_dec(trade.get("low")),
                    close=_dec(trade.get("close")),
                    volume_usd=_dec(row.get("volume")),
                )
            )
        return bars

    async def get_peak_market_cap(
        self, mint: str, since: datetime, until: datetime | None = None
    ) -> tuple[Decimal, datetime] | None:
        until = until or datetime.now(timezone.utc)
        data = await self._graphql(
            PEAK_MARKETCAP_QUERY,
            {"mint": mint, "since": _iso(since), "until": _iso(until)},
            operation="get_peak_market_cap",
        )
        rows = _path(data, "Solana", "DEXTradeByTokens") or []
        if not rows:
            return None

        row = rows[0]
        peak = _dec(row.get("peak_marketcap"))
        at = _parse_time((row.get("Block") or {}).get("Time"))
        if peak is None or at is None:
            return None
        return peak, at

    # -- DexDataProvider ------------------------------------------------------

    async def get_pairs(self, mint: str) -> list[MarketQuote]:
        quote = await self.get_quote(mint)
        return [quote] if quote else []

    async def get_new_pairs(
        self, since: datetime, dex_slug: str | None = None, limit: int = 500
    ) -> list[MarketQuote]:
        data = await self._graphql(
            NEW_POOLS_QUERY,
            {"since": _iso(since), "limit": min(limit, 2000)},
            operation="get_new_pairs",
        )
        rows = _path(data, "Solana", "DEXPools") or []
        now = datetime.now(timezone.utc)

        pairs: list[MarketQuote] = []
        for row in rows:
            pool = row.get("Pool") or {}
            market = pool.get("Market") or {}
            base = market.get("BaseCurrency") or {}
            mint = base.get("MintAddress")
            if not mint:
                continue

            dex = pool.get("Dex") or {}
            slug = _dex_slug(dex.get("ProgramAddress"), dex.get("ProtocolName"))
            if dex_slug is not None and slug != dex_slug:
                continue

            pairs.append(
                MarketQuote(
                    mint=mint,
                    provenance=Provenance(self.name, "get_new_pairs", now),
                    liquidity_usd=_dec((pool.get("Quote") or {}).get("PostAmountInUSD")),
                    dex_slug=slug,
                    pair_address=market.get("MarketAddress"),
                )
            )
        return pairs


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------


def _launchpad_slug(program_address: str | None, name: str | None) -> str | None:
    """Resolve a program to a launchpad slug.

    Falls back to a truncated program address rather than ``None`` when the
    program is unrecognised. That is the point: an unknown launchpad gets a
    stable identity and starts accumulating statistics immediately, instead of
    its tokens being filed as "no launchpad" and disappearing from §18's
    analysis (§5).
    """
    if program_address and program_address in KNOWN_LAUNCH_PROGRAMS:
        return KNOWN_LAUNCH_PROGRAMS[program_address]
    if name:
        cleaned = name.strip().lower().replace(" ", "-").replace("_", "-")
        if cleaned:
            return cleaned
    if program_address:
        return f"unknown-{program_address[:8].lower()}"
    return None


def _dex_slug(program_address: str | None, protocol_name: str | None) -> str | None:
    if program_address and program_address in KNOWN_DEX_PROGRAMS:
        return KNOWN_DEX_PROGRAMS[program_address]
    if protocol_name:
        return protocol_name.strip().lower().replace(" ", "-")
    if program_address:
        return f"unknown-{program_address[:8].lower()}"
    return None


def _path(data: dict[str, Any] | None, *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    raw = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]
