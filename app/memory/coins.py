"""Why a coin moved — the research pass, and the durable record of it.

Everything else in this system answers *what* happened. The ledger holds
peaks and timings, the signals engine holds which characteristics are
over-represented, and both are statistics over names and numbers. Neither can
tell you that a token ran because a video went viral on Tuesday, or because
somebody with three hundred thousand followers posted a screenshot, or
because it was the fourth copy of something that worked last week and the
copy was early enough to matter.

That gap is the whole reason to launch anything. "Cat-adjacent names cleared
tiers at 3x baseline" is a pattern; "this specific cat ran because a clip of
a courtroom sketch got twelve million views the same morning" is a *reason*,
and a reason is the thing you can act on twice.

**What this costs, and why it is worth it.** Roughly ninety tokens clear a
tier on a normal day at real Solana volume. Each gets one or two web searches
and one small bounded model call, which is a real step up from a system that
was spending four calls a day. The operator chose full coverage over a
cheaper sample deliberately, so the economy here is in the details rather
than in the count:

* **Once per mint, ever.** Research is keyed by mint and skipped if present.
  A token that ran on Tuesday is not re-researched on Wednesday, so the
  steady-state cost is genuinely "new qualifiers", not "qualifiers".
* **Ordered by peak, and bounded per cycle.** The biggest movers are
  researched first, so a backlog degrades into "the small ones wait", never
  "the interesting ones were missed".
* **The searches are shaped, not open-ended.** One query for the token
  itself, one for the theme. Undated results are close to useless for a
  question that is entirely about timing.
* **Cheap model settings**: ``reasoning_effort="none"``, a tight token
  ceiling, and a strict schema so nothing comes back as prose to re-parse.

**Where it lands.** Three places, deliberately:

* SQLite (``coin_research``) — the working copy. Free, local, and what the
  bots and the API read.
* Firestore (``coins``) — one document per coin, durable and shared. About
  ninety writes a day against a four-thousand budget, which is the arithmetic
  that makes this reasonable: the coins that clear a tier are a tiny fraction
  of the thirty-odd thousand daily launches, and only they are worth keeping.
* The token's memory file — so a contract address pasted into chat resolves
  to the reasoning, not just the row.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

import structlog

from app.annie import voice
from app.config import Settings
from app.memory import db, ledger
from app.providers.registry import ProviderRegistry

log = structlog.get_logger(__name__)

#: Firestore collection holding one document per researched coin.
COLLECTION = "coins"

#: How many coins to research in a single cycle. Four cycles a day against
#: ~90 qualifiers means ~23 is the steady state; this leaves headroom to
#: work through a backlog after an outage without any one cycle running long.
PER_CYCLE = 40

#: Web results per query. Past a handful the extra results are almost always
#: aggregator pages repeating the first three.
MAX_RESULTS = 5

#: What kind of thing made it move. A closed list because the point is to
#: count them later — "meme" and "a meme" and "memetic" are the same cause
#: and must not become three categories.
CATALYSTS = [
    "viral_video",
    "tweet_or_post",
    "celebrity_or_influencer",
    "news_event",
    "meme_format",
    "tech_or_product",
    "copycat",
    "insider_or_coordinated",
    "unclear",
]

RESEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "why_it_moved",
        "catalyst",
        "catalyst_detail",
        "why_now",
        "category",
        "confidence",
        "repeatable",
    ],
    "properties": {
        "why_it_moved": {
            "type": "string",
            "description": (
                "Two to four sentences on what actually drove this. Prose, in "
                "your voice. If the evidence does not support a reason, say "
                "that plainly — an honest 'nothing I can find explains this, "
                "it looks like a thin-float pump' is far more useful than a "
                "confident story assembled from nothing."
            ),
        },
        "catalyst": {"type": "string", "enum": CATALYSTS},
        "catalyst_detail": {
            "type": "string",
            "description": (
                "The specific thing: name the video, the account, the product, "
                "the coin it copies. Empty string if the catalyst is unclear."
            ),
        },
        "why_now": {
            "type": "string",
            "description": (
                "Why this moment rather than a month ago or a month from now. "
                "Timing is the part that transfers to a launch of our own."
            ),
        },
        "category": {
            "type": "string",
            "description": (
                "Two or three words naming the theme, lowercase, reusable "
                "across coins: 'political meme', 'ai agent', 'animal - dog', "
                "'nostalgia tech'. This is a label you will be grouping by, "
                "so prefer an existing shape over a novel one."
            ),
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "repeatable": {
            "type": "boolean",
            "description": (
                "Could the shape of this be deliberately reproduced by someone "
                "launching a token? A viral format usually can. A one-off news "
                "event usually cannot."
            ),
        },
    },
}

SYSTEM_PROMPT = """You are looking at one Solana token that reached a
meaningful market cap, plus whatever the web turned up about it, and working
out why it moved.

