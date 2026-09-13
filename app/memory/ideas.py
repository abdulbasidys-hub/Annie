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
                    "name", "ticker", "angle", "hook",
                    "why_now", "evidence", "grounding", "risk",
                ],
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The token name exactly as it should appear on the "
                        "launchpad. Not a description of a name — the name itself. Apply "
                        "the naming method: name the event, find the canonical subject, "
                        "and ask what Know Your Meme would have titled it tomorrow.",
                    },
                    "ticker": {
                        "type": "string",
                        "description": "Uppercase, 3-8 characters, no $ prefix. Name and "
                        "ticker are one system — the ticker should carry the identity the "
                        "name does not, rather than repeat it.",
                    },
                    "angle": {
                        "type": "string",
                        "description": "What the coin is, in one or two sentences. The "
                        "event or subject behind it, plainly, no marketing language.",
                    },
                    "hook": {
                        "type": "string",
                        "description": "Why anyone would share it. One sentence — the "
                        "contradiction, the absurdity, the resemblance, whatever makes "
                        "somebody send it to a friend. If you cannot name the hook, the "
                        "idea is weak and saying so is more useful than dressing it up.",
                    },
                    "why_now": {
                        "type": "string",
                        "description": "What in the current market makes this the moment. "
                        "Timing is most of the decision.",
                    },
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
                    "risk": {
                        "type": "string",
                        "description": "The strongest reason this fails.",
                    },
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
- The craft section below is a method the operator actually uses, not
  background reading. Work it: name the event before you name the coin, find
  the canonical subject, apply the naming and ticker rules as written. An
  idea that ignores it is a worse idea even if it sounds good.
- You are shown what recent winners shipped as websites. Use it. The
  operator hands your site plan to an agent that builds it, so "what are
  people actually building this week" is a live constraint, not background —
  and a site shape that keeps appearing among winners is worth more than a
  better one nobody is using.
- The strongest grounding available to you is a *catalyst that repeated*.
  You are shown why recent winners actually moved — a viral clip, a post, a
  copycat wave — and which of those looked deliberately repeatable. An idea
  built on a repeatable catalyst is worth more than one built on a theme
  that merely appears often, because the second tells you what was popular
  and the first tells you what can be caused.
