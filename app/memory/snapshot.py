"""Mirroring memory files to Firestore, so a wiped container is not a lobotomy.

The markdown files are the truth and SQLite is disposable, but both live on
a disk that Railway replaces on every redeploy unless a Volume is attached.
This module is the safety net: one Firestore document per memory file, in a
``memory_files`` collection.

Two properties make it affordable on Spark:

* **Written only on change.** Each document carries the file's content hash;
  a snapshot compares hashes and writes nothing for a file that did not
  move. A quiet cycle costs zero writes, a busy one costs a handful.
* **Read only on boot, and only when needed.** :func:`restore` runs at
  startup and does nothing at all if the local directory already has files.
  A deployment with a healthy Volume never reads this collection.

Realistic cost: a few hundred memory files total, of which maybe 5-30 change
per cycle. That is tens of writes a day against a 20,000/day cap — three
orders of magnitude below what the old ``tokens`` collection was doing, and
every one of them buys durability for something Annie actually reasoned
about rather than a token that existed for four minutes.

Everything here is best-effort. A snapshot failure is logged and swallowed:
the files on disk are already written by the time this runs, so a Firestore
outage costs a backup, never a memory.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from app.db import budget
from app.memory import db
from app.memory.files import MemoryFile, MemoryStore, parse
from app.memory.paths import safe_relpath

log = structlog.get_logger(__name__)

COLLECTION = "memory_files"

#: Strong references to in-flight background snapshot tasks. See
#: :func:`snapshot_in_background`.
_pending: set[asyncio.Task] = set()

#: Firestore document IDs cannot contain "/", so ``core/market-model.md``
#: is stored as ``core__market-model.md``. Reversible, and readable in the
#: Firebase console, which matters when you are looking for a file by eye.
_SEP = "__"


def _doc_id(relpath: str) -> str:
    return relpath.replace("/", _SEP)


def _relpath(doc_id: str) -> str:
    return doc_id.replace(_SEP, "/")


def _client() -> Any:
    from app.db.firestore import get_client

    return get_client()


def is_configured() -> bool:
    """Whether this deployment has Firestore credentials to mirror to at all.

    Checked before every snapshot call rather than discovered by letting the
    client raise. A deployment without credentials (a local run, a test, a
    smoke script) should skip mirroring silently and cheaply — not attempt
    it, reserve budget, throw, and log a warning on every single memory
    write. Memory on disk is fully functional without it; only durability
    across a container wipe is lost, which is exactly what
    ``paths.durability_report`` already reports on System Health.
    """
    from app.config import get_settings

    settings = get_settings()
    return bool(
        (settings.firebase_service_account_json or "").strip()
        or (settings.firebase_service_account_file or "").strip()
        or (settings.firebase_project_id or "").strip()
    )


async def snapshot_file(memory: MemoryFile) -> bool:
    """Mirror one file if its content changed. Returns True if it wrote.

    The hash is kept locally in ``docs.hash`` (already maintained by the
    search index) and compared against a small ``kv`` entry, so deciding
    "has this changed" costs no Firestore read at all — the naive version of
    this, reading the remote doc to compare, would have doubled the op count
    for no benefit.
    """
    if not is_configured():
        return False

    marker = f"snapshot:{memory.path}"
    current = memory.content_hash
    if db.kv_get(marker) == current:
        return False

    async def write() -> bool:
        await _client().collection(COLLECTION).document(_doc_id(memory.path)).set(
            {
                "path": memory.path,
                "section": memory.section,
                "title": memory.title,
                "hash": current,
                "updated": memory.updated,
                "content": memory.render(),
            }
        )
        return True

    try:
        wrote = await budget.guarded(f"memory_snapshot:{memory.section}", 1, write)
    except Exception:
        log.warning("memory_snapshot_failed", path=memory.path, exc_info=True)
        return False

    if wrote:
        db.kv_set(marker, current)
        return True
    return False


async def delete_snapshot(relpath: str) -> None:
    if not is_configured():
        return

    async def drop() -> None:
        await _client().collection(COLLECTION).document(_doc_id(relpath)).delete()

    try:
        await budget.guarded(f"memory_snapshot_delete:{relpath}", 1, drop)
    except Exception:
        log.warning("memory_snapshot_delete_failed", path=relpath, exc_info=True)
    db.kv_set(f"snapshot:{relpath}", "")


async def snapshot_all(store: MemoryStore | None = None, *, limit: int = 200) -> dict[str, int]:
    """Mirror every changed file. Called at the end of each cycle.

    ``limit`` caps one pass so that a first run against a large existing
    memory cannot consume the day's whole write budget in one go — the next
    cycle picks up where this left off, because unchanged files are skipped
    by hash on the way through.
    """
    store = store or MemoryStore()
    if not is_configured():
        return {"written": 0, "unchanged": 0, "skipped": "firestore not configured"}
    written = skipped = 0
    for memory in store.load_all():
        if written >= limit:
            break
        if await snapshot_file(memory):
            written += 1
        else:
            skipped += 1
    if written:
        log.info("memory_snapshot_complete", written=written, unchanged=skipped)
    return {"written": written, "unchanged": skipped}


async def restore(store: MemoryStore | None = None, *, force: bool = False) -> dict[str, Any]:
    """Rebuild the local memory directory from Firestore, if it is empty.

    Runs once at startup. The emptiness check is what keeps this from being
    a per-boot cost on a healthy deployment: with a Volume attached, the
    files are already there and this returns immediately having read
    nothing.

    ``force`` is the operator's "my volume is stale, pull the real thing"
    button. It overwrites local files with the snapshot, so it is never
    automatic — a Volume that is merely *newer* than the snapshot would lose
    work to it.
    """
    store = store or MemoryStore()
    existing = list(store.iter_paths())
    if existing and not force:
        return {"restored": 0, "reason": f"{len(existing)} local memory files already present"}
    if not is_configured():
        return {"restored": 0, "reason": "firestore not configured — nothing to restore from"}

    try:
        docs = [snap async for snap in _client().collection(COLLECTION).stream()]
    except Exception:
        log.warning("memory_restore_failed", exc_info=True)
        return {"restored": 0, "reason": "Firestore unreachable — starting with empty memory"}

    budget.note_reads(len(docs))
    restored = 0
    for snap in docs:
        data = snap.to_dict() or {}
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        try:
            relpath = safe_relpath(data.get("path") or _relpath(snap.id))
            memory = parse(relpath, content)
            store.write(memory)
            db.kv_set(f"snapshot:{relpath}", memory.content_hash)
            restored += 1
        except Exception:
            log.warning("memory_restore_file_failed", doc=snap.id, exc_info=True)

    log.info("memory_restored_from_snapshot", files=restored)
    return {"restored": restored, "reason": "local memory was empty" if not force else "forced"}


async def snapshot_in_background(memory: MemoryFile) -> None:
    """Fire-and-forget mirror, used by the write path.

    A memory write must not block on a network round trip — the file is
    already durable on disk at that point, and making the API's "save this
    note" endpoint wait on Firestore would be paying latency for a backup.
    """
    if not is_configured():
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # called from sync context (a test, a CLI) — nothing to schedule onto

    # Held in a module-level set: asyncio only keeps a weak reference to a
    # task, so a fire-and-forget one can be garbage-collected mid-flight and
    # simply never run. Discarded on completion.
    task = asyncio.create_task(snapshot_file(memory), name=f"snapshot_{memory.path}")
    _pending.add(task)
    task.add_done_callback(_pending.discard)
