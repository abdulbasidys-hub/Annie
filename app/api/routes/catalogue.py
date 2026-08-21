"""Catalogue routes: tokens, creators, launchpads, narratives."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.schemas import (
    CreatorDetail,
    CreatorSummary,
    LaunchpadDetail,
    LaunchpadSummary,
    NarrativeSummary,
    Page,
    TokenDetail,
    TokenSummary,
)
from app.db.models.tokens import Token
from app.db.repo import FirestoreRepo, get_repo

router = APIRouter()


def _themes(features: list[Any]) -> list[str]:
    return [f.value for f in features if f.namespace == "token" and f.key == "theme" and f.value]


def _token_summary(token: Token, themes: list[str]) -> dict[str, Any]:
    return {
        "id": token.mint,
        "mint": token.mint,
        "name": token.name,
        "symbol": token.symbol,
        "image_url": token.image_url,
        "launchpad_slug": token.launchpad_slug,
        "creator_wallet": token.creator_wallet,
        "launched_at": token.launched_at,
        "qualified_at": token.qualified_at,
        "qualified_market_cap": token.qualified_market_cap,
        "peak_market_cap": token.peak_market_cap,
        "peak_tier": token.peak_tier,
        "is_qualified": token.is_qualified,
        "verification_status": token.verification_status,
        "themes": themes,
    }


@router.get("/tokens", response_model=Page[TokenSummary])
async def list_tokens(
    q: str | None = None,
    min_tier: Decimal | None = None,
    launchpad: str | None = None,
    qualified_only: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    repo: FirestoreRepo = Depends(get_repo),
) -> dict[str, Any]:
    """List tokens.

    ``q`` (free-text search across name/symbol/mint) is not implemented in
    this Firestore deployment — full-text search needs a dedicated index
    (Algolia, Typesense, or Firestore's own extension) that this project does
    not provision. Filter by ``launchpad``, ``qualified_only`` or
    ``min_tier`` instead; a search box wired to nothing would be worse than
    no search box, so the frontend should treat this as a known gap rather
    than a bug.
    """
    tokens, total = await repo.list_tokens(
        qualified_only=qualified_only, launchpad_slug=launchpad, limit=limit, offset=offset
    )
    if min_tier is not None:
        tokens = [t for t in tokens if t.peak_market_cap is not None and t.peak_market_cap >= min_tier]
    if q:
        needle = q.lower()
        tokens = [
            t for t in tokens
            if needle in (t.name or "").lower()
            or needle in (t.symbol or "").lower()
            or needle in t.mint.lower()
        ]

    items = []
    for t in tokens:
        features = await repo.token_features(t.mint)
        items.append(_token_summary(t, _themes(features)))
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/tokens/{mint}", response_model=TokenDetail)
async def get_token(mint: str, repo: FirestoreRepo = Depends(get_repo)) -> dict[str, Any]:
    token = await repo.get_token(mint)
    if token is None:
        raise HTTPException(status_code=404, detail=f"No token with mint {mint}")

    features = await repo.token_features(mint)
    milestones = await repo.token_milestones(mint)

    return {
        **_token_summary(token, _themes(features)),
        "description": token.description,
        "decimals": token.decimals,
        "total_supply": token.total_supply,
        "ecosystem": token.ecosystem,
        "migrated_at": token.migrated_at,
        "migration_platform": token.migration_platform,
        "destination_dex_slug": token.destination_dex_slug,
        "minutes_launch_to_migration": token.minutes_launch_to_migration,
        "latest_market_cap": token.latest_market_cap,
        "latest_liquidity_usd": token.latest_liquidity_usd,
        "latest_volume_24h_usd": token.latest_volume_24h_usd,
        "latest_holder_count": token.latest_holder_count,
        "market_data_at": token.market_data_at,
        "website": token.website,
        "twitter": token.twitter,
        "telegram": token.telegram,
        "pipeline_stage": token.pipeline_stage,
        "data_sources": token.data_sources or [],
        "qualification_evidence": token.qualification_evidence or {},
        "milestones": [
            {
                "kind": m.kind,
                "threshold_usd": m.threshold_usd,
                "reached_at": m.reached_at,
                "market_cap": m.market_cap,
                "liquidity_usd": m.liquidity_usd,
                "volume_usd": m.volume_usd,
                "holder_count": m.holder_count,
                "token_age_minutes": m.token_age_minutes,
                "evidence": {
                    "source": m.source,
                    "source_type": m.source_type,
                    "observed_at": m.source_observed_at,
                    "verification_status": m.verification_status,
                },
            }
            for m in milestones
        ],
        "features": [
            {
                "namespace": f.namespace,
                "key": f.key,
                "value": f.value,
                "numeric_value": f.numeric_value,
                "source": f.source,
                "confidence": f.confidence,
            }
            for f in features
        ],
        "image_features": None,
        "related_trends": [],
    }


@router.get("/creators", response_model=Page[CreatorSummary])
async def list_creators(
    q: str | None = None,
    repeat_winners: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    repo: FirestoreRepo = Depends(get_repo),
) -> dict[str, Any]:
    creators, total = await repo.list_creators(limit=limit, offset=offset)
    if repeat_winners:
        creators = [c for c in creators if c.is_repeat_winner]
    if q:
        needle = q.lower()
        creators = [c for c in creators if needle in c.wallet.lower()]

    items = [
        {
            "id": c.wallet,
            "wallet": c.wallet,
            "total_launches": c.total_launches,
            "wins_100k": c.wins_100k,
            "wins_250k": c.wins_250k,
            "wins_500k": c.wins_500k,
            "wins_1m": c.wins_1m,
            "success_rate": c.success_rate,
            "best_market_cap": c.best_market_cap,
            "is_repeat_winner": c.is_repeat_winner,
            "first_launch_at": c.first_launch_at,
            "last_launch_at": c.last_launch_at,
            "primary_launchpad_slug": c.primary_launchpad_slug,
        }
        for c in creators
    ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/creators/{wallet}", response_model=CreatorDetail)
async def get_creator(wallet: str, repo: FirestoreRepo = Depends(get_repo)) -> dict[str, Any]:
    creator = await repo.get_creator(wallet)
    if creator is None:
        raise HTTPException(status_code=404, detail=f"No creator {wallet}")

    tokens, _ = await repo.list_tokens(creator_wallet=wallet, limit=50)
    recent = []
    for t in tokens:
        features = await repo.token_features(t.mint)
        recent.append(_token_summary(t, _themes(features)))

    return {
        "id": creator.wallet,
        "wallet": creator.wallet,
        "total_launches": creator.total_launches,
        "wins_100k": creator.wins_100k,
        "wins_250k": creator.wins_250k,
        "wins_500k": creator.wins_500k,
        "wins_1m": creator.wins_1m,
        "success_rate": creator.success_rate,
        "best_market_cap": creator.best_market_cap,
        "is_repeat_winner": creator.is_repeat_winner,
        "first_launch_at": creator.first_launch_at,
        "last_launch_at": creator.last_launch_at,
        "primary_launchpad_slug": creator.primary_launchpad_slug,
        "median_hours_between_launches": creator.median_hours_between_launches,
        "launchpad_history": creator.launchpad_history,
        "recent_tokens": recent,
        "sample": {
            "count": creator.wins_100k,
            "total": creator.total_launches,
            "frequency": creator.success_rate,
        },
    }


@router.get("/launchpads", response_model=Page[LaunchpadSummary])
async def list_launchpads(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    repo: FirestoreRepo = Depends(get_repo),
) -> dict[str, Any]:
    launchpads, total = await repo.list_launchpads(limit=limit, offset=offset)
    items = [
        {
            "id": lp.slug,
            "slug": lp.slug,
            "name": lp.name,
            "lifecycle": lp.lifecycle,
            "launch_count": lp.launch_count,
            "qualified_count": lp.qualified_count,
            "success_rate": lp.success_rate,
            "market_share": lp.market_share,
            "growth_rate_7d": lp.growth_rate_7d,
            "growth_rate_30d": lp.growth_rate_30d,
            "is_known": lp.is_known,
            "first_seen_at": lp.first_seen_at,
            "last_seen_at": lp.last_seen_at,
        }
        for lp in launchpads
    ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/launchpads/{slug}", response_model=LaunchpadDetail)
async def get_launchpad(slug: str, repo: FirestoreRepo = Depends(get_repo)) -> dict[str, Any]:
    lp = await repo.get_launchpad(slug)
    if lp is None:
        raise HTTPException(status_code=404, detail=f"No launchpad {slug}")

    tokens, _ = await repo.list_tokens(launchpad_slug=slug, qualified_only=True, limit=25)
    recent = []
    for t in tokens:
        features = await repo.token_features(t.mint)
        recent.append(_token_summary(t, _themes(features)))

    return {
        "id": lp.slug,
        "slug": lp.slug,
        "name": lp.name,
        "lifecycle": lp.lifecycle,
        "launch_count": lp.launch_count,
        "qualified_count": lp.qualified_count,
        "success_rate": lp.success_rate,
        "market_share": lp.market_share,
        "growth_rate_7d": lp.growth_rate_7d,
        "growth_rate_30d": lp.growth_rate_30d,
        "is_known": lp.is_known,
        "first_seen_at": lp.first_seen_at,
        "last_seen_at": lp.last_seen_at,
        "website": lp.website,
        "ecosystem": lp.ecosystem,
        "discovered_by": lp.discovered_by,
        "median_minutes_to_first_milestone": lp.median_minutes_to_first_milestone,
        "counts_by_tier": lp.counts_by_tier,
        "migration_destinations": [],
        "top_creators": [],
        "recent_tokens": recent,
        "share_history": [],
        "notes": lp.notes,
    }


@router.get("/narratives", response_model=Page[NarrativeSummary])
async def list_narratives(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    repo: FirestoreRepo = Depends(get_repo),
) -> dict[str, Any]:
    narratives, total = await repo.list_narratives(limit=limit, offset=offset)
    items = [
        {
            "id": n.slug,
            "slug": n.slug,
            "label": n.label,
            "category": n.category,
            "token_count": n.token_count,
            "qualified_count": n.qualified_count,
            "share_of_qualified": n.share_of_qualified,
            "baseline_share": n.baseline_share,
            "is_emergent": n.is_emergent,
            "first_seen_at": n.first_seen_at,
            "last_seen_at": n.last_seen_at,
        }
        for n in narratives
    ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}
