"""Memory routes — Annie's work memory (long-term + daily logs), distinct
from research notes/tasks (app/api/routes/intelligence.py) and chat
history (app/api/routes/annie.py). See app/db/models/research.py's Memory
model and module docstring for how these relate.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.schemas import ConsolidationRunOut, MemoryOut, Page
from app.db.repo import FirestoreRepo, get_repo

router = APIRouter()


@router.get("/memory/consolidation-runs", response_model=Page[ConsolidationRunOut])
async def list_consolidation_runs(
    limit: int = Query(20, ge=1, le=100), repo: FirestoreRepo = Depends(get_repo)
) -> dict[str, Any]:
    """History for the Memory page's Dreams tab — the run itself, distinct
    from its effects (new/archived memories, already visible on Long-Term)."""
    runs = await repo.list_consolidation_runs(limit=limit)
    return {"items": runs, "total": len(runs), "limit": limit, "offset": 0}


@router.get("/memory", response_model=Page[MemoryOut])
async def list_memory(
    type: str | None = Query(None, description="long_term | daily_log"),
    status: str | None = Query(None, description="active | uncertain | superseded | archived"),
    tag: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    repo: FirestoreRepo = Depends(get_repo),
) -> dict[str, Any]:
    memories, total = await repo.list_memories(
        type_=type, status=status, tag=tag, limit=limit, offset=offset
    )
    return {"items": memories, "total": total, "limit": limit, "offset": offset}


@router.get("/memory/{memory_id}", response_model=MemoryOut)
async def get_memory(memory_id: str, repo: FirestoreRepo = Depends(get_repo)) -> Any:
    memory = await repo.get_memory(memory_id)
    if memory is None:
        raise HTTPException(status_code=404, detail=f"No memory {memory_id}")
    return memory


@router.patch("/memory/{memory_id}", response_model=MemoryOut)
async def update_memory(
    memory_id: str, body: dict[str, Any], repo: FirestoreRepo = Depends(get_repo)
) -> Any:
    """Operator edit/archive controls. Only a small, deliberate set of
    fields is writable here — status (e.g. archiving), importance, tags,
    title and content — everything provenance-related (source_type,
    source_id, related_*_ids) is read-only from this endpoint."""
    allowed = {"status", "importance", "tags", "title", "content"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=422, detail=f"Body must contain at least one of {sorted(allowed)}.")

    existing = await repo.get_memory(memory_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"No memory {memory_id}")

    await repo.update_memory(memory_id, **updates)
    return await repo.get_memory(memory_id)


@router.delete("/memory/{memory_id}", status_code=204, response_model=None)
async def delete_memory(memory_id: str, repo: FirestoreRepo = Depends(get_repo)) -> None:
    """Genuine deletion — unlike a research note (never deleted, only
    superseded, per §4/§49's evidence-preservation rule), a memory is
    operator housekeeping, not evidence of record. Archiving
    (`PATCH .../status=archived`) is the reversible alternative when the
    memory might still be worth keeping around."""
    existing = await repo.get_memory(memory_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"No memory {memory_id}")
    await repo.delete_memory(memory_id)
