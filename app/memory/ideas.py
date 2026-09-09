"""Generating a token idea from what Annie has actually learned.

This is the payoff the rest of the system exists for: "if we need an idea to
launch a token, she'll look at what these high-moving tokens are and give us
something good". It is deliberately not a creative-writing prompt with a
market-flavoured preamble. An idea produced here has to be traceable to
evidence in memory, so the call is built from three grounded inputs and
nothing else:

1. **What is winning right now** — the current movers and the statistically
   over-represented characteristics among tokens that cleared a tier, from
   the ledger and the signals table. Free.
2. **What has worked before** — retrieved from ``playbook/`` and ``core/``,
   which is where lessons that survived more than one week end up. Retrieved
   by relevance, not loaded wholesale.
3. **What is already crowded** — the same signals read the other way. A
   theme at 40% of winners is not an opportunity, it is a queue.

Two entry points. On demand — from chat, the bots, or the Ideas page — and
once a day alongside the brief, grounded in what moved over the preceding
24 hours. The daily set exists because "what should I launch" is a question
worth answering before you think to ask it; everything else here still costs
nothing until requested.

The daily set is skipped outright when nothing moved. Three speculative
ideas generated from an empty ledger would be worse than none, because they
would arrive looking exactly like the grounded ones.

The output names its evidence per idea. An idea that cannot point at
something in memory is required to say so and be labelled speculative —
which is the honest state for a genuinely novel angle, and much more useful
than one dressed up as a finding.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import structlog

from app.annie import voice
from app.config import Settings
from app.memory import index, ledger, service, signals
from app.providers.registry import ProviderRegistry

log = structlog.get_logger(__name__)

IDEA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["read_of_the_market", "ideas", "avoid"],
    "properties": {
        "read_of_the_market": {
            "type": "string",
            "description": "Two or three sentences on what is actually working right now.",
        },
        "ideas": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "name", "ticker", "description", "image", "angle",
                    "why_now", "evidence", "grounding", "risk",
                ],
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The token name exactly as it should appear on the "
                        "launchpad. Not a description of a name — the name itself.",
                    },
                    "ticker": {
                        "type": "string",
                        "description": "Uppercase, 3-8 characters, no $ prefix.",
                    },
                    "description": {
                        "type": "string",
                        "description": "The launchpad description field, written to be pasted "
                        "in as-is. One or two lines in the register the token is aiming at — "
                        "not an explanation of the strategy, the actual copy.",
                    },
                    "image": {
                        "type": "string",
                        "description": "What the image should show, concretely enough to hand "
                        "to an artist or an image model. Subject, style, and what it must not "
                        "look like.",
                    },
                    "angle": {"type": "string", "description": "The concept, in one or two sentences."},
                    "why_now": {"type": "string", "description": "What in the current market makes this timely."},
                    "evidence": {
                        "type": "string",
                        "description": "The specific signal, token or memory this rests on. "
                        "Say plainly if there is none.",
                    },
                    "grounding": {
                        "type": "string",
                        "enum": ["observed", "inferred", "speculative"],
                        "description": "observed = directly supported by the data shown; "
                        "inferred = a reasonable step from it; speculative = a hunch.",
                    },
                    "risk": {"type": "string", "description": "The strongest reason this fails."},
                },
            },
        },
        "avoid": {
            "type": "array",
            "maxItems": 5,
            "items": {"type": "string"},
            "description": "Themes that are saturated or clearly rolling over right now.",
        },
    },
}

SYSTEM_PROMPT = """You are Annie. You have been watching the Solana memecoin market
continuously and you are being asked for launch ideas.

You are shown what is winning right now, what your notebook says has worked
before, and which themes are crowded. Ground every idea in that material.

Rules:
- Each idea must name its evidence. If an idea is a hunch with nothing behind
  it, mark grounding "speculative" and say so in the evidence field. Do not
  dress a guess up as a finding.
- Do not propose a theme listed as saturated. Being late to a crowded
  narrative is the most common way a launch fails, and the data in front of
  you already shows which those are.
- Prefer an angle adjacent to what is working over a copy of it. If cat
  themes are running, the interesting idea is usually the specific,
  unclaimed corner of that space, not another generic cat.
