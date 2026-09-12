"""Runtime configuration.

Design rule, applied without exception: **the system never substitutes a
plausible-looking default for a missing environment value.** A missing address,
endpoint or key either stops the process or disables a named capability. It
never silently becomes ``localhost``, an empty string treated as valid, or a
"sensible" fallback that produces confident, wrong data downstream.

Three tiers:

``REQUIRED``
    Absent -> :class:`ConfigurationError` at import. The process does not start.
    Only the database qualifies: Build.md §39 makes it the source of truth, so
    a system without one cannot be correct about anything.

``PRIMARY``
    Absent -> the app starts and logs a loud banner, but the dependent
    capability is hard-off. Routes that need it return HTTP 503 naming the
    missing variable. This satisfies Build.md §51 ("must not require optional
    providers before starting ... should clearly report unavailable
    capabilities") without ever faking the data.

``OPTIONAL``
    Absent -> capability reported ``DISABLED`` on System Health. Used for
    cross-validation and gap-filling only, so absence degrades breadth of
    evidence, never correctness.

Database note: this deployment uses **Cloud Firestore**, not PostgreSQL.
Build.md §72.5-§72.7 amends the original Postgres design; §52's table list is
implemented as Firestore collections in :mod:`app.db.repo`. See that module's
docstring for the mapping and why some SQL guarantees (foreign keys, unique
constraints, JOINs) are enforced in application code instead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Final

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(RuntimeError):
    """Raised when configuration is missing or malformed.

    Deliberately not caught anywhere in application startup. A misconfigured
    research system is worse than a stopped one, because it produces evidence
    that looks trustworthy.
    """


class Tier(str, Enum):
    REQUIRED = "required"
    PRIMARY = "primary"
    OPTIONAL = "optional"


class CapabilityStatus(str, Enum):
    """Reported verbatim to the System Health page."""

    AVAILABLE = "available"
    DEGRADED = "degraded"
    DISABLED = "disabled"


@dataclass(frozen=True)
class Capability:
    """A named ability the system either has or does not have.

    ``env_vars`` are listed in user-facing messages so an operator is told
    exactly which variable to set, rather than "provider unavailable".
    """

    key: str
    label: str
    tier: Tier
    env_vars: tuple[str, ...]
    description: str
    #: True when *any one* of ``env_vars`` is enough (e.g. the Firestore
    #: credential can arrive as a JSON blob or a file path — either satisfies
    #: it). False (default) means all of them are required together, as with
    #: Helius's key-plus-RPC-URL pair.
    require_any: bool = False

    def status(self, settings: "Settings") -> CapabilityStatus:
        present = [v for v in self.env_vars if _non_empty(getattr(settings, v.lower(), None))]
        needed = 1 if self.require_any else len(self.env_vars)
        if len(present) >= needed:
            return CapabilityStatus.AVAILABLE
        if present:
            # Partially configured is its own failure mode and must not read as
            # "working". Helius with a key but no RPC URL is not usable.
            return CapabilityStatus.DEGRADED
        return CapabilityStatus.DISABLED

    def missing(self, settings: "Settings") -> tuple[str, ...]:
        absent = tuple(v for v in self.env_vars if not _non_empty(getattr(settings, v.lower(), None)))
        if self.require_any and len(absent) < len(self.env_vars):
            return ()  # at least one alternative is set — nothing is "missing"
        return absent


def _non_empty(value: object) -> bool:
    """Blank and whitespace-only are *absent*, not configured-as-empty."""
    return value is not None and str(value).strip() != ""


# -----------------------------------------------------------------------------
# Capability registry
# -----------------------------------------------------------------------------
# Single source of truth for "what can this deployment actually do". The API
# health route, the worker startup check and the frontend System Health page
# all read from here, so they can never disagree.

CAPABILITIES: Final[tuple[Capability, ...]] = (
    Capability(
        key="database",
        label="Database (Firestore)",
        tier=Tier.REQUIRED,
        env_vars=("FIREBASE_SERVICE_ACCOUNT_JSON", "FIREBASE_SERVICE_ACCOUNT_FILE"),
        require_any=True,
        description="Permanent normalised research storage. Source of truth.",
    ),
    Capability(
        key="ai",
        label="AI reasoning",
        tier=Tier.PRIMARY,
        env_vars=("OPENAI_API_KEY",),
        description="Annie's interpretation, narrative categorisation, image analysis.",
    ),
    Capability(
        key="blockchain",
        label="Blockchain & discovery (Helius)",
        tier=Tier.PRIMARY,
        env_vars=("HELIUS_API_KEY", "HELIUS_RPC_URL"),
        description="On-chain verification, deployer resolution, and launch "
        "discovery. Without this, the system has no way to find new tokens.",
    ),
    Capability(
        key="market_primary",
        label="Market data (DexScreener)",
        tier=Tier.OPTIONAL,
        env_vars=(),  # public endpoints need no key; adapter is always enabled
        description="Price, liquidity, volume and market-cap for a known mint. "
        "Public endpoints, no key required — this is the qualification "
        "engine's default market-data source in this deployment.",
    ),
    Capability(
        key="web_research",
        label="Web research (Tavily)",
        tier=Tier.OPTIONAL,
        env_vars=("TAVILY_API_KEY",),
        description="External context for ecosystem events and narrative explanations.",
    ),
    Capability(
        key="telegram",
        label="Telegram bot",
        tier=Tier.OPTIONAL,
        env_vars=("TELEGRAM_BOT_TOKEN",),
        description="Chat with Annie from Telegram (§62). Open to anyone who "
        "messages the bot until you add IDs to the telegram_allowlist setting "
        "(Settings page), at which point only those IDs can chat, at your "
        "OpenAI cost.",
    ),
    Capability(
        key="discord",
        label="Discord bot",
        tier=Tier.OPTIONAL,
        env_vars=("DISCORD_BOT_TOKEN",),
        description="Chat with Annie from Discord (§62): DMs always answered, "
        "server channels answered when @mentioned. Same allowlist mechanism as "
        "Telegram (discord_allowlist setting) — open by default.",
    ),
)


class Settings(BaseSettings):
    """Environment-backed settings.

    Note the absence of defaults on anything environment-specific. Every field
    that names an address, endpoint or key defaults to ``""`` — which the
    capability check treats as *absent*, not as a usable value.
    """

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- REQUIRED -------------------------------------------------------------
    # Full service-account JSON as a single value (matches how Railway/Render/
    # Vercel expose secrets — one env var, no file upload). Locally it can also
    # be loaded from a file via FIREBASE_SERVICE_ACCOUNT_FILE, see
    # app.db.firestore._credentials_info.
    firebase_service_account_json: str = ""
    firebase_service_account_file: str = ""
    firebase_project_id: str = ""

    # -- PRIMARY --------------------------------------------------------------
    openai_api_key: str = ""
    helius_api_key: str = ""
    helius_rpc_url: str = ""

    # Shared secret Helius echoes back in every webhook call (the `authHeader`
    # given at webhook-creation time), verified in app/api/routes/webhooks.py.
    # Not a Capability — nothing shows this as "degraded" on System Health if
    # unset, because absence just means the webhook route rejects everything,
    # which is the safe failure mode, not a broken one.
    helius_webhook_secret: str = ""

    # -- OPTIONAL -------------------------------------------------------------
    dexscreener_api_key: str = ""
    tavily_api_key: str = ""
    telegram_bot_token: str = ""
    discord_bot_token: str = ""

    # -- Application ----------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = ""
    auth_secret: str = ""
    auth_username: str = ""
    auth_password: str = ""
    log_level: str = "info"

    # -- Memory & cost control (2026-09-08 rewrite) ---------------------------
    # Where Annie's markdown memory and her SQLite ledger/search index live.
    # On Railway this must point at an attached Volume's mount path (e.g.
    # /data/memory) — the container filesystem is wiped on every redeploy,
    # and without a volume the only thing keeping memory alive would be the
    # Firestore snapshot restore in app/memory/snapshot.py. Locally it
    # defaults to ./memory so the files sit in the repo where you can read
    # them. Not a Capability: a missing volume degrades durability, not
    # function, and the snapshot layer reports it on System Health instead.
    annie_memory_dir: str = ""

    #: Hard ceiling on Firestore writes this process will issue per UTC day.
    #: The Spark (free) plan allows 20,000 writes/day across the whole
    #: project; this sits well under it so a runaway loop degrades into
    #: "memory-only, logged" instead of a billing incident or a hard quota
    #: failure mid-cycle. Enforced in app/db/budget.py.
    firestore_write_budget_per_day: int = 4000

    #: Same, for reads (Spark allows 50,000/day).
    firestore_read_budget_per_day: int = 12000

    #: How many of the freshest unqualified mints the watch loop re-prices
    #: each pass. These go out as batched DexScreener lookups (30 mints per
    #: HTTP request), so 900 is 30 requests, not 900 — see
    #: app/pipeline/watch.py.
    watch_batch_size: int = 900

    #: Start the HTTP API only — no bots, no scheduler, no background tasks.
    #: Set by the test suite, and useful in production for running a second
    #: read-only instance behind the same data without two schedulers racing
    #: each other (Discord in particular tolerates two simultaneous Gateway
    #: connections per token and will deliver the same event to both).
    annie_api_only: bool = False

    #: The market-cap bar a token must clear to earn its own memory file.
    #:
    #: Deliberately higher than the $100k qualification floor. Qualification
    #: decides cohort membership for the statistics, where a low bar is
    #: correct — it is the denominator. A *memory* is different: it is prose
    #: Annie writes and reads back, and at real Solana volume roughly one
    #: token a minute clears $100k. Writing a file for each would be ~42,000
    #: files a month, which is not a notebook, and ~1,400 Firestore snapshot
    #: writes a day against a 4,000 budget.
    #:
    #: Below this, a qualifying token is still recorded in the ledger, still
    #: counted in every signal, and still listed with its CA and creator in
    #: the deterministic daily log. It just does not get its own page.
    memory_tier_usd: float = 250_000.0

    #: How old a token's market can be and still count as a new launch.
    #:
    #: Helius fires CREATE_POOL for any pool creation, including an
    #: established token opening a new market, so without this RAY, Bonk,
    #: WBTC and $WIF all arrive looking like fresh launches and qualify
    #: instantly on market caps they reached years ago. Thirty days is
    #: generous — a token that takes three weeks to run is still a launch —
    #: while excluding anything with real history.
    max_launch_age_days: float = 30.0

    #: Hard ceiling on token memory files written in one UTC day.
    #:
    #: The floor above adapts badly to a genuinely exceptional day — if a
    #: thousand tokens clear $250k, the bar did not stop anything. This does.
    #: Once hit, further qualifiers are logged and skipped; nothing is lost
    #: from the record, because the daily log lists them all regardless.
    max_token_memories_per_day: int = 30

    #: A launch is only kept in the ledger's active watchlist for this long
    #: before being pruned unless it did something. A memecoin that has not
    #: moved in 48h is not going to; a human watching the market drops it
    #: from attention, and so does Annie.
    watch_ttl_hours: int = 48

    # -- Model selection ------------------------------------------------------
    # Pinned to gpt-5.6-luna everywhere, deliberately — reasoning, vision and
    # "cheap" all point at the same model rather than being split across
    # tiers. The three separate settings stay distinct (not collapsed into
    # one) so a future change back to a split setup is a two-line edit, not a
    # redesign — but as of now, no other OpenAI model may be called.
    openai_reasoning_model: str = "gpt-5.6-luna"
    openai_vision_model: str = "gpt-5.6-luna"
    openai_cheap_model: str = "gpt-5.6-luna"

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, v: str) -> str:
        allowed = {"debug", "info", "warning", "error"}
        if v.lower() not in allowed:
            raise ConfigurationError(
                f"LOG_LEVEL={v!r} is not one of {sorted(allowed)}."
            )
        return v.lower()


    # -- Derived --------------------------------------------------------------

    @property
    def cors_origin_list(self) -> list[str]:
        """Explicit origins only.

        An unset value yields an empty list, which blocks all cross-origin
        calls. It does not become ``["*"]`` — a permissive default here is a
        security hole that would not announce itself (Build.md §66).
        """
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def capability(self, key: str) -> Capability:
        for cap in CAPABILITIES:
            if cap.key == key:
                return cap
        raise KeyError(f"Unknown capability {key!r}")

    def status_of(self, key: str) -> CapabilityStatus:
        return self.capability(key).status(self)

    def is_available(self, key: str) -> bool:
        return self.status_of(key) is CapabilityStatus.AVAILABLE

    def require(self, key: str) -> None:
        """Guard for code paths that cannot function without a capability.

        Raises :class:`CapabilityUnavailable`, which the API layer renders as a
        503 naming the exact missing variables. Callers must never catch this
        and fall back to a different data source silently — Build.md §49
        requires that substitution be recorded, not hidden.
        """
        cap = self.capability(key)
        if cap.status(self) is not CapabilityStatus.AVAILABLE:
            raise CapabilityUnavailable(cap, cap.missing(self))

    def capability_report(self) -> list[dict[str, object]]:
        """Serialisable snapshot for the System Health page."""
        return [
            {
                "key": cap.key,
                "label": cap.label,
                "tier": cap.tier.value,
                "status": cap.status(self).value,
                "description": cap.description,
                "missing_env_vars": list(cap.missing(self)),
            }
            for cap in CAPABILITIES
        ]


class CapabilityUnavailable(RuntimeError):
    """A capability was used that this deployment is not configured for."""

    def __init__(self, capability: Capability, missing: tuple[str, ...]) -> None:
        self.capability = capability
        self.missing = missing
        detail = ", ".join(missing) if missing else "unknown"
        super().__init__(
            f"Capability {capability.key!r} ({capability.label}) is unavailable. "
            f"Missing environment variable(s): {detail}."
        )


# -----------------------------------------------------------------------------
# Startup validation
# -----------------------------------------------------------------------------


def _validate_required(settings: Settings) -> None:
    missing: list[str] = []
    for cap in CAPABILITIES:
        if cap.tier is Tier.REQUIRED:
            missing.extend(cap.missing(settings))

    # AUTH_SECRET is not a provider capability but is required in any
    # deployment that exposes the frontend. A generated-on-boot default would
    # silently invalidate sessions on every restart and, worse, would look like
    # it worked — so it is fatal instead.
    if not _non_empty(settings.auth_secret):
        missing.append("AUTH_SECRET")

    if missing:
        raise ConfigurationError(
            "Missing required configuration: "
            + ", ".join(missing)
            + ". The process will not start without these. See .env.example."
        )


def startup_banner(settings: Settings) -> list[str]:
    """Lines describing every degraded/disabled PRIMARY or OPTIONAL capability.

    Returns an empty list when everything is available, so callers can tell
    "fully configured" from "logged a warning" without re-deriving it.
    """
    lines: list[str] = []
    for cap in CAPABILITIES:
        status = cap.status(settings)
        if cap.tier is Tier.REQUIRED or status is CapabilityStatus.AVAILABLE:
            continue
        missing = ", ".join(cap.missing(settings)) or "unknown"
        lines.append(f"{cap.label} [{cap.tier.value}] is {status.value}: set {missing}")
    return lines


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton. Validated once, on first access."""
    settings = Settings()
    _validate_required(settings)
    return settings
