"""Ingesting the launch stream — the hot path, and the one that used to cost the most.

Every Pump.fun / LaunchLab creation arrives here from the Helius webhook. At
real volume that is roughly 16,000 events a day, ~11 a minute, bursting far
higher. The previous implementation did one Firestore read plus one Firestore
write per event: ~32,000 operations a day on that path alone, against a Spark
plan that allows 20,000 writes and 50,000 reads *in total*. It did not fit,
and the quota failures it produced showed up as ``RESOURCE_EXHAUSTED`` inside
the webhook handler.

Now an event costs one local SQLite insert and nothing else. No Firestore, no
network, no model. The webhook handler stays fast enough to keep up with
bursts, and the daily cost of ingesting the entire market is zero.

What is *not* dropped: the creator's movement. Every launch by every wallet
gets a ``moves`` row (:func:`app.memory.ledger.record_launch`), which is what
makes "this wallet has launched 47 times this week, and one of them ran to
$2M" answerable later. That was the operator's explicit requirement, and it
is affordable here precisely because these rows are local.

What *is* dropped: everything else about a token that never does anything.
No metadata fetch, no creator resolution, no feature rows, no market lookup
at ingest — those all happen later and only for the small set that earns
them (:mod:`app.pipeline.watch`). A launch is a sighting, not a subject.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

from app.memory import ledger
from app.providers.types import TokenLaunch

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class IngestResult:
    seen: int = 0
    new: int = 0
    repeats: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seen": self.seen,
            "new": self.new,
            "repeats": self.repeats,
            "failed": self.failed,
        }


def ingest(launch: TokenLaunch) -> bool:
    """Record one launch. Returns True if this mint is new.

    Intentionally synchronous: it is a single local insert, so an async
    wrapper would add an await point and a context switch to save nothing.
    """
    return ledger.record_launch(
        mint=launch.mint,
        creator=launch.creator_wallet,
        launchpad=launch.launchpad_slug,
        symbol=launch.symbol,
        name=launch.name,
        signature=launch.signature,
        launched_at=launch.launched_at,
    )


def ingest_many(launches: list[TokenLaunch]) -> IngestResult:
    """Record a webhook delivery's worth of launches.

    One failing event must never drop the rest of the batch — that was a
    real production failure when Firestore quota errors 500'd the whole
    delivery, losing every other event in it and giving Helius an ambiguous
    signal to retry against. Each is wrapped individually and counted.
    """
    result = IngestResult()
    for launch in launches:
        result.seen += 1
        try:
            if ingest(launch):
                result.new += 1
            else:
                result.repeats += 1
        except Exception as exc:
            result.failed += 1
            if len(result.errors) < 5:
                result.errors.append(f"{launch.mint}: {exc}")
            log.warning("stream_ingest_failed", mint=launch.mint, exc_info=True)

    if result.seen:
        log.info("stream_ingested", **result.to_dict())
    return result


def backfill_from_provider_launches(launches: list[TokenLaunch]) -> IngestResult:
    """Same path for the polling backfill (:mod:`app.pipeline.discovery`).

    Kept as one function rather than two so "what a sighting looks like" is
    defined once. Polling remains a supplement to the webhook, not primary
    coverage — Pump.fun's transaction volume means 1,000 signatures covers
    about six seconds.
    """
    return ingest_many(launches)


def recent_activity(minutes: int = 60) -> dict[str, Any]:
    """Cheap liveness view for System Health: is the stream actually arriving?

    A webhook that silently stops (a rotated secret, a deleted Helius
    webhook, a changed transaction type) previously looked identical to a
    quiet market. This makes the difference visible without any remote call.
    """
    from datetime import timedelta

    from app.memory import db

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    return {
        "window_minutes": minutes,
        "sightings": int(
            db.scalar("SELECT COUNT(*) FROM sightings WHERE first_seen >= ?", (cutoff,))
        ),
        "distinct_creators": int(
            db.scalar(
                "SELECT COUNT(DISTINCT creator) FROM sightings "
                "WHERE first_seen >= ? AND creator IS NOT NULL",
                (cutoff,),
            )
        ),
        "last_sighting_at": db.scalar(
            "SELECT MAX(first_seen) FROM sightings", default=None
        ),
    }
