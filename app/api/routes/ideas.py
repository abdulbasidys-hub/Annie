"""Launch-idea generation — the thing the intelligence is for.

Deliberately on-demand only. There is no scheduled job behind this endpoint
because an idea nobody asked for is spend with no reader, and because ideas
are worth generating against the market as it is *when you ask*, not as it
was at 6am.

The grounding is in :mod:`app.memory.ideas`: current movers and signals from
the ledger, plus what memory says has actually worked, plus what is already
crowded. Every idea returned names its evidence and labels itself observed,
inferred or speculative.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.config import Settings, get_settings
from app.memory import ideas
from app.providers.registry import ProviderRegistry, get_registry

router = APIRouter()


@router.post("/ideas")
async def generate_ideas(
    body: dict[str, Any] = Body(default_factory=dict),
    settings: Settings = Depends(get_settings),
    registry: ProviderRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Generate launch ideas from what Annie currently knows.

    ``brief`` is an optional steer ("something in the AI space"). It is
    context, never permission to contradict the evidence — an idea matching
    the brief but unsupported by the data still comes back marked
    speculative.
    """
    brief = str(body.get("brief") or "").strip()
    result = await ideas.generate(
        registry, settings, brief=brief, count=int(body.get("count") or 3)
    )
    if "error" in result:
        raise HTTPException(status_code=503, detail=result["error"])

    # Recorded on generation, not on a separate "keep" press. An idea set the
    # operator never sees again because they forgot to save it is worse than
    # a playbook with a few unremarkable entries in it, and `origin` keeps
    # requested sets distinguishable from the daily ones either way.
    result["memory_path"] = await ideas.record(result, origin="requested", brief=brief)
    return result


# POST /ideas/keep was removed 2026-09-08. Generation now records the set
# itself, so "keep" would have written the same ideas a second time — and an
# idea set the operator never sees again because they forgot to press save is
# worse than a playbook with a few unremarkable entries. Requested and daily
# sets stay distinguishable through `origin`.


@router.get("/ideas/latest")
async def latest_ideas(
    origin: str | None = Query(
        "daily", description="daily | requested. Omit for whichever is newest."
    ),
) -> dict[str, Any]:
    """The most recent idea set, so the page has something without asking.

    Defaults to the daily set — generated after the brief from what moved
    over the preceding 24 hours — because that is the one that arrived on
    its own and is most likely to be the thing someone opening this page
    wants to see.
    """
    found = ideas.latest(origin=origin or None)
    return {"found": found is not None, "ideas": found}


@router.get("/ideas/history")
async def idea_history(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    """Past idea sets. Free — read from the local store, not regenerated."""
    items = ideas.history(limit=limit)
    return {"items": items, "total": len(items)}


@router.get("/ideas/context")
async def idea_context(brief: str = Query("")) -> dict[str, Any]:
    """What an idea call would be grounded in, without making the call.

    Free — everything here is local. Worth looking at before asking for
    ideas on a fresh deployment, where the honest answer is "there is not
    enough history yet for these to be grounded in anything".
    """
    from app.memory import index, ledger, signals

    movers = ledger.movers(since_hours=72, limit=20)
    winning = signals.meaningful(limit=12)
    topics = [s["name"] for s in winning[:5]] + ([brief] if brief else [])
    return {
        "movers": [m.to_dict() for m in movers],
        "winning_characteristics": winning,
        "saturated": [s for s in winning if (s.get("recent_freq") or 0) >= 0.30],
        "memories_that_would_be_used": [h.to_dict() for h in index.recall(topics=topics, budget=8)],
    }
