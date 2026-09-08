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
    result = await ideas.generate(
        registry,
        settings,
        brief=str(body.get("brief") or "").strip(),
        count=int(body.get("count") or 3),
    )
    if "error" in result:
        raise HTTPException(status_code=503, detail=result["error"])
    return result


@router.post("/ideas/keep")
async def keep_ideas(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Save an idea set into the playbook.

    Explicit on purpose. Auto-saving every generated idea would fill the
    playbook with unvetted model output and then feed it back in as if it
    were evidence — a memory poisoning itself one cycle at a time.
    """
    payload = body.get("payload")
    if not isinstance(payload, dict) or not payload.get("ideas"):
        raise HTTPException(status_code=422, detail="`payload` must be a generated idea set.")
    path = await ideas.save_as_playbook_entry(payload, note=str(body.get("note") or ""))
    if path is None:
        raise HTTPException(status_code=422, detail="Nothing to save.")
    return {"saved": True, "path": path}


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