- Tickers should look like what actually wins in the data you are shown —
  match the observed shape, not a house style.
- Be concrete. "Animal theme with a twist" is not an idea. Give a name, a
  ticker, the description copy and the image, all ready to use — someone
  should be able to open a launchpad and fill the form from your answer
  without writing anything themselves.
- The description field is the copy that goes on the token, not a note about
  the copy. Write it in the register the token is aiming at."""


async def generate(
    registry: ProviderRegistry,
    settings: Settings,
    *,
    brief: str = "",
    count: int = 3,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Produce launch ideas grounded in current memory and current market state.

    ``brief`` is an optional steer from the operator ("something in the AI
    space", "we want a low-effort quick launch"). It is passed through as
    context, never as an instruction to override the evidence — an idea that
    matches the brief but contradicts the data is still marked speculative.
    """
    now = now or datetime.now(timezone.utc)
    if not settings.is_available("ai"):
        return {"error": "OpenAI is not configured in this deployment (OPENAI_API_KEY)."}

    movers = ledger.movers(since_hours=72, limit=20)
    winning = signals.meaningful(limit=12)
    crowded = [s for s in winning if (s.get("recent_freq") or 0) >= 0.30]
    rolling_over = signals.listing(status="declining", limit=5)
    words = signals.emerging_words(movers, min_count=2, limit=10)

    # Retrieve rather than load: the playbook and core memory can be long
    # after a few months, and stuffing all of it in is the exact failure
    # this system was rebuilt to avoid.
    topics = [s["name"] for s in winning[:5]] + [w["phrase"] for w in words[:4]]
    if brief:
        topics.append(brief)
    lessons = index.recall(topics=topics, budget=8)
    playbook = [h for h in index.search("what worked launch pattern", limit=4, section="playbook")]

    context = _render_context(
        now=now, brief=brief, movers=movers, winning=winning, crowded=crowded,
        rolling_over=rolling_over, words=words, lessons=lessons, playbook=playbook,
    )

    client = await registry.reasoning.raw_client()
    try:
        response = await client.chat.completions.create(
            model=settings.openai_reasoning_model,
            messages=[
                {"role": "system", "content": await voice.prefix(SYSTEM_PROMPT)},
                {
                    "role": "user",
                    "content": f"{context}\n\nGive me {max(1, min(count, 4))} idea(s).",
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "launch_ideas", "strict": True, "schema": IDEA_SCHEMA},
            },
            max_completion_tokens=1800,
            temperature=0.8,  # higher than the learning call — this one is meant to reach
            reasoning_effort="none",
        )
    except Exception as exc:
        log.error("idea_generation_failed", exc_info=True)
        return {"error": f"Idea generation failed: {exc}"[:300]}

    try:
        payload = json.loads(response.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        return {"error": "The model returned something unparseable."}

    usage = getattr(response, "usage", None)
    payload["generated_at"] = now.isoformat(timespec="seconds")
    payload["grounded_in"] = {
        "movers": len(movers),
        "signals": len(winning),
        "memories": [h.path for h in lessons + playbook],
    }
    payload["input_tokens"] = getattr(usage, "prompt_tokens", 0) or 0
    payload["output_tokens"] = getattr(usage, "completion_tokens", 0) or 0

    log.info(
        "ideas_generated",
        count=len(payload.get("ideas") or []),
        input_tokens=payload["input_tokens"],
    )
    return payload


def _render_context(
    *,
    now: datetime,
    brief: str,
    movers: list[Any],
    winning: list[dict[str, Any]],
    crowded: list[dict[str, Any]],
    rolling_over: list[dict[str, Any]],
    words: list[dict[str, Any]],
    lessons: list[index.Hit],
    playbook: list[index.Hit],
) -> str:
    def usd(value: float | None) -> str:
        if not value:
            return "?"
        if value >= 1_000_000:
            return f"${value / 1_000_000:.2f}M"
        return f"${value / 1_000:.0f}k"

    lines = [f"# Market as of {now.isoformat(timespec='minutes')}"]
    if brief:
        lines += ["", f"**Operator's steer:** {brief}"]

    if movers:
        lines += ["", "## What has moved in the last 72h"]
        for m in movers[:15]:
            lines.append(
                f"- {m.symbol or m.name or m.mint[:8]} — peak {usd(m.peak_market_cap)}, "
                f"{m.launchpad or 'unknown pad'}"
            )

    if winning:
        lines += ["", "## Characteristics over-represented among tokens that cleared a tier"]
        for s in winning:
            lift = f"{s['lift']:.1f}x baseline" if s.get("lift") else "no baseline yet"
            lines.append(
                f"- {s['name']}: {s['recent_count']}/{s['recent_total']} of "
                f"${int(s['tier']):,}+ tokens ({lift}) [{s['status']}]"
            )

    saturated = [s["name"] for s in crowded] + [s["name"] for s in rolling_over]
    if saturated:
        lines += ["", "## Saturated or rolling over — do not propose these"]
        lines += [f"- {name}" for name in dict.fromkeys(saturated)]

    if words:
        lines += ["", "## Vocabulary appearing in winners that is not in the seed list"]
        lines.append(", ".join(f"{w['phrase']} ({w['count']})" for w in words))

    if playbook or lessons:
        lines += ["", "## From your notebook"]
        for hit in playbook + lessons:
            lines.append(f"- `{hit.path}` — {hit.title}: {hit.snippet.strip()[:320]}")
    else:
        lines += [
            "",
            "## From your notebook",
            "_Nothing relevant recorded yet — this deployment has little history. "
            "Say so, and mark ideas accordingly rather than implying support you "
            "do not have._",
        ]

    return "\n".join(lines)


def _render_markdown(payload: dict[str, Any], *, note: str = "") -> str:
    """The prose form Annie reads back on later cycles.

    Stored alongside the structured record rather than instead of it: the
    markdown is what she retrieves months later when deciding whether an
    angle has been tried, and the JSON is what the Ideas page renders as
    cards. Reconstructing one from the other would be lossy in both
    directions.
    """
    lines = [payload.get("read_of_the_market") or "", ""]

    for idea in payload.get("ideas") or []:
        lines += [
            f"### {idea.get('name')} (`{idea.get('ticker')}`)",
            "",
            f"- **Description:** {idea.get('description') or '—'}",
            f"- **Image:** {idea.get('image') or '—'}",
            f"- **Angle:** {idea.get('angle')}",
            f"- **Why now:** {idea.get('why_now')}",
            f"- **Evidence:** {idea.get('evidence')} [{idea.get('grounding')}]",
            f"- **Risk:** {idea.get('risk')}",
            "",
        ]

    if payload.get("avoid"):
        lines += ["**Avoided as saturated:** " + ", ".join(payload["avoid"]), ""]
    if note:
        lines += [f"**Note:** {note}", ""]

    return "\n".join(lines).strip()


async def record(
    payload: dict[str, Any],
    *,
    origin: str = "requested",
    brief: str = "",
    now: datetime | None = None,
) -> str | None:
    """Persist one idea set — structured for the page, prose for the notebook.

    ``origin`` separates the daily set from one the operator asked for, so
    the Ideas page can show "today's" without a request being mistaken for
    it.
    """
    from app.memory import db

    ideas = payload.get("ideas") or []
    if not ideas:
        return None

    stamp = now or datetime.now(timezone.utc)
    path = f"playbook/ideas-{stamp.date().isoformat()}.md"

    await service.append(
        path,
        _render_markdown(payload, note=brief),
        heading=f"{stamp.strftime('%H:%M UTC')}"
        + (" — daily set" if origin == "daily" else " — requested"),
        title=f"Launch ideas — {stamp.date().isoformat()}",
        tags=["playbook", "ideas"],
        importance=0.6,
    )

    db.execute(
        "INSERT INTO ideas (generated_at, day, origin, brief, payload, memory_path) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            stamp.isoformat(),
            stamp.date().isoformat(),
            origin,
            brief or None,
            json.dumps(payload),
            path,
        ),
    )
    return path


