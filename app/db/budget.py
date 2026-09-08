"""A hard ceiling on Firestore usage, enforced in this process.

The Spark (free) plan allows 20,000 document writes and 50,000 reads per
day, project-wide. Those are not soft limits: once hit, Firestore starts
refusing operations, and the failures land wherever the app happens to be —
mid-cycle, mid-conversation, mid-webhook. An intelligence system that
silently stops recording anything at 3pm every day is worse than one that
knows it is at its limit.

So usage is counted locally (SQLite — counting cost must not itself cost
anything) and checked before each write. Over budget, a write is **skipped
and logged**, not retried and not queued: everything routed through here is
either reconstructible or a snapshot that the next cycle will take again
anyway. The markdown memory on disk is unaffected either way, which is the
entire point of the file-first design — Firestore going quiet degrades
durability, never Annie's ability to think.

Budgets come from settings (``FIRESTORE_WRITE_BUDGET_PER_DAY`` /
``FIRESTORE_READ_BUDGET_PER_DAY``) and default well under the plan's caps,
because this process is not the only thing that can touch the project and
because the counter resets on the UTC day boundary while Firestore's own
quota window is its own business.

**This counts what this process does, not what the project does.** A second
deployment, the Firebase console, or a manual script all consume the same
plan quota invisibly to this counter. It is a guard against *this app*
running away — the Firebase console's Usage tab remains the authority on
actual consumption.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, TypeVar

import structlog

from app.config import get_settings
from app.memory import db

log = structlog.get_logger(__name__)

T = TypeVar("T")

WRITE_COUNTER = "firestore_writes"
READ_COUNTER = "firestore_reads"
SKIPPED_COUNTER = "firestore_writes_skipped"


class BudgetExceeded(RuntimeError):
    """Raised only by :func:`require_write`; most callers use :func:`allow_write`."""


def _budget(kind: str) -> int:
    settings = get_settings()
    return (
        settings.firestore_write_budget_per_day
        if kind == "write"
        else settings.firestore_read_budget_per_day
    )


def allow_write(count: int = 1, *, label: str = "") -> bool:
    """Reserve ``count`` writes if there is room. Returns False when there is not.

    Reserving before the write rather than counting after it is deliberate:
    a burst of concurrent webhook handlers all checking a stale counter
    would sail past the cap together. The increment is the reservation.
    """
    used = db.counter_add(WRITE_COUNTER, count)
    limit = _budget("write")
    if used <= limit:
        return True
    db.counter_add(WRITE_COUNTER, -count)  # hand the reservation back
    db.counter_add(SKIPPED_COUNTER, count)
    log.warning(
        "firestore_write_budget_exhausted",
        label=label or "unlabelled",
        used=used - count,
        limit=limit,
        detail="write skipped; memory files on disk are unaffected",
    )
    return False


def note_reads(count: int = 1) -> None:
    """Record reads for visibility. Reads are counted, never blocked.

    Blocking a read would break the API surface an operator uses to *see*
    that they are over budget, which is precisely the wrong moment to go
    blind. Writes are what run away; reads are bounded by traffic.
    """
    if count > 0:
        db.counter_add(READ_COUNTER, count)


async def guarded(
    label: str, count: int, operation: Callable[[], Awaitable[T]]
) -> T | None:
    """Run a Firestore write only if the budget allows. Returns None if skipped.

    A failure inside ``operation`` releases the reservation — a write that
    errored did not consume plan quota, and leaving it counted would make
    the budget drift tighter over time on a flaky connection.
    """
    if not allow_write(count, label=label):
        return None
    try:
        return await operation()
    except BaseException:
        # BaseException, not Exception: a snapshot task cancelled at
        # shutdown (or by asyncio.run closing its loop) would otherwise
        # leave its reservation counted forever, and the budget would drift
        # tighter with every redeploy until writes were refused for no
        # reason. A write that was cancelled or errored consumed no plan
        # quota, so the reservation goes back either way.
        db.counter_add(WRITE_COUNTER, -count)
        raise


def require_write(count: int = 1, *, label: str = "") -> None:
    """Budget check for a write whose caller cannot meaningfully continue without it."""
    if not allow_write(count, label=label):
        raise BudgetExceeded(
            f"Firestore write budget ({_budget('write')}/day) is exhausted; "
            f"{label or 'this write'} was refused. Memory on disk is unaffected."
        )


def report() -> dict[str, Any]:
    """Today's usage against the caps, for System Health."""
    settings = get_settings()
    writes = db.counter_get(WRITE_COUNTER)
    reads = db.counter_get(READ_COUNTER)
    return {
        "day": db.today_utc(),
        "writes": writes,
        "write_budget": settings.firestore_write_budget_per_day,
        "writes_skipped": db.counter_get(SKIPPED_COUNTER),
        "reads": reads,
        "read_budget": settings.firestore_read_budget_per_day,
        "write_headroom_pct": round(
            100 * max(0.0, 1 - writes / max(1, settings.firestore_write_budget_per_day)), 1
        ),
        "spark_plan_daily_caps": {"writes": 20000, "reads": 50000, "deletes": 20000},
        "note": (
            "Counted for this process only. The Firebase console's Usage tab is "
            "the authority on what the project as a whole consumed."
        ),
    }