You are not describing the chart. The numbers are already recorded and the
operator can read them. You are answering the question the numbers cannot:
what happened in the world that made people buy this, and why then.

How to weigh what you are given:

- Search results are evidence, not truth. They may be about a different
  thing with the same name — memecoin tickers collide with real brands
  constantly. If the results are about Nike the company and the token is
  called NIKE, say the results are unrelated rather than inventing a link.
- Dates matter more than usual here. A result from three months ago does not
  explain a move that happened this morning. Undated results are weak.
- Absence of evidence is a real finding. Most tokens that touch a tier do so
  on thin liquidity with no story at all. Saying so, with low confidence, is
  correct and useful — it is how the operator learns what fraction of moves
  are noise.
- Never invent a tweet, a view count, an account name, or an event. If you
  did not see it in the results, you do not know it.

`repeatable` is the field the operator will actually act on: it asks whether
someone could deliberately build the same setup. A format that can be
re-used is worth far more than a lucky accident."""


@dataclass(slots=True)
class CoinResearch:
    mint: str
    symbol: str | None = None
    name: str | None = None
    creator: str | None = None
    launchpad: str | None = None
    peak_market_cap: float | None = None
    tier: float | None = None
    qualified_at: str | None = None
    why_it_moved: str = ""
    catalyst: str = "unclear"
    catalyst_detail: str = ""
    why_now: str = ""
    category: str = ""
    confidence: str = "low"
    repeatable: bool = False
    sources: list[str] = field(default_factory=list)
    researched_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# -----------------------------------------------------------------------------
# Storage
# -----------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS coin_research (
    mint          TEXT PRIMARY KEY,
    researched_at TEXT NOT NULL,
    catalyst      TEXT,
    category      TEXT,
    confidence    TEXT,
    repeatable    INTEGER NOT NULL DEFAULT 0,
    payload       TEXT NOT NULL
)
"""


def ensure_schema() -> None:
    db.execute(SCHEMA)
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_coin_research_at "
        "ON coin_research(researched_at DESC)"
    )


def get(mint: str) -> CoinResearch | None:
    ensure_schema()
    row = db.query_one("SELECT payload FROM coin_research WHERE mint = ?", (mint,))
    if row is None:
        return None
    try:
        return CoinResearch(**json.loads(row["payload"]))
    except (json.JSONDecodeError, TypeError):
        return None


def has_research(mint: str) -> bool:
    ensure_schema()
    return db.query_one("SELECT 1 FROM coin_research WHERE mint = ?", (mint,)) is not None


def save(record: CoinResearch) -> None:
    ensure_schema()
    db.execute(
        "INSERT INTO coin_research(mint, researched_at, catalyst, category, confidence, "
        "repeatable, payload) VALUES(?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(mint) DO UPDATE SET researched_at = excluded.researched_at, "
        "catalyst = excluded.catalyst, category = excluded.category, "
        "confidence = excluded.confidence, repeatable = excluded.repeatable, "
        "payload = excluded.payload",
        (
            record.mint,
            record.researched_at,
            record.catalyst,
            record.category,
            record.confidence,
            int(record.repeatable),
            json.dumps(record.to_dict()),
        ),
    )


