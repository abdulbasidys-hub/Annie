"""Our own launches — the only tokens tracked because we said so.

Everything else in this system earns attention by clearing a bar. Thirty-odd
thousand tokens launch a day, ninety cross a tier, and the rest are forgotten
inside forty-eight hours. That filter is the whole design, and it is exactly
wrong for a coin we launched ourselves: ours matters at $4,000 market cap and
it matters at zero, because what we need from it is not "was this notable"
but "what did we get right, and what did we get wrong".

So a launch is registered by hand and then exempt. It never expires from the
ledger, it is re-priced on every watch pass regardless of what it is worth,
and it accumulates a written record instead of a single verdict.

**What gets recorded, and why that shape.** A launch carries the idea it came
from where there was one, so the loop closes: Annie proposed a name, a
ticker, an image and a first post, we launched it, and the file underneath
holds both her reasoning and what actually happened. That is the only way a
playbook entry can ever be honest — "cat-adjacent worked" written from
observation of other people's coins is a hypothesis, and written from four of
our own with their outcomes attached it is evidence.

Each cycle appends an observation: where it is now against its peak, and
whether anyone outside is talking about it. The second half costs a web
search per launch per cycle, which is affordable only because we launch a
handful of things rather than ninety a day — the same arithmetic that makes
the tier filter necessary makes this exemption cheap.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

from app.annie import voice
from app.config import Settings
from app.memory import db, ledger
from app.providers.registry import ProviderRegistry

log = structlog.get_logger(__name__)

#: Firestore collection holding one document per launch of ours.
COLLECTION = "our_launches"

#: Web results per check. Small: the question is "is anyone talking about
#: this", which the first few results answer or nobody does.
MAX_RESULTS = 5

STATUSES = ["live", "graduated", "dead", "abandoned"]

CHECKIN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["observation", "traction", "what_is_working", "what_to_change"],
    "properties": {
        "observation": {
            "type": "string",
            "description": (
                "Two to four sentences on where this launch stands. Prose, in "
                "your voice, written to be read weeks later by someone deciding "
                "what to do differently. Include the numbers only where they "
                "carry the point."
            ),
        },
        "traction": {
            "type": "string",
            "enum": ["none", "early", "building", "peaked", "fading", "dead"],
        },
        "what_is_working": {
            "type": "string",
            "description": (
                "The part worth repeating on the next launch. Empty string if "
                "nothing is — do not manufacture a lesson from a flat chart."
            ),
        },
        "what_to_change": {
            "type": "string",
            "description": (
                "The specific thing you would do differently, stated so it can "
                "be acted on next time: the name, the timing, the art, the "
                "first post, the theme. 'Market it better' is not an answer."
            ),
        },
    },
}

SYSTEM_PROMPT = """You are reviewing a token the operator launched themselves,
not one you found in the market.

This is a post-mortem in progress, and its value is entirely in being honest
about a thing the operator has money and ego in. Be straight with them:

- Do not soften a failure. A launch that went nowhere went nowhere, and
  saying so is what makes the record worth keeping. They can read the chart
  themselves; what they cannot get anywhere else is a candid read.
- Do not credit a rise to the plan when it might be the market. If everything
  in the theme is running, say the theme is running.
- Where there was a prediction — a reason you proposed this idea — check it
  against what happened and say plainly whether it held. That comparison is
  the single most useful thing in this file.
- If nothing has changed since the last check, say so briefly rather than
  restating the situation in new words. A file of near-identical paragraphs
  is worse than a short one."""


@dataclass(slots=True)
class Launch:
    mint: str
    name: str = ""
    ticker: str = ""
    launched_at: str = ""
    status: str = "live"
    note: str = ""
    #: The idea set this came from, when it came from one.
    idea_id: int | None = None
    idea_snapshot: dict[str, Any] = field(default_factory=dict)
    peak_market_cap: float | None = None
    last_market_cap: float | None = None
    last_checked_at: str | None = None
    checkins: int = 0
    memory_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SCHEMA = """
