"""Memory routes — reading, searching and editing Annie's notebook.

This is the surface the website's Memory page and the `tools/memory_pull.py`
script use, and it is deliberately shaped like a filesystem rather than a
record store: browse a tree, open a file, get raw markdown back. That is the
point of the rewrite — memory you can read directly, not rows you need an app
to interpret.

Everything here is local: file reads and SQLite lookups, no Firestore, no
model. So the Memory page is free to poll and free to browse.

Writes go through :mod:`app.memory.service`, which indexes and mirrors as a
side effect, so an operator editing a file by hand through the UI gets the
same treatment as one Annie wrote herself.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Response

from app.memory import digest as digest_module
from app.memory import index, ledger, service, signals
from app.memory.paths import SECTIONS, MemoryPathError, durability_report, safe_relpath

router = APIRouter()


@router.get("/memory")
async def memory_tree() -> dict[str, Any]:
    """The whole notebook, grouped by section. Metadata only, no bodies."""
    grouped = service.tree()
    return {
        "sections": [
            {
                "name": name,
                "description": description,
                "files": grouped.get(name, []),
                "count": len(grouped.get(name, [])),
            }
            for name, description in SECTIONS.items()
        ],
        "total_files": sum(len(v) for v in grouped.values()),
        "index": index.stats(),
        "durability": durability_report(),
    }


@router.get("/memory/search")
async def memory_search(
    q: str = Query(..., min_length=1, description="Text, or an exact mint/wallet/ticker."),
    limit: int = Query(8, ge=1, le=25),
    section: str | None = Query(None),
) -> dict[str, Any]:
    """Search memory. Exact-handle lookup first, ranked full text second.

    Passing a contract address or creator wallet here is the fast path — it
    resolves through the key index in a single probe rather than scanning
    text, which is what makes "what do you know about this CA" cheap enough
    to be a normal thing to ask.
    """
    hits = index.search(q, limit=limit, section=section)
    return {
        "query": q,
        "hits": [h.to_dict() for h in hits],
        "matched_by": "key" if hits and hits[0].matched_key else "text",
        "count": len(hits),
    }


@router.get("/memory/digest")
async def memory_digest(window_hours: int = Query(6, ge=1, le=168)) -> dict[str, Any]:
    """Exactly what the next cycle would see, before any model call.

    Useful for two things: understanding why Annie wrote what she wrote, and
    confirming that a cycle is cheap — the ``prompt_chars`` figure is the
    real input size that becomes tokens.
    """
    built = digest_module.build(window_hours=window_hours)
    rendered = built.render()
    return {
        "facts": built.facts(),
        "prompt": rendered,
        "prompt_chars": len(rendered),
        "approx_input_tokens": len(rendered) // 4,
        "would_call_model": not built.is_empty,
    }


@router.get("/memory/file")
async def read_memory_file(
    path: str = Query(..., description="e.g. core/market-model.md"),
    raw: bool = Query(False, description="Return text/markdown instead of JSON."),
) -> Any:
    """One memory file, whole."""
    try:
        relpath = safe_relpath(path)
    except MemoryPathError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    memory = service.read(relpath)
    if memory is None:
        raise HTTPException(status_code=404, detail=f"No memory file at {relpath}")

    if raw:
        return Response(content=memory.render(), media_type="text/markdown; charset=utf-8")
    return {**memory.to_dict(), "body": memory.body}


@router.post("/memory/file")
async def write_memory_file(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Create or replace a memory file by hand.

    The operator editing Annie's memory directly is a supported, intended
    action — correcting something she got wrong is far more useful than
    arguing with her about it in chat, and it goes through the same indexing
    and mirroring path as her own writes.
    """
    path = str(body.get("path") or "")
    content = body.get("body")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=422, detail="`body` (markdown) is required.")

    try:
        memory = await service.write(
            path,
            body=content,
            title=body.get("title"),
            tags=[str(t) for t in (body.get("tags") or [])],
            keys=[str(k) for k in (body.get("keys") or [])],
            importance=float(body.get("importance") or 0.5),
            confidence=str(body.get("confidence") or "medium"),
            source="operator",
        )
    except MemoryPathError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {**memory.to_dict(), "body": memory.body}


@router.post("/memory/append")
async def append_memory_file(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=422, detail="`text` is required.")
    try:
        memory = await service.append(
            str(body.get("path") or ""),
            text,
            heading=body.get("heading"),
            title=body.get("title"),
            tags=[str(t) for t in (body.get("tags") or [])],
            keys=[str(k) for k in (body.get("keys") or [])],
        )
    except MemoryPathError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {**memory.to_dict(), "body": memory.body}


@router.delete("/memory/file", status_code=204, response_model=None)
async def delete_memory_file(path: str = Query(...)) -> None:
    """Delete a memory outright.

    Genuine deletion, not archival — a memory system that only accumulates
    is a database with extra steps. Research notes remain evidence and are
    never deleted (see app/api/routes/intelligence.py); this is Annie's own
    notebook, which she and the operator are both allowed to prune.
    """
    try:
        removed = await service.forget(path)
    except MemoryPathError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if not removed:
        raise HTTPException(status_code=404, detail=f"No memory file at {path}")


