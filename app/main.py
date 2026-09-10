"""FastAPI application entry point.

Startup order matters and is deliberate:

1. Configuration is validated. A missing ``DATABASE_URL`` or ``AUTH_SECRET``
   raises before anything binds a port — see :mod:`app.config`.
2. Degraded capabilities are logged as a banner. The process starts, but the
   operator is told exactly what is off and which variables would fix it.
3. Provider telemetry is wired to the database sink so §50's health metrics
   accumulate from the first request.

Run with::

    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import (
    annie, auth, catalogue, ideas, intelligence, launches, memory, system, today,
    webhooks,
)
from app.auth import require_auth
from app.config import CapabilityUnavailable, Settings, get_settings, startup_banner
from app.db.firestore import dispose_client, get_client
from app.providers.http import ProviderTelemetry, register_telemetry_sink
from app.providers.interfaces import ProviderError
from app.providers.registry import close_registry

log = structlog.get_logger(__name__)

#: Handles for the bot background tasks (§62), so `lifespan` can cancel them
#: cleanly at shutdown instead of leaving a poll loop or Gateway connection
#: dangling when the process exits.
_bot_tasks: list[asyncio.Task] = []

#: The bot client/session objects themselves (Discord's `discord.Client`,
#: Telegram's `TelegramBot`) — kept separately from `_bot_tasks` so shutdown
#: can call their real close/stop method, not just cancel the wrapping task.
#: This matters specifically for Discord: unlike Telegram's long-polling
#: (which Telegram's own API 409s if a second poller starts with the same
#: token), Discord happily accepts more than one simultaneous Gateway
#: connection per bot token. Bare `task.cancel()` raises inside discord.py's
#: internal loop but does not guarantee the Gateway session closes before a
#: new process's connection opens during a rolling redeploy — confirmed as
#: the likely cause of a real incident (2026-08-25): a single "create this
#: channel" request produced two channels with different IDs during a
#: deploy, exactly what two briefly-overlapping live connections both
#: receiving the same event would produce. Explicit `.close()`/`.stop()`
#: gives Discord/Telegram an immediate signal to end the old session instead
#: of waiting for a heartbeat timeout.
_bot_clients: list[object] = []


async def _start_bots(settings: Settings) -> None:
    """Start whichever bot integrations are configured, as background tasks
    in this same process — see app/bots/*.py for what each one does and why
    this deployment runs them here rather than as separate services."""
    from app.db.repo import FirestoreRepo
    from app.providers.registry import get_registry

    if not (settings.is_available("telegram") or settings.is_available("discord")):
        return

    repo = FirestoreRepo(get_client())
    registry = get_registry()

    from app.bots.access_control import ensure_visible

    if settings.is_available("telegram"):
        from app.bots.telegram_bot import TelegramBot

        await ensure_visible(repo, "telegram")
        bot = TelegramBot(settings.telegram_bot_token, repo, registry, settings)
        _bot_clients.append(bot)
        _bot_tasks.append(asyncio.create_task(bot.run(), name="telegram_bot"))
        log.info("telegram_bot_enabled")

    if settings.is_available("discord"):
        from app.bots.discord_bot import build_discord_client

        await ensure_visible(repo, "discord")
        client = build_discord_client(repo, registry, settings)
        _bot_clients.append(client)
        _bot_tasks.append(
            asyncio.create_task(client.start(settings.discord_bot_token), name="discord_bot")
        )
        log.info("discord_bot_enabled")


async def _reconcile_webhook(settings: Settings) -> None:
    """Make the Helius registration match this deployment, on every boot.

    The ingest path has exactly one external dependency and every way it
    breaks looks the same from here: nothing arrives. A registration with no
    ``accountAddresses`` matches no transactions; one pointing at a previous
    deployment delivers somewhere else; one missing a transaction type covers
    half the market. None of them is visible without asking Helius, and all of
    them are mechanically fixable from what this process already knows.

    So it is fixed here rather than offered as a button. Deploying is the
    moment the correct answer is known — the code and the public URL are both
    right here — and it is also the moment the answer most often changes.
    """
    from app.providers import helius_webhook

    try:
        result = await helius_webhook.reconcile(settings)
    except Exception:
        # reconcile() does not raise, but boot must not depend on that.
        log.warning("webhook_reconcile_crashed", exc_info=True)
        return
    if result.get("action") not in {"none", "skipped"}:
        log.info("webhook_reconcile_result", **result)


async def _start_scheduler(settings: Settings) -> None:
    """Start the daily job scheduler (see app/scheduling/) as a background
    task in this same process — same reasoning as `_start_bots` above."""
    from app.db.repo import FirestoreRepo
    from app.providers.registry import get_registry
    from app.scheduling.jobs import JOBS
    from app.scheduling.scheduler import Scheduler

    scheduler = Scheduler(
        registry=get_registry(), repo=FirestoreRepo(get_client()), settings=settings, jobs=JOBS
    )
    _bot_tasks.append(asyncio.create_task(scheduler.run(), name="scheduler"))
    log.info("scheduler_enabled", jobs=[j.name for j in JOBS])


def _configure_logging(level: str) -> None:
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper()))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper())
        ),
    )


async def _telemetry_to_db(event: ProviderTelemetry) -> None:
    """Persist a provider interaction (§50) as a live rollup, not an event log.

    See :mod:`app.db.repo`'s module docstring for why this deployment keeps a
    per-provider rollup document instead of one row per call. A telemetry
    write failing must never fail the call it was describing, hence the
    broad except — a health-page gap is far cheaper than a corrupted request.
    """
    from app.db.repo import FirestoreRepo

    try:
        repo = FirestoreRepo(get_client())
        await repo.record_provider_event(
            provider=event.provider,
            event_type=event.event_type,
            latency_ms=event.latency_ms,
            estimated_cost_usd=event.estimated_cost_usd,
            error_message=event.error_message,
        )
    except Exception:
        log.warning("telemetry_write_failed", provider=event.provider, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()  # raises ConfigurationError if unusable
    _configure_logging(settings.log_level)

    banner = startup_banner(settings)
    if banner:
        log.warning("capabilities_degraded", count=len(banner))
        for line in banner:
            log.warning("capability", detail=line)
    else:
        log.info("all_capabilities_available")

    register_telemetry_sink(_telemetry_to_db)

    # Memory comes up before anything that could write to it. On a healthy
    # deployment (an attached Railway Volume) this is a few local file reads
    # and an index reconcile; on a wiped container it restores the markdown
    # from the Firestore mirror first, which is the only time that
    # collection is ever read. Failing here must not stop the process — an
    # API that starts with empty memory is far more useful than one that
    # refuses to start — so it is logged loudly and the app continues.
    try:
        from app.memory.bootstrap import ensure_memory_ready

        report = await ensure_memory_ready()
        log.info(
            "memory_initialised",
            root=report["root"],
            files=report["index"].get("files"),
            restored=report["restored"].get("restored"),
        )
    except Exception:
        log.error("memory_init_failed", exc_info=True)

    log.info("annie_api_started", port=settings.api_port)

    if settings.annie_api_only:
        # ANNIE_API_ONLY: serve the HTTP API and nothing else. Memory is
        # already up (above), which is all the read surface needs.
        log.info("background_tasks_disabled", reason="ANNIE_API_ONLY is set")
    else:
        await _start_bots(settings)
        await _start_scheduler(settings)
        # Deliberately not awaited: a third-party API must never sit between
        # this process and serving traffic. It writes only when the live
        # registration is actually wrong, so the usual case is one GET.
        _bot_tasks.append(
            asyncio.create_task(_reconcile_webhook(settings), name="webhook_reconcile")
        )

    yield

    # Close the actual bot session first — see `_bot_clients`'s docstring for
    # why this matters more than it looks for Discord specifically. `close`
    # (discord.Client) and `stop` (TelegramBot) are both async and idempotent
    # enough to call even if the client never fully connected.
    for client in _bot_clients:
        closer = getattr(client, "close", None) or getattr(client, "stop", None)
        if closer is None:
            continue
        try:
            await closer()
        except Exception:
            log.warning("bot_client_close_failed", client=type(client).__name__, exc_info=True)

    for task in _bot_tasks:
        task.cancel()
    for task in _bot_tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    await close_registry()
    await dispose_client()

    from app.memory import db as memory_db

    memory_db.close()


settings = get_settings()

app = FastAPI(
    title="Annie",
    description="Solana memecoin intelligence and research system.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # Explicit origins only. `cors_origin_list` returns [] when unset, which
    # blocks all cross-origin calls rather than defaulting to "*".
    allow_origins=settings.cors_origin_list,
    # No cookies cross-origin (§66 — auth is a bearer token, see app/auth.py),
    # so no credentialed CORS needed; the Authorization header carries the
    # session instead and just needs to be an allowed header, below.
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.exception_handler(CapabilityUnavailable)
async def capability_handler(request: Request, exc: CapabilityUnavailable):
    """503 that names the missing variables.

    The frontend renders these directly, so an operator sees "set
    TAVILY_API_KEY" rather than a generic failure.
    """
    return JSONResponse(
        status_code=503,
        content={
            "error": f"{exc.capability.label} is not configured in this deployment.",
            "detail": exc.capability.description,
            "capability": exc.capability.key,
            "missing_env_vars": list(exc.missing),
        },
    )


@app.exception_handler(ProviderError)
async def provider_handler(request: Request, exc: ProviderError):
    status = 504 if exc.retryable else 502
    return JSONResponse(
        status_code=status,
        content={
            "error": f"Upstream provider {exc.provider} failed.",
            "detail": str(exc),
            "capability": None,
            "missing_env_vars": [],
        },
    )


# Every data/AI route requires a valid session (§66) — applied at the router
# level so a route added later is protected by default. /api/auth/*, the
# bare /health liveness check, and /api/webhooks/* (Helius calls this
# directly server-to-server, authenticated by its own shared secret instead
# — see app/api/routes/webhooks.py) are the only unauthenticated surface.
_protected = [Depends(require_auth)]
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(webhooks.router, prefix="/api/webhooks", tags=["webhooks"])
app.include_router(system.router, prefix="/api/system", tags=["system"], dependencies=_protected)
app.include_router(catalogue.router, prefix="/api", tags=["catalogue"], dependencies=_protected)
app.include_router(intelligence.router, prefix="/api", tags=["intelligence"], dependencies=_protected)
app.include_router(annie.router, prefix="/api/annie", tags=["annie"], dependencies=_protected)
app.include_router(memory.router, prefix="/api", tags=["memory"], dependencies=_protected)
app.include_router(ideas.router, prefix="/api", tags=["ideas"], dependencies=_protected)
app.include_router(launches.router, prefix="/api", tags=["launches"], dependencies=_protected)
app.include_router(today.router, prefix="/api", tags=["today"], dependencies=_protected)


@app.get("/health", include_in_schema=False)
async def liveness() -> dict[str, str]:
    """Liveness only — deliberately does not touch the database.

    A readiness probe that fails during a transient database blip would restart
    a process that is fine. Capability and dependency status live at
    /api/system/health, which is what the UI reads.
    """
    return {"status": "ok"}
