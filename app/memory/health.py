"""Why is there nothing? — a plain answer, not a set of zeros.

"No data" has several very different causes and they need different actions
from the operator. Left undistinguished, all of them produce the same
unhelpful reply: a list of zeros and a shrug. That reply is honest but it is
not useful, and it makes a broken pipeline look identical to a new one.

The four states this separates:

``never_started``
    Nothing has ever been seen. The webhook is not reaching this deployment
    — wrong URL, wrong secret, or the Helius webhook was never created.

``memory_not_durable``
    Data arrives, but ``ANNIE_MEMORY_DIR`` is not on a mounted volume, so
    every redeploy wipes the ledger and the notebook. Symptom: counts that
    reset to zero after each deploy and a notebook that never grows past its
    four seeded files.

``warming_up``
    The stream is arriving and the ledger is filling, but nothing has
    cleared a tier yet and no cycle has run. Genuinely just new. Nothing to
    fix; it needs hours, not intervention.

``stream_stopped``
    It worked before and has gone quiet. The webhook died — a rotated
    secret, a deleted Helius webhook, or a changed transaction type. This is
    the one that used to be invisible, because a dead webhook and a quiet
    market produce identical numbers.

``healthy``
    Data is arriving and being kept.

Everything here is local reads. It is free, so both Annie and the System
Health page can ask on every request.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.memory import db, index, ledger
from app.memory.paths import CORE_FILES, durability_report

#: A stream this quiet is not a quiet market. Real volume is ~11 launches a
#: minute, so an hour with nothing at all means delivery has stopped, not
#: that nobody launched anything.
QUIET_HOUR_THRESHOLD = 1

#: Below this, "no signals yet" is a sample-size fact rather than a fault —
#: the statistics need a cohort before anything can clear the bars.
MIN_QUALIFIED_FOR_SIGNALS = 20


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age_hours(value: Any) -> float | None:
    parsed = _parse(value)
    if parsed is None:
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds() / 3600


def diagnose() -> dict[str, Any]:
    """What state is this deployment in, and what should be done about it."""
    from app.scheduling.jobs import JOBS
    from app.scheduling.scheduler import job_status

    stats = ledger.stats()
    durability = durability_report()
    memory_files = index.stats()["files"]

    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    last_hour = int(
        db.scalar("SELECT COUNT(*) FROM sightings WHERE first_seen >= ?", (hour_ago,))
    )
    last_sighting_age = _age_hours(stats.get("last_sighting_at"))

    jobs = {j.name: job_status(j.settings_key) for j in JOBS}
    cycle_state = jobs.get("cycle", {})
    last_cycle = cycle_state.get("last_run_at")
    last_watch = jobs.get("watch", {}).get("last_run_at")
    last_result = cycle_state.get("last_result") or {}

    ever_seen = stats["sightings_total"] > 0 or stats["creators_total"] > 0
    # Only the seeded placeholders means she has not written anything of her
    # own yet. Counted from CORE_FILES rather than a literal — this was `<= 4`
    # and silently stopped detecting a wiped volume the moment a fifth core
    # file was added.
    notebook_is_seeded_only = memory_files <= len(CORE_FILES)

    state, headline, actions = _classify(
        ever_seen=ever_seen,
        last_hour=last_hour,
        last_sighting_age=last_sighting_age,
        durable=bool(durability["looks_like_volume"]),
        qualified_total=stats["qualified_total"],
        notebook_is_seeded_only=notebook_is_seeded_only,
        last_cycle=last_cycle,
    )

    return {
        "state": state,
        "headline": headline,
        "what_to_check": actions,
        "stream": {
            "sightings_last_hour": last_hour,
            "last_sighting_at": stats.get("last_sighting_at"),
            "hours_since_last_sighting": (
                round(last_sighting_age, 1) if last_sighting_age is not None else None
            ),
            "expected_rate": "roughly 11 launches a minute at real Pump.fun volume",
        },
        "ledger": {
            "held": stats["sightings_total"],
            "seen_24h": stats["sightings_24h"],
            "qualified_total": stats["qualified_total"],
            "creators_seen": stats["creators_total"],
            "creators_tracked": stats["creators_tracked"],
        },
        "notebook": {
            "files": memory_files,
            "only_seeded_placeholders": notebook_is_seeded_only,
        },
        "durability": durability,
        "jobs": {
            "last_cycle_at": last_cycle,
            "hours_since_cycle": (
                round(_age_hours(last_cycle), 1) if _age_hours(last_cycle) is not None else None
            ),
            "last_cycle_slot": cycle_state.get("last_slot"),
            "last_watch_at": last_watch,
        },
        # The most common reason a brief "never arrives" is that everything
        # ran and there was nowhere to send it. Reported as its own state so
        # it does not need reading out of a log.
        "delivery": _delivery_state(last_result),
    }


def _delivery_state(last_result: dict[str, Any]) -> dict[str, Any]:
    """Whether the last cycle's brief actually went anywhere."""
    if not last_result:
        return {"status": "unknown", "detail": "no cycle has completed yet"}
    if last_result.get("delivered"):
        return {"status": "delivered", "detail": None}
    reason = last_result.get("reason") or "not delivered"
    return {
        "status": "undelivered",
        "detail": reason,
        "fix": (
            "Ask Annie in Discord to create a channel for the morning brief, or "
            "set that purpose on an existing one. Everything else ran — the brief "
            "was written, it just had nowhere to go."
            if "channel" in reason
            else "Set DISCORD_BOT_TOKEN to have briefs delivered."
        ),
    }


