"""Tavily adapter — Annie's external web research (§13).

Two constraints from the spec shape this file:

1. Web research must never replace blockchain data. Nothing in the ingestion
   or qualification path may import this module; it is reachable only from the
   research engine and Annie's tool surface.
2. Annie must call it *selectively* — "it should not search the web for every
   token". The rate limiter is therefore set deliberately low, and the adapter
   exposes a per-task call budget so that a runaway research loop hits a hard
   stop rather than an invoice.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

from app.providers.http import HttpProvider
from app.providers.interfaces import ProviderError
from app.providers.types import Provenance, WebResult

log = structlog.get_logger(__name__)

BASE_URL = "https://api.tavily.com"


class TavilyAdapter(HttpProvider):
    """Implements ``WebResearchProvider``."""

    name = "tavily"
    #: Roughly one credit per basic search. Kept as an order-of-magnitude
    #: figure for the cost display, not a billing record.
    cost_per_request_usd = 0.005

    def __init__(self, api_key: str) -> None:
        self._api_key = (api_key or "").strip()
        super().__init__(
            base_url=BASE_URL,
            headers={"Content-Type": "application/json"},
            rate_per_second=1.0,  # intentionally slow; see module docstring
            burst=2,
            timeout_seconds=30.0,
            max_retries=2,
        )

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def _healthcheck(self) -> bool:
        results = await self.search("solana", max_results=1)
        return bool(results)

    async def search(
        self,
        query: str,
        max_results: int = 5,
        recency_days: int | None = None,
        *,
        topic: str = "general",
        include_domains: list[str] | None = None,
    ) -> list[WebResult]:
        """Search the web.

        ``recency_days`` matters more here than in general search: §19 asks the
        system to find events that *coincide with* an observed migration, so an
        undated or stale result is close to useless as evidence.
        """
        payload: dict[str, Any] = {
            "api_key": self._api_key,
            "query": query,
            "max_results": max(1, min(max_results, 20)),
            "search_depth": "basic",
            "topic": topic,
            "include_answer": False,  # we want sources, not a summary to quote
        }
        if recency_days is not None:
            payload["days"] = recency_days
        if include_domains:
            payload["include_domains"] = include_domains

        data = await self.request(
            "POST", "/search", operation="search", json=payload
        )
        if not data:
            return []

        now = datetime.now(timezone.utc)
        results: list[WebResult] = []
        for item in data.get("results") or []:
            url = item.get("url")
            title = item.get("title")
            if not url or not title:
                continue
            results.append(
                WebResult(
                    title=title,
                    url=url,
                    snippet=item.get("content"),
                    published_at=_parse_published(item.get("published_date")),
                    score=_dec(item.get("score")),
                    provenance=Provenance(
                        provider=self.name,
                        operation="search",
                        observed_at=now,
                        raw_reference=url,
                        confidence=_dec(item.get("score")),
                    ),
                )
            )
        return results

    async def search_ecosystem_event(
        self, subject: str, window_days: int = 14
    ) -> list[WebResult]:
        """Look for a public event that coincides with an observed change (§19).

        The caller supplies the subject; the query template is fixed here so
        that every ecosystem investigation searches comparably. Ad-hoc phrasing
        per call would make results incomparable across investigations, which
        defeats the purpose of storing them as evidence.
        """
        query = (
            f"{subject} Solana launchpad news announcement change "
            f"last {window_days} days"
        )
        try:
            return await self.search(
                query, max_results=8, recency_days=window_days, topic="news"
            )
        except ProviderError as exc:
            # Web research is supplementary; its failure must not abort an
            # investigation that has on-chain evidence.
            log.info("web_research_failed", subject=subject, error=str(exc))
            return []


def _parse_published(value: Any) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(str(value), fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