def recent(limit: int = 50, *, category: str | None = None) -> list[CoinResearch]:
    ensure_schema()
    if category:
        rows = db.query(
            "SELECT payload FROM coin_research WHERE category = ? "
            "ORDER BY researched_at DESC LIMIT ?",
            (category, limit),
        )
    else:
        rows = db.query(
            "SELECT payload FROM coin_research ORDER BY researched_at DESC LIMIT ?",
            (limit,),
        )
    out: list[CoinResearch] = []
    for row in rows:
        try:
            out.append(CoinResearch(**json.loads(row["payload"])))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def categories(*, since_hours: int = 168) -> list[dict[str, Any]]:
    """What themes the researched coins fall into, most common first.

    This is the categorisation that was missing. The signals engine derives
    themes from a seeded vocabulary matched against names, which cannot see a
    theme nobody thought to seed; these labels come from reading what the
    coin actually was.
    """
    ensure_schema()
    rows = db.query(
        """
        SELECT category, COUNT(*) AS n,
               SUM(repeatable) AS repeatable_n
          FROM coin_research
         WHERE category != '' AND researched_at >= datetime('now', ?)
         GROUP BY category ORDER BY n DESC
        """,
        (f"-{int(since_hours)} hours",),
    )
    return [
        {
            "category": r["category"],
            "coins": int(r["n"]),
            "repeatable": int(r["repeatable_n"] or 0),
        }
        for r in rows
    ]


# -----------------------------------------------------------------------------
# The research pass
# -----------------------------------------------------------------------------


def pending(limit: int = PER_CYCLE) -> list[Any]:
    """Qualified tokens with no research yet, biggest peak first.

    Ordering by peak is what makes a backlog degrade safely: after an outage
    the queue drains from the top, so the coins that actually mattered are
    researched first and the ones that brushed the floor wait.
    """
    ensure_schema()
    rows = db.query(
        """
        SELECT s.* FROM sightings s
         LEFT JOIN coin_research r ON r.mint = s.mint
         WHERE s.qualified_at IS NOT NULL AND r.mint IS NULL
         ORDER BY COALESCE(s.peak_market_cap, 0) DESC
         LIMIT ?
        """,
        (limit,),
    )
    return [ledger.Sighting.from_row(r) for r in rows]


async def _gather_evidence(
    registry: ProviderRegistry, sighting: Any
) -> tuple[str, list[str]]:
    """Search the web for what this token is and why anyone cared."""
    label = sighting.name or sighting.symbol
    if not label:
        return "", []

    queries = [f"solana memecoin {label} ${sighting.symbol or ''}".strip()]
    if sighting.name and sighting.name != sighting.symbol:
        queries.append(f'"{sighting.name}" viral meme trending')

    blocks: list[str] = []
    sources: list[str] = []
    for query in queries:
        try:
            results = await registry.web_research.search(
                query, max_results=MAX_RESULTS, recency_days=14
            )
        except Exception:
            log.info("coin_research_search_failed", mint=sighting.mint, exc_info=True)
            continue
        for item in results:
            url = getattr(item, "url", "") or ""
            title = getattr(item, "title", "") or ""
            snippet = (getattr(item, "snippet", "") or "")[:400]
            published = getattr(item, "published_at", None) or "undated"
            blocks.append(f"- [{published}] {title}\n  {url}\n  {snippet}")
            if url:
                sources.append(url)

    return "\n".join(blocks[: MAX_RESULTS * 2]), sources[: MAX_RESULTS * 2]


def _brief(sighting: Any, evidence: str) -> str:
    from app.memory.rollup import _usd

    lines = [
        f"Token: {sighting.name or '(unnamed)'} ({sighting.symbol or '?'})",
        f"Contract: {sighting.mint}",
        f"Creator: {sighting.creator or 'unknown'}",
        f"Launchpad: {sighting.launchpad or 'unknown'}",
        f"First seen: {sighting.first_seen}",
        f"Qualified at: {sighting.qualified_at}",
        f"Peak market cap: {_usd(sighting.peak_market_cap)}",
        f"Current market cap: {_usd(sighting.market_cap)}",
    ]
    if sighting.peak_market_cap and sighting.market_cap:
        drop = 1 - (sighting.market_cap / sighting.peak_market_cap)
        if drop > 0.5:
            lines.append(f"Round-tripped: {drop * 100:.0f}% off its peak.")

    lines.append("")
    if evidence:
        lines += ["What the web says:", evidence]
    else:
        lines.append(
            "The web turned up nothing about this token. That is itself "
            "informative — say so rather than reaching."
        )
    return "\n".join(lines)