def latest(origin: str | None = None) -> dict[str, Any] | None:
    """The most recent idea set, optionally restricted to one origin.

    The ``id DESC`` tiebreak is load-bearing, not decoration. The clock has
    finite resolution — coarse on Windows — so two sets recorded inside the
    same cycle (the daily three, then an operator asking for something else a
    moment later) can carry an identical ``generated_at``, and SQLite is then
    free to return either. The autoincrement id is the true insertion order.
    """
    from app.memory import db

    if origin:
        row = db.query_one(
            "SELECT * FROM ideas WHERE origin = ? ORDER BY generated_at DESC, id DESC LIMIT 1",
            (origin,),
        )
    else:
        row = db.query_one("SELECT * FROM ideas ORDER BY generated_at DESC, id DESC LIMIT 1")
    return _row(row)


def history(*, limit: int = 20) -> list[dict[str, Any]]:
    from app.memory import db

    rows = db.query("SELECT * FROM ideas ORDER BY generated_at DESC, id DESC LIMIT ?", (limit,))
    return [r for r in (_row(row) for row in rows) if r]


def _row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        return None
    return {
        "id": row["id"],
        "generated_at": row["generated_at"],
        "day": row["day"],
        "origin": row["origin"],
        "brief": row["brief"],
        "memory_path": row["memory_path"],
        **payload,
    }


