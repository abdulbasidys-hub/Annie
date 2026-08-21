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
        key="jobs",
        label="Background jobs",
        tier=Tier.PRIMARY,
        env_vars=("REDIS_URL",),
        description="Scheduled ingestion, enrichment and trend runs. The app "
        "also exposes manual trigger endpoints that work without this.",
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
        key="market_birdeye",
        label="Cross-validation (Birdeye)",
        tier=Tier.OPTIONAL,
        env_vars=("BIRDEYE_API_KEY",),
        description="Secondary market statistics and historical market information.",
    ),
    Capability(
        key="market_bitquery",
        label="Market & launch data (Bitquery)",
        tier=Tier.OPTIONAL,
        env_vars=("BITQUERY_API_KEY",),
        description="Not this deployment's discovery source (that's Helius). "
        "If configured, used as an extra cross-validation reading.",
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
        description="Chat with Annie from Telegram (§62). No access restriction "
        "is applied — anyone who messages the bot can chat with Annie, at your "
        "OpenAI cost.",
    ),
    Capability(
        key="discord",
        label="Discord bot",
        tier=Tier.OPTIONAL,
        env_vars=("DISCORD_BOT_TOKEN",),
        description="Chat with Annie from Discord (§62): DMs always answered, "
        "server channels answered when @mentioned. No access restriction — see "
        "the Telegram capability's note.",
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
    redis_url: str = ""
    openai_api_key: str = ""
    helius_api_key: str = ""
    helius_rpc_url: str = ""

    # -- OPTIONAL -------------------------------------------------------------
    dexscreener_api_key: str = ""
    birdeye_api_key: str = ""
    bitquery_api_key: str = ""
    tavily_api_key: str = ""
    telegram_bot_token: str = ""
    discord_bot_token: str = ""

    # -- Application ----------------------------------------------------------
    # "development" | "production". Governs session-cookie attributes only
    # (Secure + SameSite=None needs HTTPS, which local dev does not have) —
    # nothing else in the app branches on this. Set to "production" on every
    # real deployment; the default is deliberately the safer-for-localhost one.
    environment: str = "development"

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = ""
    auth_secret: str = ""
    auth_username: str = ""
    auth_password: str = ""
    log_level: str = "info"
    autonomous_research_enabled: bool = False

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

    @field_validator("environment")
    @classmethod
    def _valid_environment(cls, v: str) -> str:
        """Normalised so ``ENVIRONMENT=Production`` (or trailing whitespace
        from a pasted dashboard value) doesn't silently fail the exact-match
        check in app/auth.py and fall back to development-mode cookies —
        which a browser then refuses to send cross-origin, with no error
        anywhere to explain why login "succeeds" but every request 401s."""
        normalised = v.strip().lower()
        allowed = {"development", "production"}
        if normalised not in allowed:
            raise ConfigurationError(
                f"ENVIRONMENT={v!r} is not one of {sorted(allowed)}."
            )
        return normalised

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
