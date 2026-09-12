"""Catalogue routes: tokens, creators, launchpads, narratives.

Tokens and creators read the local ledger (app/memory/ledger.py) rather than
Firestore as of the 2026-09-08 rewrite; launchpads and narratives still come
from Firestore, which is correct for them — there are a handful of each, they
change rarely, and they are genuinely shared state.

These endpoints return plain dicts rather than the old response models. The
shapes changed with the storage: a "token" here is a sighting with a peak and
a check count, not a document with a qualification-evidence blob and a
features subcollection.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.schemas import LaunchpadDetail, LaunchpadSummary, NarrativeSummary, Page
from app.db.repo import FirestoreRepo, get_repo

router = APIRouter()


def _themes(name: str | None, symbol: str | None) -> list[str]:
    """Derived on read, never stored.

    Themes used to be rows in a per-token ``features`` subcollection — 13 to
    47 documents each, written on enrichment and re-read on every trend run.
    They come from three short strings via pure functions, so recomputing
    them here costs microseconds and storing them cost the bill.
    """
    from app.analysis.features import extract_all

    return sorted(
        {
            f.value
            for f in extract_all(name, symbol, None)
            if f.namespace == "token" and f.key == "theme" and f.value
        }
    )


#: Below this, a token that once moved is finished. Measured on production:
#: two thirds of everything that had ever cleared a tier was sitting under
#: it, so the page was mostly headstones.
MIN_LIVE_MARKET_CAP = 3_000.0


def _sighting_summary(sighting: Any) -> dict[str, Any]:
    return {
        "id": sighting.mint,
        "mint": sighting.mint,
        "name": sighting.name,
        "symbol": sighting.symbol,
        "launchpad_slug": sighting.launchpad,
        "creator_wallet": sighting.creator,
        "first_seen": sighting.first_seen,
        "qualified_at": sighting.qualified_at,
        "market_cap": sighting.market_cap,
        "peak_market_cap": sighting.peak_market_cap,
        "peak_tier": sighting.tier,
        "is_qualified": bool(sighting.qualified_at),
        "status": sighting.status,
        "checks": sighting.checks,
        "round_tripped": bool(
            sighting.peak_market_cap
            and sighting.market_cap
            and sighting.market_cap < sighting.peak_market_cap * 0.5
        ),
        "themes": _themes(sighting.name, sighting.symbol),
        # The researched half. `themes` above are seeded-vocabulary matches
        # against the name; this is what the coin actually was and why anyone
        # bought it. None until the next cycle's research pass reaches it.
        **_research_summary(sighting.mint),
    }


def _research_summary(mint: str) -> dict[str, Any]:
    from app.memory import coins

    record = coins.get(mint)
    if record is None:
        return {"researched": False}
    return {
        "researched": True,
        "why_it_moved": record.why_it_moved,
        "catalyst": record.catalyst,
        "catalyst_detail": record.catalyst_detail,
        "why_now": record.why_now,
        "category": record.category,
        "confidence": record.confidence,
        "repeatable": record.repeatable,
        "sources": record.sources[:5],
    }


def _top_creators_for(sightings: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    """Rank the creators behind a set of sightings, by launches then winners."""
    from app.memory import ledger

    counts: dict[str, int] = {}
    for row in sightings:
        wallet = row.get("creator_wallet")
        if wallet:
            counts[wallet] = counts.get(wallet, 0) + 1

    ranked = []
    for wallet, seen_here in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]:
        creator = ledger.get_creator(wallet)
        if creator is None:
            continue
        ranked.append({**_creator_summary(creator), "launches_here": seen_here})
    return ranked


def _creator_summary(creator: dict[str, Any]) -> dict[str, Any]:
    launches = creator.get("launches") or 0
    winners = creator.get("winners") or 0
    return {
        "id": creator["wallet"],
        "wallet": creator["wallet"],
        "total_launches": launches,
        "launches_in_window": creator.get("recent_launches"),
        "winners": winners,
        "success_rate": round(winners / launches, 4) if launches else None,
        "best_market_cap": creator.get("best_market_cap"),
        "best_mint": creator.get("best_mint"),
        "is_tracked": bool(creator.get("tracked")),
        "first_seen": creator.get("first_seen"),
        "last_seen": creator.get("last_seen"),
        "dossier_path": creator.get("dossier_path"),
    }


@router.get("/tokens")
async def list_tokens(
    hours: int = Query(168, ge=1, le=2160, description="How far back to look."),
    qualified_only: bool = Query(False),
    launchpad_slug: str | None = Query(None),
    # A token that moved and has come back under this is finished. It stays
    # in the ledger — the brief that reported it and the research explaining
    # why it ran are both still true — but it is not something to be looking
    # at, and two thirds of the qualified list was these.
    min_market_cap: float = Query(MIN_LIVE_MARKET_CAP, ge=0),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Tokens Annie is holding — the ones that moved, not everything sighted.

    Reads the local ledger. What is *not* here is the point: the thousands
    of launches a day that never traded are sighted, counted, and dropped
    within 48 hours, so this list is small and every row in it did
    something. See app/memory/ledger.py.
    """
    from app.memory import ledger

    found = ledger.movers(since_hours=hours, min_market_cap=0.0, limit=(limit + offset) * 4)
    if min_market_cap:
        found = [t for t in found if (t.market_cap or 0) >= min_market_cap]
    if qualified_only:
        found = [t for t in found if t.qualified_at]
    if launchpad_slug:
        found = [t for t in found if t.launchpad == launchpad_slug]

    page = found[offset : offset + limit]
    return {
        "items": [_sighting_summary(t) for t in page],
        "total": len(found),
        "limit": limit,
        "offset": offset,
        "window_hours": hours,
    }


