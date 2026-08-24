"""app/pipeline/qualification.py's Qualifier.evaluate_resolved — pure
decision logic (no network), and the piece this session's redesign now
leans on entirely: since DexScreener has no quote for a token still on its
Pump.fun bonding curve (verified empirically against real chain data), this
function alone is what enforces "must have migrated to qualify" — there is
no separate migration-detection code path to also test.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.db.enums import VerificationStatus
from app.pipeline.qualification import Qualifier
from app.providers.registry import ResolvedQuote
from app.providers.types import MarketQuote, Provenance

NOW = datetime.now(timezone.utc)


def _quote(market_cap, liquidity_usd=Decimal("50000")) -> MarketQuote:
    return MarketQuote(
        mint="TestMint111111111111111111111111111111111",
        provenance=Provenance(provider="dexscreener", operation="get_quote", observed_at=NOW),
        market_cap=market_cap,
        liquidity_usd=liquidity_usd,
    )


def _resolved(quote, *, provider="dexscreener", status=VerificationStatus.VERIFIED,
              used_fallback=False, conflict=None, errors=None) -> ResolvedQuote:
    return ResolvedQuote(
        quote=quote, provider=provider, verification_status=status,
        used_fallback=used_fallback, conflict=conflict, errors=errors or [],
    )


class TestNoQuote:
    def test_no_provider_data_is_not_qualified_and_pending(self):
        qualifier = Qualifier(None)
        verdict = qualifier.evaluate_resolved("mint", _resolved(None, provider=None, status=VerificationStatus.PENDING))
        assert verdict.qualified is False
        assert verdict.verification_status == VerificationStatus.PENDING
        assert verdict.needs_verification is True
        assert verdict.tier_reached is None

    def test_this_is_the_real_gate_for_a_token_still_on_its_bonding_curve(self):
        # DexScreener returns quote=None for a pre-migration mint — confirmed
        # empirically this session (5/5 non-migrated tokens, zero pairs each).
        # No separate "has it migrated" check exists; this branch IS that check.
        qualifier = Qualifier(None)
        verdict = qualifier.evaluate_resolved(
            "mint", _resolved(None, provider=None, status=VerificationStatus.PENDING)
        )
        assert verdict.qualified is False
        assert "No provider returned a market cap" in verdict.reasons[0]


class TestLiquidityFloor:
    def test_below_minimum_liquidity_is_rejected_even_above_threshold(self):
        qualifier = Qualifier(None)
        quote = _quote(market_cap=Decimal("500000"), liquidity_usd=Decimal("500"))
        verdict = qualifier.evaluate_resolved("mint", _resolved(quote))
        assert verdict.qualified is False
        assert verdict.needs_verification is True
        assert "Liquidity" in verdict.reasons[0]

    def test_unknown_liquidity_does_not_block_qualification(self):
        # liquidity_usd=None means "provider didn't report it," not "it's
        # zero" — the check is skipped rather than treated as a failure.
        qualifier = Qualifier(None)
        quote = _quote(market_cap=Decimal("500000"), liquidity_usd=None)
        verdict = qualifier.evaluate_resolved("mint", _resolved(quote))
        assert verdict.qualified is True


class TestImplausibleMultiple:
    def test_absurd_market_cap_from_one_uncorroborated_source_is_held(self):
        # $1M top tier x 50 = $50M ceiling; a single-source reading above
        # that is treated as suspect, not a spectacular win.
        qualifier = Qualifier(None)
        quote = _quote(market_cap=Decimal("100000000"))  # $100M, single source
        verdict = qualifier.evaluate_resolved(
            "mint", _resolved(quote, status=VerificationStatus.VERIFIED)
        )
        assert verdict.qualified is False
        assert verdict.needs_verification is True
        assert "exceeds" in verdict.reasons[0]

    def test_same_absurd_cap_is_accepted_when_cross_verified(self):
        qualifier = Qualifier(None)
        quote = _quote(market_cap=Decimal("100000000"))
        verdict = qualifier.evaluate_resolved(
            "mint", _resolved(quote, status=VerificationStatus.CROSS_VERIFIED)
        )
        assert verdict.qualified is True


class TestThreshold:
    def test_below_threshold_is_not_qualified(self):
        qualifier = Qualifier(None)
        quote = _quote(market_cap=Decimal("50000"))
        verdict = qualifier.evaluate_resolved("mint", _resolved(quote))
        assert verdict.qualified is False
        assert verdict.tier_reached is None
        assert "below the" in verdict.reasons[0]

    def test_at_exactly_the_threshold_qualifies(self):
        qualifier = Qualifier(None)
        quote = _quote(market_cap=Decimal("100000"))
        verdict = qualifier.evaluate_resolved("mint", _resolved(quote))
        assert verdict.qualified is True
        assert verdict.tier_reached == Decimal("100000")

    def test_reaches_the_highest_tier_it_actually_clears(self):
        qualifier = Qualifier(None)
        quote = _quote(market_cap=Decimal("750000"))
        verdict = qualifier.evaluate_resolved("mint", _resolved(quote))
        assert verdict.qualified is True
        assert verdict.tier_reached == Decimal("500000")  # not 1,000,000

    def test_top_tier(self):
        qualifier = Qualifier(None)
        quote = _quote(market_cap=Decimal("2000000"))
        verdict = qualifier.evaluate_resolved("mint", _resolved(quote))
        assert verdict.qualified is True
        assert verdict.tier_reached == Decimal("1000000")


class TestConflict:
    def test_disagreement_is_recorded_but_does_not_block_qualification(self):
        qualifier = Qualifier(None)
        quote = _quote(market_cap=Decimal("500000"))
        conflict = {
            "field": "market_cap", "divergence": "0.3", "threshold": "0.15",
            "values": [{"provider": "dexscreener", "value": "500000"}, {"provider": "other", "value": "700000"}],
            "detected_at": NOW.isoformat(),
        }
        verdict = qualifier.evaluate_resolved("mint", _resolved(quote, conflict=conflict))
        assert verdict.qualified is True
        assert verdict.conflict == conflict
        assert any("disagree" in r for r in verdict.reasons)


class TestEvidenceIsAlwaysRecorded:
    def test_a_no_verdict_still_has_reasons(self):
        # §4: "the system must record the evidence used to determine
        # qualification" — a rejection is a decision, not silence.
        qualifier = Qualifier(None)
        quote = _quote(market_cap=Decimal("1"))
        verdict = qualifier.evaluate_resolved("mint", _resolved(quote))
        assert verdict.qualified is False
        assert len(verdict.reasons) > 0
        evidence = verdict.as_evidence()
        assert evidence["reasons"]
        assert evidence["market_cap"] == "1"
