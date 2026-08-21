"""Instrumented HTTP base for provider adapters.

Centralises the three things every adapter needs and none should reimplement:
retry with backoff, rate-limit handling, and the telemetry §50 asks for
(requests, errors, rate limits, latency, approximate cost, last success).

Telemetry is emitted to a pluggable sink rather than written to the database
here. The adapters must stay usable from scripts and tests that have no
database, and a provider layer that opens its own transactions is a provider
layer that will eventually deadlock against the caller's.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from app.providers.interfaces import (
    ProviderError,
    ProviderRateLimited,
    ProviderUnavailable,
)

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class ProviderTelemetry:
    provider: str
    operation: str
    event_type: str
    occurred_at: datetime
    latency_ms: int | None = None
    status_code: int | None = None
    error_message: str | None = None
    retry_count: int = 0
    estimated_cost_usd: float | None = None
    context: dict[str, Any] = field(default_factory=dict)


TelemetrySink = Callable[[ProviderTelemetry], Awaitable[None]]

_sinks: list[TelemetrySink] = []


def register_telemetry_sink(sink: TelemetrySink) -> None:
    _sinks.append(sink)


def clear_telemetry_sinks() -> None:
    _sinks.clear()


async def _emit(event: ProviderTelemetry) -> None:
    for sink in _sinks:
        try:
            await sink(event)
        except Exception:  # a broken sink must never break a provider call
            log.warning("telemetry_sink_failed", provider=event.provider, exc_info=True)


class RateLimiter:
    """Simple async token bucket, one per adapter.

    Providers meter differently (requests/second, points/month), so this only
    smooths burst rate. Quota accounting lives in the adapter that knows its
    own pricing model.
    """

    def __init__(self, rate_per_second: float, burst: int = 1) -> None:
        self._interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self._burst = max(1, burst)
        self._tokens = float(self._burst)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self._interval == 0.0:
            return
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._updated
                self._updated = now
                self._tokens = min(
                    float(self._burst), self._tokens + elapsed / self._interval
                )
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) * self._interval)


class HttpProvider:
    """Base class for HTTP-backed adapters.

    Subclasses set ``name``, call ``super().__init__`` with a base URL and
    headers, and use :meth:`request` for every call. They must not construct
    their own ``httpx`` client — doing so silently opts out of all telemetry,
    which is how a provider ends up looking healthy on the dashboard while
    failing every call.
    """

    name: str = "unset"

    #: Rough per-request cost used for the estimates on System Health. A wrong
    #: order of magnitude here is worse than no number, so adapters that cannot
    #: estimate honestly leave it at 0.0 and the UI shows "not estimated".
    cost_per_request_usd: float = 0.0

    def __init__(
        self,
        *,
        base_url: str,
        headers: dict[str, str] | None = None,
        timeout_seconds: float = 20.0,
        rate_per_second: float = 5.0,
        burst: int = 5,
        max_retries: int = 3,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._limiter = RateLimiter(rate_per_second, burst)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers or {},
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
        )

    # -- lifecycle ------------------------------------------------------------

    def is_configured(self) -> bool:  # pragma: no cover - overridden
        return True

    async def aclose(self) -> None:
        await self._client.aclose()

    async def healthcheck(self) -> bool:
        """Never raises — the health page must render even when everything is down."""
        try:
            return await self._healthcheck()
        except Exception as exc:
            log.info("healthcheck_failed", provider=self.name, error=str(exc))
            return False

    async def _healthcheck(self) -> bool:  # pragma: no cover - overridden
        return self.is_configured()

    # -- requests -------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        expected_status: tuple[int, ...] = (200,),
        allow_404: bool = False,
    ) -> Any:
        """Perform an instrumented, retried request.

        Returns parsed JSON, or ``None`` when ``allow_404`` and the resource
        does not exist. A 404 returning ``None`` is meaningful: it means the
        provider affirmatively does not have this record, which is different
        from an error and must not trigger failover.
        """
        if not self.is_configured():
            raise ProviderUnavailable(
                self.name, operation, "adapter is not configured in this deployment"
            )

        attempt = 0
        last_error: Exception | None = None

        while attempt <= self._max_retries:
            await self._limiter.acquire()
            started = time.perf_counter()
            try:
                response = await self._client.request(
                    method, path, params=params, json=json
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                latency = int((time.perf_counter() - started) * 1000)
                last_error = exc
                await _emit(
                    ProviderTelemetry(
                        provider=self.name,
                        operation=operation,
                        event_type="timeout"
                        if isinstance(exc, httpx.TimeoutException)
                        else "error",
                        occurred_at=datetime.now(timezone.utc),
                        latency_ms=latency,
                        error_message=str(exc),
                        retry_count=attempt,
                    )
                )
                attempt += 1
                if attempt > self._max_retries:
                    break
                await asyncio.sleep(self._backoff(attempt))
                continue

            latency = int((time.perf_counter() - started) * 1000)

            if response.status_code == 429:
                retry_after = _parse_retry_after(response)
                await _emit(
                    ProviderTelemetry(
                        provider=self.name,
                        operation=operation,
                        event_type="rate_limited",
                        occurred_at=datetime.now(timezone.utc),
                        latency_ms=latency,
                        status_code=429,
                        retry_count=attempt,
                    )
                )
                attempt += 1
                if attempt > self._max_retries:
                    raise ProviderRateLimited(self.name, operation, retry_after)
                await asyncio.sleep(retry_after or self._backoff(attempt))
                continue

            if response.status_code == 404 and allow_404:
                await _emit(
                    ProviderTelemetry(
                        provider=self.name,
                        operation=operation,
                        event_type="success",
                        occurred_at=datetime.now(timezone.utc),
                        latency_ms=latency,
                        status_code=404,
                        estimated_cost_usd=self.cost_per_request_usd or None,
                        context={"not_found": True},
                    )
                )
                return None

            if response.status_code in expected_status:
                await _emit(
                    ProviderTelemetry(
                        provider=self.name,
                        operation=operation,
                        event_type="success",
                        occurred_at=datetime.now(timezone.utc),
                        latency_ms=latency,
                        status_code=response.status_code,
                        retry_count=attempt,
                        estimated_cost_usd=self.cost_per_request_usd or None,
                    )
                )
                if not response.content:
                    return None
                try:
                    return response.json()
                except ValueError as exc:
                    raise ProviderError(
                        self.name,
                        operation,
                        f"response was not JSON: {exc}",
                        retryable=False,
                        status_code=response.status_code,
                    ) from exc

            retryable = response.status_code >= 500
            body = response.text[:400]
            await _emit(
                ProviderTelemetry(
                    provider=self.name,
                    operation=operation,
                    event_type="error",
                    occurred_at=datetime.now(timezone.utc),
                    latency_ms=latency,
                    status_code=response.status_code,
                    error_message=body,
                    retry_count=attempt,
                )
            )
            if not retryable:
                raise ProviderError(
                    self.name,
                    operation,
                    f"HTTP {response.status_code}: {body}",
                    retryable=False,
                    status_code=response.status_code,
                )
            last_error = ProviderError(
                self.name,
                operation,
                f"HTTP {response.status_code}: {body}",
                retryable=True,
                status_code=response.status_code,
            )
            attempt += 1
            if attempt > self._max_retries:
                break
            await asyncio.sleep(self._backoff(attempt))

        raise ProviderError(
            self.name,
            operation,
            f"failed after {self._max_retries} retries: {last_error}",
            retryable=True,
        )

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential with a ceiling. Deterministic — jitter is added by the
        caller's own scheduling spread, and reproducible timing makes provider
        failures easier to diagnose from the event log."""
        return min(2.0 ** attempt * 0.5, 20.0)


def _parse_retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
