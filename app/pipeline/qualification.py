"""Stage 2 — qualification (§4).

Decides whether a token is a research subject, and records *why*. The evidence
requirement is the point: §4 says "the system must record the evidence used to
determine qualification", so this module never returns a bare boolean. Every
verdict carries the market cap observed, which provider reported it, whether
anything disagreed, and which rule version was applied.

The rule set is data, not code. Thresholds live in the settings table (§4:
"the exact qualification logic must be configurable"), and the rule version is
stamped on each decision so a token qualified under old thresholds can be
identified after they change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import structlog

from app.db.enums import VerificationStatus
from app.providers.registry import ProviderRegistry, ResolvedQuote

log = structlog.get_logger(__name__)

#: Bumped whenever the logic below changes in a way that would alter verdicts.
#: Stored on every token so a mixed-vintage database stays interpretable.
RULE_VERSION = "qualification/v1"

DEFAULT_TIERS: tuple[Decimal, ...] = (
    Decimal("100000"),
    Decimal("250000"),
    Decimal("500000"),
    Decimal("1000000"),
)

#: A market cap this far above the next tier from a single unverified source is
#: treated as suspect rather than as a spectacular win. Thin-pool price spikes
#: routinely imply eight-figure caps for tokens with $400 of liquidity.
IMPLAUSIBLE_MULTIPLE = Decimal("50")

#: Minimum liquidity for a market cap to be considered real. A market cap
#: computed from a pool no one could exit is not a measurement of value.
MIN_LIQUIDITY_USD = Decimal("1000")


@dataclass(slots=True)
class QualificationVerdict:
    """The outcome of evaluating one token, with its full justification."""

    mint: str
    qualified: bool
    market_cap: Decimal | None
    tier_reached: Decimal | None
    provider: str | None
    verification_status: str
    rule_version: str = RULE_VERSION
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    reasons: list[str] = field(default_factory=list)
    conflict: dict[str, Any] | None = None
    needs_verification: bool = False

    def as_evidence(self) -> dict[str, Any]:
        """Serialised for ``Token.qualification_evidence``."""
        return {
            "rule_version": self.rule_version,
            "evaluated_at": self.evaluated_at.isoformat(),
            "market_cap": str(self.market_cap) if self.market_cap else None,
            "tier_reached": str(self.tier_reached) if self.tier_reached else None,
            "provider": self.provider,
            "verification_status": self.verification_status,
            "reasons": list(self.reasons),
            "conflict": self.conflict,
            "needs_verification": self.needs_verification,
        }


def tier_for(market_cap: Decimal | None, tiers: tuple[Decimal, ...] = DEFAULT_TIERS) -> Decimal | None:
    """Highest tier a market cap satisfies."""
    if market_cap is None:
        return None
    reached = [t for t in tiers if market_cap >= t]
    return max(reached) if reached else None


class Qualifier:
    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        tiers: tuple[Decimal, ...] = DEFAULT_TIERS,
        min_liquidity: Decimal = MIN_LIQUIDITY_USD,
    ) -> None:
        self.registry = registry
        self.tiers = tuple(sorted(tiers))
        self.min_liquidity = min_liquidity

    async def evaluate(self, mint: str) -> QualificationVerdict:
        resolved = await self.registry.resolve_market_cap(mint, cross_validate=True)
        return self.evaluate_resolved(mint, resolved)

    def evaluate_resolved(self, mint: str, resolved: ResolvedQuote) -> QualificationVerdict:
        """Pure decision logic, separated so it can be tested without network."""
        reasons: list[str] = []

        if resolved.quote is None or resolved.quote.market_cap is None:
            reasons.append(
                "No provider returned a market cap."
                + (f" Errors: {'; '.join(resolved.errors)}" if resolved.errors else "")
            )
            return QualificationVerdict(
                mint=mint,
                qualified=False,
                market_cap=None,
                tier_reached=None,
                provider=None,
                verification_status=VerificationStatus.PENDING,
                reasons=reasons,
                needs_verification=True,
            )

        quote = resolved.quote
        market_cap = quote.market_cap
        threshold = self.tiers[0]

        # -- Plausibility --------------------------------------------------
        # Run before the threshold check. A token failing these is not
        # rejected outright — it is flagged for verification, because §49
        # forbids discarding information on the say-so of one source.

        if quote.liquidity_usd is not None and quote.liquidity_usd < self.min_liquidity:
            reasons.append(
                f"Liquidity ${quote.liquidity_usd:,.0f} is below the "
                f"${self.min_liquidity:,.0f} floor; market cap may not be realisable."
            )
            return QualificationVerdict(
                mint=mint,
                qualified=False,
                market_cap=market_cap,
                tier_reached=None,
                provider=resolved.provider,
                verification_status=VerificationStatus.UNVERIFIED,
                reasons=reasons,
                conflict=resolved.conflict,
                needs_verification=True,
            )

        top_tier = self.tiers[-1]
        if (
            market_cap > top_tier * IMPLAUSIBLE_MULTIPLE
            and resolved.verification_status != VerificationStatus.CROSS_VERIFIED
        ):
            reasons.append(
                f"Market cap ${market_cap:,.0f} exceeds ${top_tier:,.0f} by more than "
                f"{IMPLAUSIBLE_MULTIPLE}x on a single uncorroborated source; "
                "held for verification rather than recorded as a win."
            )
            return QualificationVerdict(
                mint=mint,
                qualified=False,
                market_cap=market_cap,
                tier_reached=None,
                provider=resolved.provider,
                verification_status=VerificationStatus.UNVERIFIED,
                reasons=reasons,
                conflict=resolved.conflict,
                needs_verification=True,
            )

        # -- Disagreement ---------------------------------------------------
        if resolved.conflict is not None:
            values = ", ".join(
                f"{v['provider']}=${Decimal(v['value']):,.0f}"
                for v in resolved.conflict["values"]
            )
            reasons.append(
                f"Providers disagree materially ({values}); recorded as disputed "
                "and queued for verification. No provider was silently preferred."
            )

        # -- Threshold ------------------------------------------------------
        if market_cap < threshold:
            reasons.append(
                f"Market cap ${market_cap:,.0f} is below the ${threshold:,.0f} "
                "research threshold."
            )
            return QualificationVerdict(
                mint=mint,
                qualified=False,
                market_cap=market_cap,
                tier_reached=None,
                provider=resolved.provider,
                verification_status=resolved.verification_status,
                reasons=reasons,
                conflict=resolved.conflict,
            )

        tier = tier_for(market_cap, self.tiers)
        reasons.append(
            f"Market cap ${market_cap:,.0f} from {resolved.provider} meets the "
            f"${threshold:,.0f} threshold; highest tier reached ${tier:,.0f}."
        )
        if resolved.used_fallback:
            reasons.append(
                f"Primary market provider was unavailable; value came from "
                f"{resolved.provider} and is marked "
                f"{resolved.verification_status}."
            )

        return QualificationVerdict(
            mint=mint,
            qualified=True,
            market_cap=market_cap,
            tier_reached=tier,
            provider=resolved.provider,
            verification_status=resolved.verification_status,
            reasons=reasons,
            conflict=resolved.conflict,
            needs_verification=(
                resolved.verification_status
                in (VerificationStatus.DISPUTED, VerificationStatus.UNVERIFIED)
            ),
        )
