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

from app.api.routes import annie, auth, catalogue, intelligence, memory, personality, system, webhooks
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

    if settings.is_available("telegram"):
        from app.bots.telegram_bot import TelegramBot

        bot = TelegramBot(settings.telegram_bot_token, repo, registry, settings)
        _bot_tasks.append(asyncio.create_task(bot.run(), name="telegram_bot"))
        log.info("telegram_bot_enabled")

    if settings.is_available("discord"):
        from app.bots.discord_bot import build_discord_client

        client = build_discord_client(repo, registry, settings)
        _bot_tasks.append(
            asyncio.create_task(client.start(settings.discord_bot_token), name="discord_bot")
        )
        log.info("discord_bot_enabled")


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
    log.info("annie_api_started", port=settings.api_port)

    await _start_bots(settings)
    await _start_scheduler(settings)

    yield

    for task in _bot_tasks:
        task.cancel()
    for task in _bot_tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    await close_registry()
    await dispose_client()


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
app.include_router(personality.router, prefix="/api", tags=["personality"], dependencies=_protected)


@app.get("/health", include_in_schema=False)
async def liveness() -> dict[str, str]:
    """Liveness only — deliberately does not touch the database.

    A readiness probe that fails during a transient database blip would restart
    a process that is fine. Capability and dependency status live at
    /api/system/health, which is what the UI reads.
    """
    return {"status": "ok"}