async def research_one(
    registry: ProviderRegistry, settings: Settings, sighting: Any
) -> CoinResearch | None:
    """Search, judge, and record why one coin moved."""
    evidence, sources = await _gather_evidence(registry, sighting)

    client = await registry.reasoning.raw_client()
    try:
        response = await client.chat.completions.create(
            model=settings.openai_reasoning_model,
            messages=[
                {"role": "system", "content": voice.prefix(SYSTEM_PROMPT)},
                {"role": "user", "content": _brief(sighting, evidence)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "coin_research",
                    "strict": True,
                    "schema": RESEARCH_SCHEMA,
                },
            },
            max_completion_tokens=700,
            temperature=0.3,
            reasoning_effort="none",
        )
        payload = json.loads(response.choices[0].message.content or "{}")
    except Exception:
        log.warning("coin_research_call_failed", mint=sighting.mint, exc_info=True)
        return None

    record = CoinResearch(
        mint=sighting.mint,
        symbol=sighting.symbol,
        name=sighting.name,
        creator=sighting.creator,
        launchpad=sighting.launchpad,
        peak_market_cap=sighting.peak_market_cap,
        tier=sighting.tier,
        qualified_at=sighting.qualified_at,
        why_it_moved=str(payload.get("why_it_moved") or "").strip(),
        catalyst=str(payload.get("catalyst") or "unclear"),
        catalyst_detail=str(payload.get("catalyst_detail") or "").strip(),
        why_now=str(payload.get("why_now") or "").strip(),
        category=str(payload.get("category") or "").strip().lower(),
        confidence=str(payload.get("confidence") or "low"),
        repeatable=bool(payload.get("repeatable")),
        sources=sources,
        researched_at=db.utcnow_iso(),
    )
    save(record)
    return record


async def run_research(
    registry: ProviderRegistry, settings: Settings, *, limit: int = PER_CYCLE
) -> dict[str, Any]:
    """Research every qualifier that has not been looked at yet.

    Bounded per call and idempotent per mint, so running it more often costs
    nothing extra once the queue is drained — the steady-state cost is the
    number of *new* qualifiers, not the number of qualifiers.
    """
    if not settings.is_available("ai"):
        return {"skipped": "ai not configured"}

    queue = pending(limit)
    if not queue:
        return {"researched": 0, "remaining": 0}

    researched: list[str] = []
    failed = 0
    for sighting in queue:
        record = await research_one(registry, settings, sighting)
        if record is None:
            failed += 1
            continue
        researched.append(sighting.mint)
        try:
            await mirror(record)
        except Exception:
            log.info("coin_mirror_failed", mint=sighting.mint, exc_info=True)
        try:
            from app.memory.rollup import write_token_memory

            await write_token_memory(sighting.mint)
        except Exception:
            log.info("coin_memory_failed", mint=sighting.mint, exc_info=True)

    remaining = len(pending(1000))
    log.info(
        "coin_research_complete",
        researched=len(researched),
        failed=failed,
        remaining=remaining,
    )
    return {
        "researched": len(researched),
        "failed": failed,
        "remaining": remaining,
        "mints": researched[:10],
    }


# -----------------------------------------------------------------------------
# Firestore mirror
# -----------------------------------------------------------------------------


async def mirror(record: CoinResearch) -> bool:
    """One document per coin, in Firestore.

    Affordable precisely because the tier filter is doing its job: about
    ninety coins a day clear one, out of thirty-odd thousand launches. Ninety
    writes against a four-thousand-a-day budget is the arithmetic that makes
    "keep everything about the ones that mattered" a reasonable position.
    """
    from app.db import budget
    from app.memory import snapshot

    if not snapshot.is_configured():
        return False

    async def write() -> bool:
        from app.db.firestore import get_client

        await get_client().collection(COLLECTION).document(record.mint).set(
            record.to_dict()
        )
        return True

    return bool(await budget.guarded(f"coin:{record.mint}", 1, write))
