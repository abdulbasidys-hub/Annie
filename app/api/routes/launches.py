"""Our own launches — register one, read the record, close it out.

The rest of the API exposes what Annie found. This exposes what we did, and
the asymmetry is deliberate: a launch of ours is tracked because we said so,
not because it cleared a bar, so registering one is an operator action rather
than something the pipeline can infer.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.config import Settings, get_settings
from app.providers.registry import ProviderRegistry, get_registry

router = APIRouter()


@router.get("/launches")
async def list_launches(
    status: str | None = Query(None, description="live, graduated, dead, abandoned"),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """Everything we have launched, newest first."""
    from app.memory import launches, ledger

    rows = launches.listing(status=status, limit=limit)
    out = []
    for launch in rows:
        sighting = ledger.get_sighting(launch.mint)
        out.append(
            {
                **launch.to_dict(),
                "market_cap": sighting.market_cap if sighting else None,
                "peak_market_cap": (
                    sighting.peak_market_cap if sighting else launch.peak_market_cap
                ),
                "qualified_at": sighting.qualified_at if sighting else None,
            }
        )
    return {"items": out, "total": len(out)}


@router.get("/launches/{mint}")
async def get_launch(mint: str) -> dict[str, Any]:
    """One launch, with its ledger row and the written record underneath."""
    from app.memory import launches, ledger, service

    launch = launches.get(mint)
    if launch is None:
        raise HTTPException(status_code=404, detail="Not one of ours.")

    sighting = ledger.get_sighting(mint)
    memory = service.read(launches.launch_path(mint))
    return {
        **launch.to_dict(),
        "market_cap": sighting.market_cap if sighting else None,
        "peak_market_cap": sighting.peak_market_cap if sighting else None,
        "first_seen": sighting.first_seen if sighting else None,
        "record": memory.body if memory else "",
    }


@router.post("/launches", status_code=201)
async def register_launch(
    body: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Tell Annie a mint is ours.

    From this point it is exempt from every filter in the system: never
    pruned, re-priced ahead of everything else, and reviewed in writing on
    every cycle regardless of what it is worth.
    """
    from app.memory import launches

    mint = str(body.get("mint") or "").strip()
    if not mint or len(mint) < 32:
        raise HTTPException(
            status_code=422,
            detail="mint must be a Solana contract address.",
        )

    launch = await launches.register(
        mint,
        name=str(body.get("name") or "").strip(),
        ticker=str(body.get("ticker") or "").strip().upper(),
        note=str(body.get("note") or "").strip(),
        idea_id=body.get("idea_id"),
    )
    return launch.to_dict()


@router.patch("/launches/{mint}")
async def update_launch(mint: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Close one out, or reopen it. Status drives whether it is still reviewed."""
    from app.memory import launches

    status = str(body.get("status") or "").strip()
    if status not in launches.STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {', '.join(launches.STATUSES)}.",
        )

    launch = launches.set_status(mint, status)
    if launch is None:
        raise HTTPException(status_code=404, detail="Not one of ours.")
    return launch.to_dict()


@router.post("/launches/{mint}/review")
async def review_launch(
    mint: str,
    registry: ProviderRegistry = Depends(get_registry),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Force a check-in now rather than waiting for the next cycle."""
    from app.memory import launches

    launch = launches.get(mint)
    if launch is None:
        raise HTTPException(status_code=404, detail="Not one of ours.")

    result = await launches.review_one(registry, settings, launch)
    if result is None:
        raise HTTPException(status_code=502, detail="The review call failed — see logs.")
    return result
