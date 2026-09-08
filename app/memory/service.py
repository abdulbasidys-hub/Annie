"""The one door into memory. Everything else goes through here.

:mod:`app.memory.files` writes markdown, :mod:`app.memory.index` maintains
retrieval, :mod:`app.memory.snapshot` mirrors to Firestore. Keeping those
three separate makes each testable alone, but it also creates the obvious
failure mode — a caller writing a file and forgetting to index it, so a
memory exists on disk that Annie can never find again.

So nothing outside this package calls those modules directly. Every write
here does all three, in an order chosen for what survives a crash: disk
first (the truth), then the index (rebuildable), then the snapshot
(best-effort, off the critical path).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

import structlog

from app.memory import index, snapshot
from app.memory.files import MemoryFile, MemoryStore
from app.memory.paths import safe_relpath

log = structlog.get_logger(__name__)

_store = MemoryStore()


def store() -> MemoryStore:
    return _store


def read(relpath: str) -> MemoryFile | None:
    return _store.read(relpath)


def exists(relpath: str) -> bool:
    return _store.exists(relpath)


def tree() -> dict[str, list[dict[str, Any]]]:
    return _store.tree()


async def write(
    relpath: str,
    *,
    body: str,
    title: str | None = None,
    kind: str | None = None,
    tags: Iterable[str] = (),
    keys: Iterable[str] = (),
    importance: float = 0.5,
    confidence: str = "medium",
    source: str = "",
    evidence: str = "",
    mirror: bool = True,
) -> MemoryFile:
    """Create or replace a memory. Disk, then index, then snapshot."""
    path = safe_relpath(relpath)
    existing = _store.read(path)
    memory = MemoryFile(
        path=path,
        title=title or (existing.title if existing else path.rsplit("/", 1)[-1].removesuffix(".md")),
        kind=kind or (existing.kind if existing else path.split("/", 1)[0]),
        body=body,
        tags=sorted({*(existing.tags if existing else []), *(t for t in tags if t)}),
        keys=sorted({*(existing.keys if existing else []), *(k for k in keys if k)}),
        importance=importance,
        confidence=confidence,
        created=existing.created if existing else "",
        source=source or (existing.source if existing else ""),
        evidence=evidence or (existing.evidence if existing else ""),
        extra=existing.extra if existing else {},
    )
    written = _store.write(memory)
    index.index_file(written)
    if mirror:
        await snapshot.snapshot_in_background(written)
    return written


async def append(
    relpath: str,
    text: str,
    *,
    heading: str | None = None,
    title: str | None = None,
    tags: Iterable[str] = (),
    keys: Iterable[str] = (),
    importance: float | None = None,
    mirror: bool = True,
) -> MemoryFile:
    """Grow a memory without rewriting it.

    The append path is what keeps AI cost flat as memory grows: a creator
    dossier gets one new paragraph per notable movement rather than being
    regenerated in full, so the model never has to read back and re-emit
    everything it wrote last week.
    """
    path = safe_relpath(relpath)
    memory = _store.read(path)
    if memory is None:
        memory = MemoryFile(
            path=path,
            title=title or path.rsplit("/", 1)[-1].removesuffix(".md"),
            kind=path.split("/", 1)[0],
            importance=importance if importance is not None else 0.5,
        )
    if title:
        memory.title = title
    if importance is not None:
        memory.importance = importance
    memory.tags = sorted({*memory.tags, *(t for t in tags if t)})
    memory.keys = sorted({*memory.keys, *(k for k in keys if k)})

    block = text.strip()
    if heading:
        block = f"### {heading}\n\n{block}"
    memory.body = (memory.body.rstrip() + "\n\n" + block).strip() if memory.body.strip() else block

    written = _store.write(memory)
    index.index_file(written)
    if mirror:
        await snapshot.snapshot_in_background(written)
    return written


async def forget(relpath: str, *, mirror: bool = True) -> bool:
    """Delete a memory outright.

    Real deletion, not archival. A memory system that only ever accumulates
    is a database with extra steps — the ability to decide something was
    wrong and remove it is what keeps the remaining files worth reading.
    """
    path = safe_relpath(relpath)
    removed = _store.delete(path)
    if removed:
        index.drop_file(path)
        if mirror:
            await snapshot.delete_snapshot(path)
        log.info("memory_forgotten", path=path)
    return removed


# -- retrieval passthroughs ---------------------------------------------------


def search(text: str, **kwargs: Any) -> list[index.Hit]:
    return index.search(text, **kwargs)


def by_key(key: str, **kwargs: Any) -> list[index.Hit]:
    return index.by_key(key, **kwargs)


def recall(**kwargs: Any) -> list[index.Hit]:
    return index.recall(**kwargs)


def core_context(limit: int = 6) -> list[index.Hit]:
    return index.core_context(limit)


def stats() -> dict[str, Any]:
    return index.stats()


# -- conventional paths -------------------------------------------------------
# Centralised so the daily job, the rollup, the agent tools and the API all
# agree on where a given memory lives. A creator dossier named two different
# ways in two places is a memory that silently forks.


def daily_path(day: datetime) -> str:
    return f"daily/{day.astimezone(timezone.utc).date().isoformat()}.md"


def weekly_path(day: datetime) -> str:
    iso = day.astimezone(timezone.utc).isocalendar()
    return f"weekly/{iso.year}-W{iso.week:02d}.md"


def monthly_path(day: datetime) -> str:
    stamp = day.astimezone(timezone.utc)
    return f"monthly/{stamp.year}-{stamp.month:02d}.md"


def creator_path(wallet: str) -> str:
    return f"creators/{wallet}.md"


def token_path(mint: str) -> str:
    return f"tokens/{mint}.md"


def narrative_path(name: str) -> str:
    from app.memory.paths import slug

    return f"narratives/{slug(name)}.md"