- An idea here is a *decision*, not a brief. The operator is choosing which
  one to launch, so give exactly what picks a winner: what it is, why anyone
  would share it, why now, what it rests on, what kills it. They will ask you
  to elaborate on the one they pick, and that is when art direction, the
  website, the X account and the posts get written. Do not produce those here.
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

    # New coins only. An established token opening a new pool arrives looking
    # like a mover, and grounding ideas on those produced a read of the
    # market that "familiar tickers win" — which is a description of a
    # logging artefact, not of anything happening. The operator launches new
    # coins, so the evidence has to be new coins.
    movers = [m for m in ledger.movers(since_hours=72, limit=60) if not m.is_revival][:20]
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

    # Researched causes, repeatable ones first — an idea built on something
    # that can be caused again beats one built on something that was merely
    # popular.
    from app.memory import coins

    # Same filter on the researched causes. A revival's catalyst is real and
    # worth knowing, but it is the wrong evidence for "what should I launch":
    # an exchange listing moving a three-year-old coin is not a thing anyone
    # can reproduce with a new token.
    fresh_mints = {m.mint for m in ledger.movers(since_hours=336, limit=500)
                   if not m.is_revival}
    reasons = sorted(
        [r for r in coins.recent(limit=60) if r.mint in fresh_mints][:25],
        key=lambda r: (not r.repeatable, {"high": 0, "medium": 1}.get(r.confidence, 2)),
    )
    site_shapes = [
        p for p in coins.site_patterns(since_hours=168)
        if p["site_kind"] not in ("none", "dead")
    ]
    instructions = _standing_instructions()

    context = _render_context(
        now=now, brief=brief, movers=movers, winning=winning, crowded=crowded,
        rolling_over=rolling_over, words=words, lessons=lessons, playbook=playbook,
        reasons=reasons, site_shapes=site_shapes, instructions=instructions,
    )

    client = await registry.reasoning.raw_client()
    try:
        response = await client.chat.completions.create(
            model=settings.openai_reasoning_model,
            messages=[
                {"role": "system", "content": _system_prompt()},
                {
                    "role": "user",
                    "content": f"{context}\n\nGive me {max(1, min(count, 4))} idea(s).",
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "launch_ideas", "strict": True, "schema": IDEA_SCHEMA},
            },
            # Three ideas at sixteen fields each, one of which is a 60-100
            # word image prompt, does not fit in 1800 — the response was
            # being truncated mid-JSON and arriving as "unparseable", which
            # is how a day's launch ideas silently failed to send. Raised
            # when the schema grew; it must be raised again if it grows.
            max_completion_tokens=4500,
            temperature=0.8,  # higher than the learning call — this one is meant to reach
            reasoning_effort="none",
        )
    except Exception as exc:
        log.error("idea_generation_failed", exc_info=True)
        return {"error": f"Idea generation failed: {exc}"[:300]}

    try:
        payload = json.loads(response.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        # Almost always truncation rather than malformed output: the response
        # hit max_completion_tokens mid-JSON. Saying which matters, because
        # the fix is a number in this file and the symptom is a day with no
        # launch ideas and no explanation.
        finish = getattr(response.choices[0], "finish_reason", None)
        truncated = finish == "length"
        log.error(
            "idea_generation_unparseable",
            finish_reason=finish,
            truncated=truncated,
            chars=len(response.choices[0].message.content or ""),
        )
        return {
            "error": (
                "The response was cut off before it finished — the token ceiling "
                "is too low for the current schema."
                if truncated
                else "The model returned something unparseable."
            ),
            "truncated": truncated,
        }

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


def _system_prompt() -> str:
    """Voice, then the job, then the craft.

    The skills pack goes last so it is the most recent thing read before the
    market data — it is the part most likely to be ignored, because it asks
    the model to work differently from how it would by default.
    """
    from app.memory import skills

    base = voice.prefix(SYSTEM_PROMPT)
    craft = skills.for_task("launch")
    sep = chr(10) * 2 + "---" + chr(10) * 2
    return f"{base}{sep}{craft}" if craft else base


def _standing_instructions() -> str:
    """What the operator has told her to keep doing, from `core/instructions.md`.

    The same file the cycle already honours. It is loaded here because
    naming a coin and writing the launch post are exactly the places an
    operator has a house style — and a style stated once in chat is useless
    if the thing that generates the ideas never reads it.
    """
    from app.memory import service

    memory = service.read("core/instructions.md")
    if memory is None:
        return ""
    body = memory.body.strip()
    # Skip the seeded placeholder, which is prose about the file rather than
    # an instruction and would otherwise read as one.
    if "no standing instructions" in body.lower():
        return ""
    return body[:2000]


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
    reasons: list[Any] | None = None,
    site_shapes: list[dict[str, Any]] | None = None,
    instructions: str = "",
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
        lines += ["", "## New coins that moved in the last 72h"]
        for m in movers[:15]:
            lines.append(
                f"- {m.symbol or m.name or m.mint[:8]} — peak {usd(m.peak_market_cap)}, "
                f"{m.launchpad or 'unknown pad'}"
            )

    if site_shapes:
        lines += ["", "## What recent winners shipped as websites"]
        for shape in site_shapes[:8]:
            lines.append(
                f"- {shape['site_kind'].replace('_', ' ')}: {shape['coins']} of them"
            )
        lines.append(
            "  (This is what is being built right now. The operator hands your "
            "site plan straight to a build agent.)"
        )

    if reasons:
        # The most actionable block here. Everything above says what was
        # popular; this says what *caused* it, which is the only part that
        # can be deliberately reproduced.
        lines += ["", "## Why recent new coins actually moved"]
        for r in reasons[:12]:
            flag = "REPEATABLE" if r.repeatable else "one-off"
            detail = f" — {r.catalyst_detail}" if r.catalyst_detail else ""
            lines.append(
                f"- {r.symbol or r.mint[:8]} [{r.category or 'uncategorised'}] "
                f"{r.catalyst.replace('_', ' ')}{detail} ({flag}, {r.confidence} confidence)"
            )
            if r.why_now:
                lines.append(f"  timing: {r.why_now}")
            if getattr(r, "site_notes", ""):
                lines.append(f"  their site: {r.site_notes}")

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


    if instructions.strip():
        # Last, and framed as binding. These are the operator's standing
        # orders on how to name a coin and how to write the post — given
        # directly, and they outrank the model's own taste.
        lines += [
            "",
            "## Standing instructions from the operator",
            "",
            "These are not suggestions. Follow them even where your own "
            "judgement would differ.",
            "",
            instructions.strip(),
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

    # Keep the register current. It is the file the operator scans to find an
    # idea from three weeks ago, so it is worth rebuilding on every write
    # rather than on a schedule that could leave it stale.
    try:
        await update_log()
    except Exception:
        log.warning("idea_log_update_failed", exc_info=True)

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


LAUNCHED_SCHEMA = """
CREATE TABLE IF NOT EXISTS launched_ideas (
    ticker      TEXT PRIMARY KEY,
    mint        TEXT NOT NULL,
    launched_at TEXT NOT NULL
)
"""


def mark_launched(ticker: str, mint: str) -> None:
    """Attach an idea to the coin it became.

    Themes cycle: AI one week, politics the next, then something else. An
    idea that was right and untimely is not spent — it comes back when its
    week does. So the register distinguishes three states rather than two:
    launched (this became a coin, here is the contract), and everything else,
    which is still available.
    """
    from app.memory import db

    db.execute(LAUNCHED_SCHEMA)
    db.execute(
        "INSERT INTO launched_ideas(ticker, mint, launched_at) VALUES(?, ?, ?) "
        "ON CONFLICT(ticker) DO UPDATE SET mint = excluded.mint, "
        "launched_at = excluded.launched_at",
        ((ticker or "").strip().lstrip("$").upper(), mint, db.utcnow_iso()),
    )


def launched_map() -> dict[str, dict[str, str]]:
    from app.memory import db

    db.execute(LAUNCHED_SCHEMA)
    rows = db.query("SELECT ticker, mint, launched_at FROM launched_ideas", ())
    return {r["ticker"]: {"mint": r["mint"], "launched_at": r["launched_at"]} for r in rows}


LOG_PATH = "playbook/idea-log.md"


async def update_log() -> str:
    """Rewrite the register of every idea ever generated.

    One file, newest first, so the operator can scan months of ideas in one
    place and say "bring back the worm one". The per-day files hold the full
    reasoning; this is the index into them, and it exists because a list
    spread across forty dated files is not a list anybody reads.

    Rewritten rather than appended: an append-only log would drift out of
    step with the database the first time a set failed to record, and this
    is cheap to rebuild from the rows that are the actual record.
    """
    entries = history(limit=300)
    if not entries:
        return LOG_PATH

    lines = [
        "Ideas still available, newest first. Nothing here expires — themes "
        "cycle, and one that was right but untimely comes back when its week "
        "does. Ask for any by ticker to elaborate it into a full launch kit. "
        "Anything we launched has left this list and lives in Our Launches "
        "with its contract address.",
        "",
    ]
    launched = launched_map()
    current_day = None
    keys: list[str] = []
    for entry in entries:
        day = entry.get("day") or (entry.get("generated_at") or "")[:10]
        if day != current_day:
            current_day = day
            origin = entry.get("origin") or "daily"
            lines += ["", f"## {day}" + (" (requested)" if origin == "requested" else "")]
            if entry.get("brief"):
                lines.append(f"_Asked for: {entry['brief']}_")
            lines.append("")
        for idea in entry.get("ideas") or []:
            ticker = str(idea.get("ticker") or "?").upper()
            keys.append(ticker.lower())
            grounding = idea.get("grounding") or "?"
            summary = idea.get("angle") or idea.get("description") or ""
            if ticker in launched:
                # It became a coin. It lives in Our Launches now, with its
                # contract address and its check-ins — leaving it here too
                # would make this a list of things to consider that is
                # partly things already done.
                continue
            lines.append(f"- **${ticker}** — {idea.get('name')} _({grounding})_")
            if summary:
                lines.append(f"  {summary}")

    await service.write(
        LOG_PATH,
        body="\n".join(lines),
        title="Idea log — every idea, newest first",
        kind="playbook",
        tags=["ideas", "log"],
        keys=keys[:200],
        importance=0.7,
        confidence="high",
        source="deterministic",
    )
    return LOG_PATH


def log_entries(limit: int = 50) -> list[dict[str, Any]]:
    """The register as data, for the API and the bots."""
    out: list[dict[str, Any]] = []
    launched = launched_map()
    for entry in history(limit=100):
        for idea in entry.get("ideas") or []:
            if str(idea.get("ticker") or "").upper() in launched:
                continue
            out.append({
                "ticker": idea.get("ticker"),
                "name": idea.get("name"),
                "angle": idea.get("angle") or idea.get("description"),
                "grounding": idea.get("grounding"),
                "day": entry.get("day"),
                "origin": entry.get("origin"),
                "set_id": entry.get("id"),
            })
            if len(out) >= limit:
                return out
    return out


def find_idea(ticker: str = "", *, name: str = "") -> dict[str, Any] | None:
    """One idea from recent history, by ticker or name.

    Searched across recent sets rather than only the latest, because the
    operator may well come back to yesterday's third idea — and because
    "elaborate on $LAWCAT" should not depend on which set it came from.
    """
    want_t = (ticker or "").strip().lstrip("$").upper()
    want_n = (name or "").strip().lower()
    if not want_t and not want_n:
        return None

    # Searches launched ideas too, unlike the register. Relaunching a
    # theme when its week comes back round is the point, and "elaborate on
    # $LAWCAT" should still work for one we shipped in August.
    entries = history(limit=200)
    for entry in entries:
        for idea in entry.get("ideas") or []:
            if want_t and str(idea.get("ticker", "")).strip().upper() == want_t:
                return {**idea, "set_id": entry.get("id"), "day": entry.get("day")}
            if want_n and str(idea.get("name", "")).strip().lower() == want_n:
                return {**idea, "set_id": entry.get("id"), "day": entry.get("day")}

    # Then partial, because people remember a coin as "the worm one" rather
    # than by its exact registered name.
    needle = want_n or want_t.lower()
    if len(needle) >= 3:
        for entry in entries:
            for idea in entry.get("ideas") or []:
                haystack = f"{idea.get('name', '')} {idea.get('ticker', '')}".lower()
                if needle in haystack:
                    return {**idea, "set_id": entry.get("id"), "day": entry.get("day")}
    return None


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
            str(idea.get("angle") or idea.get("description") or ""),
        ]
        if idea.get("hook"):
            lines.append(f"· Hook: {idea['hook']}")
        lines += [
            f"· Why now: {idea.get('why_now')}",
            f"· Risk: {idea.get('risk')}",
            "",
        ]

    lines += [
        '_Say "elaborate on $TICKER" for the full launch kit — art prompts, '
        "site, X, Telegram and the posts._",
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