@router.post("/memory/reindex")
async def reindex() -> dict[str, Any]:
    """Rebuild the search index from the files.

    The escape hatch that makes the index safe to treat as disposable — any
    corruption, hand-edit outside the app, or schema change is fixed here
    rather than by a migration.
    """
    count = index.reindex_all(service.store())
    return {"reindexed": count, "index": index.stats()}


@router.post("/memory/restore")
async def restore_from_snapshot(force: bool = Query(False)) -> dict[str, Any]:
    """Pull memory back from the Firestore mirror.

    Without ``force`` this does nothing when local files already exist —
    that is the automatic boot behaviour. With it, the snapshot overwrites
    local files, which is the "my volume is stale, pull the real thing"
    action and is never automatic: a volume that is merely *newer* than the
    snapshot would lose work to it.
    """
    from app.memory import snapshot

    result = await snapshot.restore(service.store(), force=force)
    index.sync_if_stale(service.store())
    return {**result, "index": index.stats()}


# -----------------------------------------------------------------------------
# The ledger — what memory is built from
# -----------------------------------------------------------------------------


@router.get("/ledger/stats")
async def ledger_stats() -> dict[str, Any]:
    from app.db import budget
    from app.pipeline import stream

    return {
        "ledger": ledger.stats(),
        "signals": signals.counts(),
        "memory": index.stats(),
        "stream": stream.recent_activity(60),
        "firestore": budget.report(),
    }


@router.get("/ledger/movers")
async def ledger_movers(
    hours: int = Query(24, ge=1, le=336), limit: int = Query(50, ge=1, le=200)
) -> dict[str, Any]:
    found = ledger.movers(since_hours=hours, limit=limit)
    return {"items": [s.to_dict() for s in found], "total": len(found), "window_hours": hours}


@router.get("/ledger/creators")
async def ledger_creators(
    limit: int = Query(50, ge=1, le=200),
    tracked_only: bool = Query(False),
    window_hours: int | None = Query(None, ge=1, le=720),
) -> dict[str, Any]:
    """Creator leaderboard.

    ``window_hours`` switches from lifetime totals to "who is busy right
    now", which is the question that actually decides who to watch.
    """
    found = ledger.top_creators(limit=limit, tracked_only=tracked_only, window_hours=window_hours)
    return {"items": found, "total": len(found)}


@router.get("/ledger/creators/{wallet}")
async def ledger_creator_detail(wallet: str, moves: int = Query(60, ge=1, le=500)) -> dict[str, Any]:
    creator = ledger.get_creator(wallet)
    if creator is None:
        raise HTTPException(status_code=404, detail=f"No creator {wallet}")
    dossier = service.read(service.creator_path(wallet))
    return {
        "creator": creator,
        "tokens": [t.to_dict() for t in ledger.creator_tokens(wallet)],
        # "movements", matching /api/creators/{wallet} and the language the
        # ledger itself uses. Two names for one thing across two routes is
        # how a frontend ends up rendering an empty panel.
        "movements": ledger.creator_moves(wallet, limit=moves),
        "dossier": {"path": dossier.path, "body": dossier.body} if dossier else None,
    }


@router.get("/ledger/token/{mint}")
async def ledger_token_detail(mint: str) -> dict[str, Any]:
    """Everything known about one mint: ledger row, memory file, related memories."""
    sighting = ledger.get_sighting(mint)
    related = index.by_key(mint, limit=5)
    memory = _memory_for_mint(mint, related)

    if sighting is None and memory is None and not related:
        raise HTTPException(status_code=404, detail=f"Nothing known about {mint}")
    return {
        "sighting": sighting.to_dict() if sighting else None,
        "memory": {"path": memory.path, "body": memory.body} if memory else None,
        "related_memories": [
            h.to_dict() for h in related if memory is None or h.path != memory.path
        ],
    }


def _memory_for_mint(mint: str, related: list[Any]) -> Any:
    """The file about this token, whatever it happens to be called.

    The conventional path (``tokens/<mint>.md``) is tried first, but Annie
    names files herself during a cycle and may well have called it
    ``tokens/moon-cat.md``. Falling back to the strongest key hit means a
    lookup by contract address finds her file either way — which is the
    whole promise of writing the CA into the ``keys`` header.
    """
    exact = service.read(service.token_path(mint))
    if exact is not None:
        return exact
    for hit in related:
        if hit.section == "tokens":
            return service.read(hit.path)
    return None


# Signals used to be served from here as well as from
# app/api/routes/intelligence.py — two routers registering /api/signals and
# /api/signals/{slug} with different shapes, where which one answered came
# down to include order in app/main.py. intelligence.py owns them now, since
# it also owns the /trends aliases and the serializer the frontend renders.
# Recomputation lives at POST /api/system/run/signals with the other manual
# stage triggers.