def _classify(
    *,
    ever_seen: bool,
    last_hour: int,
    last_sighting_age: float | None,
    durable: bool,
    qualified_total: int,
    notebook_is_seeded_only: bool,
    last_cycle: Any,
) -> tuple[str, str, list[str]]:
    """Pick the one explanation that fits, and say what to do about it.

    Ordered by how much it matters: a deployment that has never received an
    event has a different problem from one whose disk keeps getting wiped,
    and telling someone to attach a volume when their webhook is dead wastes
    their afternoon.
    """
    volume_action = (
        "Attach a Railway Volume (Variables → Volumes → mount at /data) and set "
        "ANNIE_MEMORY_DIR=/data/memory. Without one, every redeploy wipes the "
        "ledger and the notebook."
    )
    webhook_actions = [
        "Check the Helius webhook still points at this deployment's "
        "/api/webhooks/helius URL.",
        "Check HELIUS_WEBHOOK_SECRET here matches the authHeader the webhook "
        "was created with — a mismatch returns 401 and looks exactly like silence.",
        "Look for `helius_webhook_received` in the server logs. If it never "
        "appears, nothing is arriving. If it appears with unparsed>0, the "
        "payload shape changed.",
    ]

    if not ever_seen:
        return (
            "never_started",
            "Nothing has ever arrived. The launch stream is not reaching this "
            "deployment — this is a wiring problem, not a quiet market.",
            webhook_actions + ([volume_action] if not durable else []),
        )

    if not durable and notebook_is_seeded_only:
        return (
            "memory_not_durable",
            "Data is arriving but nothing survives. Memory is not on a mounted "
            "volume, so every redeploy wipes the ledger and the notebook back to "
            "empty.",
            [volume_action],
        )

    if last_hour < QUIET_HOUR_THRESHOLD:
        age = f"{last_sighting_age:.0f}h ago" if last_sighting_age else "unknown"
        return (
            "stream_stopped",
            f"The stream has stopped. Last launch seen {age}, and at real volume "
            f"that should be seconds, not hours.",
            webhook_actions,
        )

    if qualified_total == 0:
        return (
            "warming_up",
            "The stream is arriving and the ledger is filling, but nothing has "
            "cleared a tier yet. This is normal on a new deployment — most "
            "launches never trade, so it takes hours before the first winner.",
            [
                "Nothing to fix. Check back after a full cycle "
                "(00:00 / 06:00 / 12:00 / 18:00 WAT).",
            ],
        )

    if qualified_total < MIN_QUALIFIED_FOR_SIGNALS:
        return (
            "warming_up",
            f"Only {qualified_total} tokens have cleared a tier so far. Signals "
            f"need about {MIN_QUALIFIED_FOR_SIGNALS} before anything is "
            f"statistically meaningful, so there is real data but not yet a real "
            f"finding.",
            ["Nothing to fix — it needs a few days of accumulation."],
        )

    if last_cycle is None:
        return (
            "warming_up",
            "There is data, but no cycle has run yet, so nothing has been written "
            "to the notebook. The cycle is what turns observations into memory.",
            [
                "Trigger one now: System Health → Run now → Cycle. Otherwise it "
                "fires at 00:00 / 06:00 / 12:00 / 18:00 WAT.",
            ],
        )

    return ("healthy", "Data is arriving and being kept.", [])


def summary_line() -> str:
    """One sentence, for putting in front of a model that found nothing."""
    report = diagnose()
    if report["state"] == "healthy":
        return ""
    return f"{report['headline']} ({report['state']})"
