"""Daily, weekly and monthly summarisation — and the creator dossiers.

Two kinds of writing happen here, and keeping them separate is the point:

**Deterministic** (:func:`write_daily_log`, :func:`update_creator_dossier`).
Counts, tiers, wallets, contract addresses. Every line is a direct query
result, so there is nothing for a model to get wrong and no reason to pay one.
This is also what guarantees the operator's requirement that contract
addresses and creator wallets are always in memory and always retrievable —
that must not depend on an LLM remembering to include a field.

**Synthesised** (:func:`write_weekly_summary`, :func:`write_monthly_summary`).
One model call each, reading the period's own files rather than the raw
ledger. A week's summary reads seven daily files; a month's reads four or
five weeklies. That cascade is what keeps cost flat as history grows — the
monthly call never sees a single token row, and next year's summaries cost
exactly what this year's did.

The compaction is real, not cosmetic. After a month, the ~120 cycle
observations behind it are represented by one monthly file plus whatever
durable lessons earned a place in ``core/`` or ``playbook/``. The dailies
underneath can be pruned (:func:`prune_old_dailies`) because everything worth
keeping has already been lifted up a level.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from app.config import Settings
from app.memory import ledger, service, signals
from app.memory.files import MemoryStore
from app.providers.registry import ProviderRegistry

log = structlog.get_logger(__name__)

#: Daily files older than this are deleted, after the weekly that covers
#: them exists. Ninety days of dailies is already more history than any
#: cycle reads; the weeks and months above them are the durable record.
DAILY_RETENTION_DAYS = 90


def _usd(value: float | None) -> str:
    if not value:
        return "?"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}k"
    return f"${value:.0f}"


# -----------------------------------------------------------------------------
# Deterministic
# -----------------------------------------------------------------------------


#: How many of the day's qualifiers get named in the daily log. Every one of
#: them is in the ledger regardless; this is a bound on how long the file a
#: person actually reads is allowed to get.
DAILY_LOG_TOKENS = 50


async def write_daily_log(*, now: datetime | None = None) -> dict[str, Any]:
    """The day's factual record. No model, no judgement, no hedging needed.

    Deliberately written as a *replacement* of the day's file rather than an
    append: the cycle-learning step already appends its observations to this
    same file during the day, so this runs at the day boundary and puts the
    settled numbers at the top of what is already there.
    """
    now = now or datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if now.hour < 12:  # running just after midnight summarises the day that ended
        day_start -= timedelta(days=1)
    day_end = day_start + timedelta(days=1)

    qualified = ledger.qualified_in_window(day_start, day_end)
    movers = ledger.movers(since_hours=24, limit=15)
    busy = ledger.top_creators(limit=8, window_hours=24)
    stats = ledger.stats()

    lines = [
        f"Launches sighted: {stats['sightings_24h']}. "
        f"Reached a tier: {len(qualified)}. "
        f"Currently watching: {stats['watching']}. "
        f"Creator movements recorded: {stats['moves_24h']}.",
    ]

    if qualified:
        # Ordered by peak, so a cut here keeps the biggest. At real Solana
        # volume this list can be four figures long; naming that in the file
        # matters, because a reader who sees fifty entries under a heading
        # that says 1,400 should know the rest is in the ledger and reachable
        # by asking, not that the day was mis-recorded.
        shown = qualified[:DAILY_LOG_TOKENS]
        heading = "## Qualified today"
        if len(qualified) > len(shown):
            heading += f" — top {len(shown)} of {len(qualified)}"
        lines += ["", heading]
        for token in shown:
            lines.append(
                f"- **{token.symbol or token.name or token.mint[:8]}** — "
                f"crossed {_usd(token.tier)}, peaked {_usd(token.peak_market_cap)} "
                f"on {token.launchpad or 'unknown'}\n"
                f"  - CA: `{token.mint}`\n"
                f"  - Creator: `{token.creator or 'unknown'}`"
            )
    else:
        lines += ["", "## Qualified today", "", "_None crossed a tier._"]

    if movers:
        lines += ["", "## Biggest movers (including ones that round-tripped)"]
        for token in movers[:10]:
            lines.append(
                f"- {token.symbol or token.mint[:8]}: peak {_usd(token.peak_market_cap)}, "
                f"now {_usd(token.market_cap)} — `{token.mint}`"
            )

    if busy:
        lines += ["", "## Most active creators"]
        for creator in busy:
            lines.append(
                f"- `{creator['wallet']}` — {creator.get('recent_launches', 0)} launches today, "
                f"{creator.get('winners', 0)} winners lifetime, best {_usd(creator.get('best_market_cap'))}"
            )

    if stats.get("launchpads"):
        lines += ["", "## Launchpad share"]
        for pad in stats["launchpads"][:6]:
            lines.append(f"- {pad['launchpad']}: {pad['n']}")

    path = service.daily_path(day_start)
    existing = service.read(path)
    observations = _extract_observations(existing.body) if existing else ""

    body = "\n".join(lines)
    if observations:
        body += "\n\n## Observations recorded during the day\n\n" + observations

    keys = [t.mint for t in shown] + [t.creator for t in shown if t.creator] if qualified else []
    await service.write(
        path,
        body=body,
        title=f"Daily log — {day_start.date().isoformat()}",
        kind="daily",
        tags=["daily", day_start.date().isoformat()],
        keys=keys,
        importance=0.35,
        confidence="high",
        source="deterministic",
    )
    return {
        "path": path,
        "qualified": len(qualified),
        "movers": len(movers),
        "sightings_24h": stats["sightings_24h"],
    }


def _extract_observations(body: str) -> str:
    """Keep the day's cycle-written notes when the deterministic half is rewritten.

    The learning step appends timestamped ``### YYYY-MM-DD HH:MM UTC`` blocks
    to the daily file as the day goes. Those are the interesting half and
    must survive the end-of-day rewrite of the counts above them.
    """
    kept: list[str] = []
    capturing = False
    for line in body.splitlines():
        if line.startswith("### ") and "UTC" in line:
            capturing = True
        elif line.startswith("## "):
            capturing = False
        if capturing:
            kept.append(line)
    return "\n".join(kept).strip()


async def update_creator_dossier(wallet: str, *, reason: str = "") -> str | None:
    """Write or refresh one creator's file. Deterministic, so it is free.

    Only called for wallets the ledger has marked ``tracked`` — repeat
    launchers and anyone who has produced a winner. Writing a dossier for
    every wallet that ever launched once would recreate, in markdown, exactly
    the per-token document explosion this rewrite removed.
    """
    creator = ledger.get_creator(wallet)
    if creator is None:
        return None

    tokens = ledger.creator_tokens(wallet, limit=25)
    moves = ledger.creator_moves(wallet, limit=40)
    winners = [t for t in tokens if t.qualified_at]

    lines = [
        f"Wallet: `{wallet}`",
        "",
        f"- First seen: {creator['first_seen']}",
        f"- Last seen: {creator['last_seen']}",
        f"- Launches recorded: {creator['launches']}",
        f"- Tokens that reached a tier: {creator['winners']}",
        f"- Best result: {_usd(creator.get('best_market_cap'))}"
        + (f" (`{creator['best_mint']}`)" if creator.get("best_mint") else ""),
    ]
    if creator["launches"]:
        hit_rate = 100 * (creator["winners"] or 0) / creator["launches"]
        lines.append(f"- Hit rate: {hit_rate:.1f}% of launches reached a tier")
    if reason:
        lines.append(f"- Why tracked: {reason}")

    if winners:
        lines += ["", "## Winners"]
        for token in winners:
            lines.append(
                f"- {token.symbol or token.name or token.mint[:8]} — "
                f"{_usd(token.peak_market_cap)} peak, crossed {_usd(token.tier)} "
                f"on {token.qualified_at}\n  - CA: `{token.mint}`"
            )

    recent = [m for m in moves if m["kind"] == "launch"][:15]
    if recent:
        lines += ["", "## Recent launches"]
        for move in recent:
            lines.append(f"- {move['at']} — `{move['mint']}`")

    path = service.creator_path(wallet)
    await service.write(
        path,
        body="\n".join(lines),
        title=f"Creator {wallet[:8]}…{wallet[-4:]}",
        kind="creator",
        tags=["creator", "tracked" if creator.get("tracked") else "seen"],
        keys=[wallet] + [t.mint for t in winners[:10]],
        importance=0.6 if creator["winners"] else 0.45,
        confidence="high",
        source="deterministic",
    )
    ledger.set_dossier_path(wallet, path)
    return path


async def write_token_memory(mint: str, *, note: str = "") -> str | None:
    """A file for a token that actually did something.

    Carries the CA and the creator wallet in both the body and the ``keys``
    header, which is what makes "look up this contract address" a single
    index probe from chat — the operator's explicit requirement that CA and
    creator go to memory rather than being derivable only from a website.
    """
    token = ledger.get_sighting(mint)
    if token is None:
        return None

    lines = [
        f"**{token.symbol or token.name or mint[:8]}**",
        "",
        f"- CA: `{mint}`",
        f"- Creator: `{token.creator or 'unknown'}`",
        f"- Launchpad: {token.launchpad or 'unknown'}",
        f"- First seen: {token.first_seen}",
        f"- Peak market cap: {_usd(token.peak_market_cap)}",
        f"- Tier crossed: {_usd(token.tier)}" if token.tier else "- Tier crossed: none",
    ]
    if token.qualified_at:
        lines.append(f"- Qualified at: {token.qualified_at}")
    if token.market_cap and token.peak_market_cap and token.market_cap < token.peak_market_cap * 0.5:
        lines.append(
            f"- Round-tripped: currently {_usd(token.market_cap)}, "
            f"{100 * (1 - token.market_cap / token.peak_market_cap):.0f}% off peak"
        )
    if note:
        lines += ["", note]

    path = service.token_path(mint)
    await service.write(
        path,
        body="\n".join(lines),
        title=f"{token.symbol or token.name or mint[:8]} ({mint[:6]}…)",
        kind="token",
        tags=["token", token.launchpad or "unknown-pad"],
        keys=[mint] + ([token.creator] if token.creator else []) + (
            [token.symbol.lower()] if token.symbol else []
        ),
        importance=0.55 if token.tier else 0.4,
        confidence="high",
        source="deterministic",
    )
    return path


# -----------------------------------------------------------------------------
# Synthesised
# -----------------------------------------------------------------------------

ROLLUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "what_changed", "lessons", "drop"],
    "properties": {
        "summary": {"type": "string", "description": "The period in a few paragraphs of prose."},
        "what_changed": {
            "type": "string",
            "description": "What is different now versus the start of the period.",
        },
        "lessons": {
            "type": "array",
            "maxItems": 4,
            "description": "Durable lessons — things that will still be true next period. "
            "Empty is a correct answer.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim", "evidence", "confidence"],
                "properties": {
                    "claim": {"type": "string"},
                    "evidence": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
            },
        },
        "drop": {
            "type": "array",
            "maxItems": 5,
            "items": {"type": "string"},
            "description": "Paths of memory files that this period's evidence "
            "contradicts or that have gone stale.",
        },
    },
}

ROLLUP_PROMPT = """You are Annie, reviewing your own notebook at the end of a {period}.