CREATE TABLE IF NOT EXISTS our_launches (
    mint        TEXT PRIMARY KEY,
    launched_at TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'live',
    payload     TEXT NOT NULL
)
"""


def ensure_schema() -> None:
    db.execute(SCHEMA)


def _row_to_launch(row: Any) -> Launch | None:
    try:
        return Launch(**json.loads(row["payload"]))
    except (json.JSONDecodeError, TypeError):
        return None


def get(mint: str) -> Launch | None:
    ensure_schema()
    row = db.query_one("SELECT payload FROM our_launches WHERE mint = ?", (mint,))
    return _row_to_launch(row) if row else None


def is_ours(mint: str) -> bool:
    ensure_schema()
    return db.query_one("SELECT 1 FROM our_launches WHERE mint = ?", (mint,)) is not None


def all_mints() -> list[str]:
    """Every launch of ours, for the prune and watch exemptions."""
    ensure_schema()
    return [r["mint"] for r in db.query("SELECT mint FROM our_launches", ())]


def listing(*, status: str | None = None, limit: int = 100) -> list[Launch]:
    ensure_schema()
    if status:
        rows = db.query(
            "SELECT payload FROM our_launches WHERE status = ? "
            "ORDER BY launched_at DESC LIMIT ?",
            (status, limit),
        )
    else:
        rows = db.query(
            "SELECT payload FROM our_launches ORDER BY launched_at DESC LIMIT ?", (limit,)
        )
    return [x for x in (_row_to_launch(r) for r in rows) if x]


def save(launch: Launch) -> None:
    ensure_schema()
    db.execute(
        "INSERT INTO our_launches(mint, launched_at, status, payload) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(mint) DO UPDATE SET launched_at = excluded.launched_at, "
        "status = excluded.status, payload = excluded.payload",
        (launch.mint, launch.launched_at, launch.status, json.dumps(launch.to_dict())),
    )


async def register(
    mint: str,
    *,
    name: str = "",
    ticker: str = "",
    note: str = "",
    idea_id: int | None = None,
    now: datetime | None = None,
) -> Launch:
    """Mark a mint as ours. Idempotent — re-registering updates, never duplicates.

    The token is put straight into the ledger if it is not already there. A
    launch registered minutes after the mint usually *is* already there via
    the webhook, but registering one from another wallet, or after an outage,
    must work the same way.
    """
    mint = mint.strip()
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")

    existing = get(mint)
    sighting = ledger.get_sighting(mint)
    if sighting is None:
        ledger.record_launch(mint=mint, creator=None, launchpad="ours", name=name or None,
                             symbol=ticker or None)
        sighting = ledger.get_sighting(mint)

    idea_snapshot: dict[str, Any] = existing.idea_snapshot if existing else {}
    if idea_id is not None and not idea_snapshot:
        idea_snapshot = _idea_snapshot(idea_id, ticker=ticker, name=name)

    launch = Launch(
        mint=mint,
        name=name or (sighting.name if sighting else "") or (existing.name if existing else ""),
        ticker=ticker or (sighting.symbol if sighting else "") or (existing.ticker if existing else ""),
        launched_at=existing.launched_at if existing else stamp,
        status=existing.status if existing else "live",
        note=note or (existing.note if existing else ""),
        idea_id=idea_id if idea_id is not None else (existing.idea_id if existing else None),
        idea_snapshot=idea_snapshot,
        peak_market_cap=sighting.peak_market_cap if sighting else None,
        last_market_cap=sighting.market_cap if sighting else None,
        checkins=existing.checkins if existing else 0,
        memory_path=existing.memory_path if existing else launch_path(mint),
    )
    save(launch)
    await _write_memory(launch, opening=existing is None)
    await mirror(launch)
    log.info("launch_registered", mint=mint, ticker=launch.ticker, new=existing is None)
    return launch


def _idea_snapshot(idea_id: int, *, ticker: str, name: str) -> dict[str, Any]:
    """Freeze the idea this launch came from, so the loop can close later.

    Copied rather than referenced: the idea set is a record of what was
    proposed at a moment, and a launch reviewed in six weeks needs the
    proposal as it stood, not as it may have been edited since.
    """
    from app.memory import ideas

    for entry in ideas.history(limit=50):
        if entry.get("id") != idea_id:
            continue
        for idea in entry.get("ideas") or []:
            if ticker and str(idea.get("ticker", "")).upper() == ticker.upper():
                return idea
            if name and str(idea.get("name", "")).lower() == name.lower():
                return idea
        return (entry.get("ideas") or [{}])[0]
    return {}


def launch_path(mint: str) -> str:
    return f"launches/{mint}.md"


def set_status(mint: str, status: str) -> Launch | None:
    launch = get(mint)
    if launch is None:
        return None
    launch.status = status if status in STATUSES else launch.status
    save(launch)
    return launch


# -----------------------------------------------------------------------------
# The per-cycle review
# -----------------------------------------------------------------------------


async def _outside_chatter(
    registry: ProviderRegistry, launch: Launch
) -> tuple[str, list[str]]:
    label = launch.name or launch.ticker
    if not label:
        return "", []
    try:
        results = await registry.web_research.search(
            f"{label} ${launch.ticker} solana token", max_results=MAX_RESULTS, recency_days=7
        )
    except Exception:
        log.info("launch_search_failed", mint=launch.mint, exc_info=True)
        return "", []

    blocks, sources = [], []
    for item in results:
        url = getattr(item, "url", "") or ""
        title = getattr(item, "title", "") or ""
        snippet = (getattr(item, "snippet", "") or "")[:300]
        published = getattr(item, "published_at", None) or "undated"
        blocks.append(f"- [{published}] {title}\n  {url}\n  {snippet}")
        if url:
            sources.append(url)
    return "\n".join(blocks), sources


def _review_brief(launch: Launch, sighting: Any, chatter: str) -> str:
    from app.memory.rollup import _usd

    lines = [
        f"Our launch: {launch.name or launch.ticker or launch.mint[:8]} (${launch.ticker})",
        f"Contract: {launch.mint}",
        f"Launched: {launch.launched_at}",
        f"Check-in number: {launch.checkins + 1}",
    ]
    if sighting:
        lines += [
            f"Market cap now: {_usd(sighting.market_cap)}",
            f"Peak: {_usd(sighting.peak_market_cap)}",
        ]
        if sighting.peak_market_cap and sighting.market_cap:
            drop = 1 - (sighting.market_cap / sighting.peak_market_cap)
            if drop > 0.3:
                lines.append(f"Down {drop * 100:.0f}% from peak.")
    else:
        lines.append("No market data yet — no tradeable pair has appeared.")

    if launch.idea_snapshot:
        idea = launch.idea_snapshot
        lines += [
            "",
            "This came from one of your own ideas. What you predicted at the time:",
            f"- Angle: {idea.get('angle') or idea.get('description') or '?'}",
            f"- Why now: {idea.get('why_now') or '?'}",
            f"- Risk you flagged: {idea.get('risk') or '?'}",
            f"- Grounding: {idea.get('grounding') or '?'}",
        ]

    if launch.note:
        lines += ["", f"Operator's note: {launch.note}"]

    lines += ["", "Is anyone outside talking about it?"]
    lines.append(chatter or "Nothing found in the last week. That is a finding, not a gap.")
    return "\n".join(lines)


async def review_one(
    registry: ProviderRegistry, settings: Settings, launch: Launch
) -> dict[str, Any] | None:
    """One check-in on one launch: price, chatter, and a written observation."""
    sighting = ledger.get_sighting(launch.mint)
    chatter, sources = await _outside_chatter(registry, launch)

    client = await registry.reasoning.raw_client()
    try:
        response = await client.chat.completions.create(
            model=settings.openai_reasoning_model,
            messages=[
                {"role": "system", "content": voice.prefix(SYSTEM_PROMPT)},
                {"role": "user", "content": _review_brief(launch, sighting, chatter)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "launch_checkin",
                    "strict": True,
                    "schema": CHECKIN_SCHEMA,
                },
            },
            max_completion_tokens=700,
            temperature=0.3,
            reasoning_effort="none",
        )
        payload = json.loads(response.choices[0].message.content or "{}")
    except Exception:
        log.warning("launch_review_failed", mint=launch.mint, exc_info=True)
        return None

    launch.checkins += 1
    launch.last_checked_at = db.utcnow_iso()
    if sighting:
        launch.last_market_cap = sighting.market_cap
        launch.peak_market_cap = sighting.peak_market_cap
    save(launch)

    await _append_checkin(launch, payload, sources)
    await mirror(launch)
    return {"mint": launch.mint, "traction": payload.get("traction"), **payload}


async def run_reviews(
    registry: ProviderRegistry, settings: Settings, *, limit: int = 10
) -> dict[str, Any]:
    """Review every live launch. Cheap because there are never many."""
    if not settings.is_available("ai"):
        return {"skipped": "ai not configured"}

    live = [x for x in listing(limit=limit) if x.status == "live"]
    if not live:
        return {"reviewed": 0}

    reviewed = 0
    for launch in live:
        if await review_one(registry, settings, launch) is not None:
            reviewed += 1
    log.info("launch_reviews_complete", reviewed=reviewed, live=len(live))
    return {"reviewed": reviewed, "live": len(live)}


# -----------------------------------------------------------------------------
# The written record
# -----------------------------------------------------------------------------


async def _write_memory(launch: Launch, *, opening: bool) -> None:
    from app.memory import service

    if not opening and service.exists(launch_path(launch.mint)):
        return

    lines = [
        f"**{launch.name or launch.ticker or launch.mint[:8]}** — our launch",
        "",
        f"- CA: `{launch.mint}`",
        f"- Ticker: ${launch.ticker or '?'}",
        f"- Launched: {launch.launched_at}",
        f"- Status: {launch.status}",
    ]
    if launch.note:
        lines += ["", f"Operator's note: {launch.note}"]

    if launch.idea_snapshot:
        idea = launch.idea_snapshot
        lines += [
            "",
            "## The idea it came from",
            "",
            f"- Angle: {idea.get('angle') or idea.get('description') or '?'}",
            f"- Why now: {idea.get('why_now') or '?'}",
            f"- Evidence at the time: {idea.get('evidence') or '?'}",
            f"- Grounding: {idea.get('grounding') or '?'}",
            f"- Risk flagged: {idea.get('risk') or '?'}",
        ]
        if idea.get("first_tweet"):
            lines += ["", f"- Launch post used: {idea['first_tweet']}"]

    lines += ["", "## Check-ins", "", "_None yet._"]

    await service.write(
        launch_path(launch.mint),
        body="\n".join(lines),
        title=f"Our launch — {launch.ticker or launch.mint[:8]}",
        kind="launch",
        tags=["launch", "ours"],
        keys=[launch.mint] + ([launch.ticker.lower()] if launch.ticker else []),
        importance=0.95,
        confidence="high",
        source="operator",
    )


async def _append_checkin(
    launch: Launch, payload: dict[str, Any], sources: list[str]
) -> None:
    from app.memory import service

    path = launch_path(launch.mint)
    memory = service.read(path)
    body = memory.body if memory else ""
    body = body.replace("\n_None yet._", "")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    block = [
        "",
        f"### {stamp} — check-in {launch.checkins}",
        "",
        str(payload.get("observation") or "").strip(),
        "",
        f"- Traction: {payload.get('traction')}",
    ]
    if payload.get("what_is_working"):
        block.append(f"- Working: {payload['what_is_working']}")
    if payload.get("what_to_change"):
        block.append(f"- Change next time: {payload['what_to_change']}")
    if sources:
        block += [f"- Seen at: {u}" for u in sources[:3]]

    await service.write(
        path,
        body=body.rstrip() + "\n" + "\n".join(block),
        title=f"Our launch — {launch.ticker or launch.mint[:8]}",
        kind="launch",
        tags=["launch", "ours", str(payload.get("traction") or "")],
        keys=[launch.mint] + ([launch.ticker.lower()] if launch.ticker else []),
        importance=0.95,
        confidence="high",
        source="annie",
    )


async def mirror(launch: Launch) -> bool:
    from app.db import budget
    from app.memory import snapshot

    if not snapshot.is_configured():
        return False

    async def write() -> bool:
        from app.db.firestore import get_client

        await get_client().collection(COLLECTION).document(launch.mint).set(
            launch.to_dict()
        )
        return True

    return bool(await budget.guarded(f"launch:{launch.mint}", 1, write))