@router.get("/tokens/{mint}")
async def get_token(mint: str) -> dict[str, Any]:
    """One token: its ledger row, its memory file, and where else it appears."""
    from app.memory import index, ledger, service

    sighting = ledger.get_sighting(mint)
    memory = service.read(service.token_path(mint))
    related = index.by_key(mint, limit=5)

    if sighting is None and memory is None and not related:
        raise HTTPException(status_code=404, detail=f"Nothing known about {mint}")

    creator = ledger.get_creator(sighting.creator) if sighting and sighting.creator else None
    return {
        **(_sighting_summary(sighting) if sighting else {"mint": mint}),
        "memory": {"path": memory.path, "body": memory.body} if memory else None,
        "mentioned_in": [h.to_dict() for h in related],
        "creator": creator,
    }


@router.get("/creators")
async def list_creators(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    tracked_only: bool = Query(False),
    # A wallet that has launched forty tokens and produced no winner is a
    # bot, and there are tens of thousands of them. Default on: ranking by
    # volume put the noisiest wallets in the market at the top of the page.
    winners_only: bool = Query(True),
    window_hours: int | None = Query(None, ge=1, le=2160,
                                     description="Omit for lifetime totals."),
) -> dict[str, Any]:
    """The creator leaderboard.

    Every launch by every wallet is recorded — this is complete, not a
    sample. That completeness is affordable precisely because these rows are
    local; as Firestore documents it was the second-largest cost on the bill.
    """
    from app.memory import ledger

    found = ledger.top_creators(
        limit=limit + offset, tracked_only=tracked_only,
        window_hours=window_hours, winners_only=winners_only,
    )
    return {
        "items": [_creator_summary(c) for c in found[offset : offset + limit]],
        "total": len(found),
        "limit": limit,
        "offset": offset,
    }


@router.get("/creators/{wallet}")
async def get_creator(wallet: str) -> dict[str, Any]:
    """One wallet: totals, its tokens, its full movement history, its dossier."""
    from app.memory import ledger, service

    creator = ledger.get_creator(wallet)
    if creator is None:
        raise HTTPException(status_code=404, detail=f"No creator {wallet}")

    dossier = service.read(service.creator_path(wallet))
    return {
        **_creator_summary(creator),
        "tokens": [_sighting_summary(t) for t in ledger.creator_tokens(wallet, limit=50)],
        "movements": ledger.creator_moves(wallet, limit=100),
        "dossier": {"path": dossier.path, "body": dossier.body} if dossier else None,
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

    # Recent tokens come from the ledger, filtered locally. The old version
    # ran a Firestore query and then one feature-subcollection read per
    # token returned — 25 tokens meant 26+ document reads just to render
    # this panel.
    from app.memory import ledger

    recent = [
        _sighting_summary(t)
        for t in ledger.movers(since_hours=720, min_market_cap=0.0, limit=200)
        if t.launchpad == slug
    ][:25]

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
        # Who is actually launching here, tallied from the ledger rows above
        # rather than kept as a denormalised field that would need a write
        # every time anyone launched anything.
        "top_creators": _top_creators_for(recent),
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