You are shown the files you wrote during it. Produce the {period}'s summary.

Write it for yourself to read in three months. That means: what the market was
actually doing, what changed, and what you now believe that you did not before.
Not a list of counts — those are already recorded in the files you are reading.

Be strict about `lessons`. A lesson belongs here only if it would still be
worth acting on next {period}. Something that happened once is an observation,
not a lesson; leave the list empty rather than promoting noise.

In `drop`, name files whose content this period's evidence contradicts, or
that recorded a passing thing that is now clearly over. Only paths you were
actually shown."""


async def write_weekly_summary(
    registry: ProviderRegistry, settings: Settings, *, now: datetime | None = None
) -> dict[str, Any]:
    """Summarise the week from its daily files. One model call."""
    now = now or datetime.now(timezone.utc)
    week_end = now
    week_start = now - timedelta(days=7)
    sources = _files_in_range("daily", week_start, week_end)
    return await _synthesise(
        registry, settings,
        period="week",
        path=service.weekly_path(now - timedelta(days=1)),
        title=f"Week of {week_start.date().isoformat()}",
        sources=sources,
        tags=["weekly"],
        importance=0.7,
        now=now,
    )


async def write_monthly_summary(
    registry: ProviderRegistry, settings: Settings, *, now: datetime | None = None
) -> dict[str, Any]:
    """Summarise the month from its weekly files. One model call, never sees raw data."""
    now = now or datetime.now(timezone.utc)
    month_start = (now.replace(day=1) - timedelta(days=1)).replace(day=1)
    sources = _files_in_range("weekly", month_start, now)
    return await _synthesise(
        registry, settings,
        period="month",
        path=service.monthly_path(now - timedelta(days=1)),
        title=f"{month_start.strftime('%B %Y')}",
        sources=sources,
        tags=["monthly"],
        importance=0.85,
        now=now,
    )


def _files_in_range(section: str, start: datetime, end: datetime) -> list[Any]:
    """Period files, by their ``updated`` stamp. Cheap — local reads only."""
    store = MemoryStore()
    picked = []
    for memory in store.load_all(section):
        stamp = memory.updated or memory.created
        if not stamp:
            continue
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if start <= when <= end:
            picked.append(memory)
    picked.sort(key=lambda m: m.updated or "")
    return picked


async def _synthesise(
    registry: ProviderRegistry,
    settings: Settings,
    *,
    period: str,
    path: str,
    title: str,
    sources: list[Any],
    tags: list[str],
    importance: float,
    now: datetime,
) -> dict[str, Any]:
    if not sources:
        return {"skipped": f"no {period} source files to summarise", "path": path}
    if not settings.is_available("ai"):
        return {"skipped": "ai not configured", "path": path}

    # Bounded input: the last 2,500 characters of each source file. A daily
    # file's tail is its observations; its head is counts already reflected
    # in the ledger. This keeps a month's call the same size as a week's.
    body = "\n\n---\n\n".join(
        f"## {m.path} — {m.title}\n{m.body[-2500:]}" for m in sources[-12:]
    )
    top_signals = signals.meaningful(limit=8)
    if top_signals:
        body += "\n\n---\n\n## Signals currently meaningful\n" + "\n".join(
            f"- {s['name']} [{s['status']}] {s['recent_count']}/{s['recent_total']} "
            f"of ${int(s['tier']):,}+ tokens"
            for s in top_signals
        )

    client = await registry.reasoning.raw_client()
    try:
        response = await client.chat.completions.create(
            model=settings.openai_reasoning_model,
            messages=[
                {"role": "system", "content": ROLLUP_PROMPT.format(period=period)},
                {"role": "user", "content": body},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "rollup", "strict": True, "schema": ROLLUP_SCHEMA},
            },
            max_completion_tokens=2000,
            temperature=0.3,
            reasoning_effort="none",
        )
    except Exception as exc:
        log.error("rollup_call_failed", period=period, exc_info=True)
        return {"error": str(exc)[:300], "path": path}

    try:
        payload = json.loads(response.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        payload = {}

    lessons = payload.get("lessons") or []
    lines = [
        payload.get("summary") or "_No summary produced._",
        "",
        "## What changed",
        "",
        payload.get("what_changed") or "_Nothing notable._",
    ]
    if lessons:
        lines += ["", "## Lessons"]
        for lesson in lessons:
            lines.append(
                f"- **{lesson.get('claim')}** "
                f"({lesson.get('confidence', 'medium')} confidence)\n"
                f"  - Evidence: {lesson.get('evidence')}"
            )
    lines += ["", f"_Synthesised from {len(sources)} file(s): "
              + ", ".join(f"`{m.path}`" for m in sources[-12:]) + "._"]

    await service.write(
        path,
        body="\n".join(lines),
        title=title,
        kind=period,
        tags=tags,
        importance=importance,
        confidence="medium",
        source="rollup",
        evidence=f"{len(sources)} source files",
    )

    # Act on the model's own pruning suggestions, bounded to files it was
    # shown and to sections that are safe to delete from.
    dropped: list[str] = []
    shown = {m.path for m in sources}
    for candidate in (payload.get("drop") or [])[:5]:
        target = str(candidate)
        if target in shown or target.split("/", 1)[0] in {"notes", "narratives", "tokens"}:
            if await service.forget(target):
                dropped.append(target)

    return {
        "path": path,
        "sources": len(sources),
        "lessons": len(lessons),
        "dropped": dropped,
        "input_tokens": getattr(getattr(response, "usage", None), "prompt_tokens", 0) or 0,
        "output_tokens": getattr(getattr(response, "usage", None), "completion_tokens", 0) or 0,
    }


async def prune_old_dailies(*, keep_days: int = DAILY_RETENTION_DAYS) -> list[str]:
    """Delete daily files whose content has been lifted into weeklies.

    Only ever removes ``daily/`` files, and only ones older than the
    retention window — by which point the weekly and monthly above them are
    the record. This is the file-level counterpart to the ledger's prune:
    memory that compacts upward instead of accumulating.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).date().isoformat()
    removed: list[str] = []
    for memory in MemoryStore().load_all("daily"):
        stem = memory.path.rsplit("/", 1)[-1].removesuffix(".md")
        if stem < cutoff and await service.forget(memory.path):
            removed.append(memory.path)
    if removed:
        log.info("dailies_pruned", count=len(removed))
    return removed