async def generate_daily(
    registry: ProviderRegistry,
    settings: Settings,
    *,
    count: int = 3,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The set that lands with the daily brief, from what moved yesterday.

    Runs once a day rather than per cycle, deliberately. Ideas are a
    judgement about what to do next, and a judgement that changes every six
    hours is noise — a day is roughly the shortest window over which "what
    is working" means anything in this market.

    Skips silently when there is nothing to ground it in. Three speculative
    ideas generated from an empty ledger would be worse than none, because
    they would arrive looking exactly like the grounded ones.
    """
    now = now or datetime.now(timezone.utc)

    if not settings.is_available("ai"):
        return {"skipped": "ai not configured"}

    movers = ledger.movers(since_hours=24, limit=20)
    if not movers:
        log.info("daily_ideas_skipped", reason="nothing moved in the last 24h")
        return {"skipped": "nothing moved in the last 24h"}

    payload = await generate(registry, settings, count=count, now=now)
    if "error" in payload:
        return {"error": payload["error"]}

    path = await record(payload, origin="daily", now=now)
    log.info(
        "daily_ideas_generated",
        count=len(payload.get("ideas") or []),
        path=path,
        input_tokens=payload.get("input_tokens"),
    )
    return {
        "generated": len(payload.get("ideas") or []),
        "path": path,
        "grounded_in_movers": len(movers),
        "input_tokens": payload.get("input_tokens"),
        "output_tokens": payload.get("output_tokens"),
    }


def format_for_delivery(payload: dict[str, Any], *, limit: int = 3) -> str:
    """The Discord/Telegram form. Short enough to read on a phone."""
    ideas = (payload.get("ideas") or [])[:limit]
    if not ideas:
        return ""

    lines = ["**Launch ideas from yesterday's movers**", ""]
    if payload.get("read_of_the_market"):
        lines += [payload["read_of_the_market"], ""]

    for idea in ideas:
        lines += [
            f"**{idea.get('name')}**  `${idea.get('ticker')}`  _{idea.get('grounding')}_",
            f"{idea.get('description') or idea.get('angle')}",
            f"· Image: {idea.get('image')}",
            f"· Why now: {idea.get('why_now')}",
            f"· Risk: {idea.get('risk')}",
            "",
        ]

    if payload.get("avoid"):
        lines.append(f"_Avoiding: {', '.join(payload['avoid'][:4])}_")
    return "\n".join(lines)


async def save_as_playbook_entry(payload: dict[str, Any], *, note: str = "") -> str | None:
    """Keep an idea set the operator liked, so later ideas can build on it.

    Only called explicitly for a *requested* set. Auto-saving every generated
    idea would fill the playbook with unvetted output and then feed it back
    in as if it were evidence — a memory poisoning itself one cycle at a
    time. The daily set is the deliberate exception: it is recorded because
    it was asked for by the schedule rather than by a passing whim.
    """
    return await record(payload, origin="requested", brief=note)
