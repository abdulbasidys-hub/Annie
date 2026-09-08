"""Annie's agent loop (§47, §62) — the piece the original build left unwritten.

Two OpenAI calls per turn, not one:

1. A bounded **tool-calling loop** (§36: "must use bounded research loops")
   against the raw ``AsyncOpenAI`` client, so Annie can query the database and
   (if configured) the web before answering.
2. One **structured finishing call** that restates the answer through a JSON
   schema requiring ``claim_type``, ``confidence`` and ``citations`` —
   :mod:`app.annie.persona` asks for these in prose, but a prompt asking an
   LLM to self-label is not the same guarantee as a schema the API refuses to
   violate. This is the same "schema over free text" discipline
   :class:`app.providers.openai_provider.OpenAIReasoner` already uses
   elsewhere; the agent loop needs the raw client only because multi-turn tool
   calling isn't expressible through that class's single-shot ``structured()``.

Every tool call is logged via :meth:`FirestoreRepo.record_tool_call` (§47).
Most tools only ever *read*, so a model that misuses one can produce a wrong
answer, never wrong data. The two exceptions are scoped narrowly on purpose:
``manage_discord_channel`` (only offered when Discord already confirmed the
permission) and ``create_research_task``, which queues bounded, budgeted
background work through the same engine autonomous research uses
(:mod:`app.research.runner`) rather than touching token/trend data directly —
this is what lets "go look into X" in chat or Discord/Telegram actually start
real work instead of Annie describing a capability :mod:`app.annie.persona`
tells her she has but that never existed as a callable tool (fixed
2026-08-25).

**Model: pinned to gpt-5.6-luna, deliberately — no other OpenAI model may be
called.** Two API quirks specific to this model, discovered by actually
calling it rather than assumed, apply to every ``chat.completions.create()``
call in this file and in :mod:`app.providers.openai_provider`:

* ``max_tokens`` is rejected outright; use ``max_completion_tokens``.
* Function tools are rejected alongside this model's default
  ``reasoning_effort`` ("use /v1/responses or set reasoning_effort to
  'none'"). Every call here sets ``reasoning_effort="none"`` rather than
  migrating to the Responses API — Luna is OpenAI's fast/cheap tier, not the
  deep-reasoning flagship, so skipping its extended-reasoning pass fits both
  the model's role and this app's compact, tool-driven calls.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import structlog

from app.annie import persona
from app.annie.platform import PlatformContext
from app.config import Settings
from app.db.models.ops import ToolCall
from app.db.repo import FirestoreRepo
from app.providers.openai_provider import estimate_cost
from app.providers.registry import ProviderRegistry

log = structlog.get_logger(__name__)

MAX_TOOL_ROUNDS = 6
MAX_CONTEXT_MESSAGES = 20  # prior turns fed back in, oldest trimmed first


@dataclass(slots=True)
class AgentReply:
    content: str
    claim_type: str | None
    confidence: str | None
    citations: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: Decimal | None = None
    latency_ms: int | None = None


FINAL_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["content", "claim_type", "confidence", "citations"],
    "properties": {
        "content": {
            "type": "string",
            "description": "The answer, in Annie's voice, formatted per the system prompt.",
        },
        "claim_type": {
            "type": "string",
            "enum": ["fact", "inference", "hypothesis", "speculation"],
            "description": "The strongest claim type the answer relies on.",
        },
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "id", "label"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["token", "trend", "creator", "launchpad", "note", "report"],
                    },
                    "id": {"type": ["string", "null"]},
                    "label": {"type": "string"},
                },
            },
        },
    },
}


class AnnieAgent:
    def __init__(
        self,
        repo: FirestoreRepo,
        registry: ProviderRegistry,
        settings: Settings,
        *,
        platform_context: PlatformContext | None = None,
    ) -> None:
        self.repo = repo
        self.registry = registry
        self.settings = settings
        self.platform_context = platform_context

    async def respond(self, *, conversation_id: str | None, user_message: str) -> AgentReply:
        started = time.perf_counter()
        client = await self.registry.reasoning.raw_client()
        model = self.settings.openai_reasoning_model

        capabilities_note = _capabilities_note(self.settings)
        channel_note = _channel_note(self.platform_context)
        sender_note = _sender_context_note(self.platform_context)
        instructions_note = _standing_instructions_note()
        personality_overrides = await _personality_overrides(self.repo)
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": persona.system_prompt(
                    capabilities_note=(
                        capabilities_note + channel_note + sender_note + instructions_note
                    ),
                    personality_overrides=personality_overrides,
                ),
            },
        ]
        if conversation_id:
            history = await self.repo.list_messages(conversation_id, limit=MAX_CONTEXT_MESSAGES)
            for m in history[-MAX_CONTEXT_MESSAGES:]:
                role = "assistant" if m.role == "annie" else "user"
                messages.append({"role": role, "content": m.content})
        messages.append({"role": "user", "content": user_message})

        tools = _tool_specs(self.settings, self.platform_context)
        tool_call_log: list[dict[str, Any]] = []
        total_input = 0
        total_output = 0

        for _round in range(MAX_TOOL_ROUNDS):
            response = await client.chat.completions.create(
                model=model, messages=messages, tools=tools, tool_choice="auto",
                temperature=0.2, max_completion_tokens=1200,
                # gpt-5.6-luna's default reasoning_effort is incompatible with
                # function tools on this endpoint ("use /v1/responses or set
                # reasoning_effort to 'none'") — 'none' keeps the simpler
                # chat.completions surface, which fits this tool loop and
                # Luna's fast/cheap-tier role better than an extra reasoning pass.
                reasoning_effort="none",
            )
            usage = getattr(response, "usage", None)
            total_input += getattr(usage, "prompt_tokens", 0) or 0
            total_output += getattr(usage, "completion_tokens", 0) or 0

            choice = response.choices[0]
            calls = choice.message.tool_calls or []
            if not calls:
                # Model is done reasoning. Fold its own summary in as context
                # for the structured finishing call below, then stop looping.
                if choice.message.content:
                    messages.append({"role": "assistant", "content": choice.message.content})
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": choice.message.content,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.function.name, "arguments": c.function.arguments},
                        }
                        for c in calls
                    ],
                }
            )

            for call in calls:
                result, succeeded, error = await self._run_tool(call.function.name, call.function.arguments)
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, default=str)}
                )
                entry = {
                    "tool": call.function.name,
                    "arguments": _safe_args(call.function.arguments),
                    "succeeded": succeeded,
                    "error_message": error,
                }
                tool_call_log.append(entry)
                await self.repo.record_tool_call(
                    ToolCall(
                        tool=call.function.name,
                        arguments=_safe_args(call.function.arguments),
                        succeeded=succeeded,
                        error_message=error,
                        conversation_id=conversation_id,
                    )
                )
        else:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"You've used all {MAX_TOOL_ROUNDS} tool rounds available for this turn. "
                        "Answer now with what you have, and say plainly what you couldn't check."
                    ),
                }
            )

        final = await self._finish(client, model, messages)
        total_input += final["input_tokens"]
        total_output += final["output_tokens"]

        latency_ms = int((time.perf_counter() - started) * 1000)
        cost = estimate_cost(model, total_input, total_output)

        return AgentReply(
            content=final["content"],
            claim_type=final["claim_type"],
            confidence=final["confidence"],
            citations=final["citations"],
            tool_calls=tool_call_log,
            model=model,
            input_tokens=total_input,
            output_tokens=total_output,
            cost_usd=cost,
            latency_ms=latency_ms,
        )

    async def _finish(self, client: Any, model: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """The structured finishing call — see module docstring.

        This is the only call whose output the user actually sees, so the
        instruction below has to work against strict-JSON-schema mode's own
        pull toward flatter, more literal text (confirmed as a real
        complaint, 2026-08-25 — "the chatbot is not even giving the vibes").
        Earlier wording just said "restate your answer", with nothing
        counteracting that pull — this one says explicitly not to.
        """
        prompt_messages = messages + [
            {
                "role": "user",
                "content": (
                    "Put your answer to the CURRENT question into the required JSON "
                    "object's `content` field — word for word if you already drafted "
                    "it earlier in this turn's own reasoning above, not a flattened or "
                    "safer-sounding rewrite. This is not a request to reuse or echo "
                    "anything from your answers to EARLIER questions in this "
                    "conversation's history — a new question gets a new answer, "
                    "grounded in tool calls made for it specifically, even if an "
                    "earlier turn covered similar ground. Keep your actual voice: the "
                    "tone, warmth, dry humour and directness from the system prompt's "
                    "Voice section belong in `content` exactly as much as in a "
                    "plain-text reply. Do not add new claims not already grounded in "
                    "the tool results above — if you did not check something, its "
                    "claim_type must reflect that (hypothesis/speculation), not fact."
                ),
            }
        ]
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=prompt_messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "annie_answer", "strict": True, "schema": FINAL_ANSWER_SCHEMA},
                },
                max_completion_tokens=1200,
                temperature=0.2,
                reasoning_effort="none",
            )
        except Exception as exc:  # the schema call itself failed — degrade honestly
            log.warning("annie_finish_failed", error=str(exc))
            return {
                "content": "Something went wrong formatting that answer. Try asking again.",
                "claim_type": "speculation",
                "confidence": "low",
                "citations": [],
                "input_tokens": 0,
                "output_tokens": 0,
            }

        usage = getattr(response, "usage", None)
        content = response.choices[0].message.content or "{}"
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            payload = {}

        return {
            "content": payload.get("content") or "I don't have an answer for that.",
            "claim_type": payload.get("claim_type", "speculation"),
            "confidence": payload.get("confidence", "low"),
            "citations": payload.get("citations") or [],
            "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
        }

    # -- tool dispatch ----------------------------------------------------

    async def _run_tool(self, name: str, raw_arguments: str) -> tuple[Any, bool, str | None]:
        try:
            args = json.loads(raw_arguments) if raw_arguments else {}
        except json.JSONDecodeError:
            return {"error": "arguments were not valid JSON"}, False, "invalid arguments"

        handler = _TOOL_HANDLERS.get(name)
        if handler is None:
            return {"error": f"unknown tool {name!r}"}, False, "unknown tool"

        try:
            result = await handler(self, args)
            return result, True, None
        except Exception as exc:
            log.warning("annie_tool_failed", tool=name, error=str(exc))
            return {"error": str(exc)}, False, str(exc)


# -----------------------------------------------------------------------------
# Tools — read-only, each backed directly by the repository or a provider.
# -----------------------------------------------------------------------------


async def _tool_dashboard_summary(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    """Where things stand — and, when nothing is there, why.

    The ``why`` is the part that matters. A deployment whose webhook is
    misconfigured and one that started ten minutes ago produce identical
    zeros, and reporting those zeros without distinguishing them is honest
    but useless. :mod:`app.memory.health` classifies which it is, and that
    verdict is attached here so the answer can name a cause instead of
    listing counts.

    ``*_raw`` counts every signal regardless of sample size;
    ``signals_meaningful`` applies the statistical bars. Both are reported
    rather than picking one, after a real inconsistency where a raw count of
    18 "new" trends sat next to a filtered list returning zero of them with
    nothing in either result explaining the gap.
    """
    from app.memory import health, index, ledger, signals

    stats = ledger.stats()
    counts = signals.counts()
    diagnosis = health.diagnose()

    result: dict[str, Any] = {
        "launches_seen_24h": stats["sightings_24h"],
        "currently_watching": stats["watching"],
        "reached_a_tier_24h": stats["qualified_24h"],
        "reached_a_tier_total": stats["qualified_total"],
        "creators_seen": stats["creators_total"],
        "creators_tracked": stats["creators_tracked"],
        "creator_movements_24h": stats["moves_24h"],
        "launchpads_24h": stats["launchpads"],
        "signals_raw": {k: v for k, v in counts.items() if k != "meaningful"},
        "signals_meaningful": counts.get("meaningful", 0),
        "memory_files": index.stats()["files"],
        "pipeline_state": diagnosis["state"],
    }

    if diagnosis["state"] != "healthy":
        result["why_there_is_nothing"] = diagnosis["headline"]
        result["what_the_operator_should_check"] = diagnosis["what_to_check"]
        result["note"] = (
            "There is no usable data, and the reason is above. Tell them what "
            "is actually wrong and what to check, in plain words. Do NOT "
            "present this as a market finding, and do NOT recite the zeros "
            "back at them — 'nothing has arrived' is the answer, the counts "
            "are just how you know."
        )
    else:
        result["note"] = (
            "Launches seen is everything sighted; almost none of it is kept. "
            "signals_raw counts every characteristic regardless of sample size "
            "— use list_signals (not the raw count) to cite anything specific."
        )
    return result


async def _tool_system_status(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    """Is the pipeline actually working, and if not, what is wrong.

    Call this when someone asks why you have no data, why nothing is
    updating, or whether something is broken. It distinguishes a webhook
    that is not delivering from a deployment that is simply new, which the
    raw counts cannot.
    """
    from app.memory import health

    return health.diagnose()


async def _tool_search_tokens(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    """Tokens that actually moved, from the ledger.

    Note what this does *not* search: the thousands of launches a day that
    never traded. Those are sighted, pruned within 48 hours, and were never
    subjects. If someone asks about a specific mint that is not here, that
    is what ``live_token_lookup`` is for.
    """
    from app.memory import ledger

    limit = min(int(args.get("limit") or 10), 25)
    hours = min(int(args.get("hours") or 24), 720)
    floor = float(args.get("min_market_cap_usd") or ledger.WATCH_FLOOR_USD)

    found = ledger.movers(since_hours=hours, min_market_cap=floor, limit=limit)
    if args.get("qualified_only"):
        found = [t for t in found if t.qualified_at]
    if args.get("launchpad_slug"):
        found = [t for t in found if t.launchpad == args["launchpad_slug"]]

    return {
        "window_hours": hours,
        "tokens": [
            {
                "mint": t.mint, "name": t.name, "symbol": t.symbol,
                "launchpad": t.launchpad, "creator_wallet": t.creator,
                "market_cap": t.market_cap, "peak_market_cap": t.peak_market_cap,
                "tier_reached": t.tier, "qualified_at": t.qualified_at,
                "round_tripped": bool(
                    t.peak_market_cap and t.market_cap
                    and t.market_cap < t.peak_market_cap * 0.5
                ),
            }
            for t in found
        ],
    }


async def _tool_get_token(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    """Everything Annie holds about one mint: ledger row plus any memory.

    The memory half is the interesting one — if this token was worth writing
    about, the file carries her actual reasoning, not just numbers.
    """
    from app.memory import index, ledger, service

    mint = str(args.get("mint") or "").strip()
    if not mint:
        return {"error": "mint is required"}

    sighting = ledger.get_sighting(mint)
    memory = service.read(service.token_path(mint))
    related = index.by_key(mint, limit=4)

    if sighting is None and memory is None and not related:
        return {
            "found": False,
            "note": (
                "Nothing on this mint. Either it never did anything worth "
                "keeping, or it is too new. Use live_token_lookup for its "
                "current market data straight from the chain."
            ),
        }

    result: dict[str, Any] = {"found": True, "mint": mint}
    if sighting is not None:
        result["ledger"] = {
            "name": sighting.name, "symbol": sighting.symbol,
            "creator_wallet": sighting.creator, "launchpad": sighting.launchpad,
            "first_seen": sighting.first_seen, "market_cap": sighting.market_cap,
            "peak_market_cap": sighting.peak_market_cap, "tier_reached": sighting.tier,
            "qualified_at": sighting.qualified_at, "status": sighting.status,
        }
    if memory is not None:
        result["memory"] = {"path": memory.path, "content": memory.body}
    if related:
        result["mentioned_in"] = [
            {"path": h.path, "excerpt": h.snippet} for h in related if h.path != (memory.path if memory else "")
        ]
    return result


async def _tool_list_signals(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    """Characteristics over-represented among tokens that cleared a tier.

    Defaults to those with enough sample to mean something. Sorting purely
    by percentage change is a trap this fell into once: a characteristic
    appearing exactly once in a tiny cohort produces a huge swing against a
    zero baseline and would rank first, ahead of anything with real
    standing — which is how a single token's incidental use of the word
    "still" once got cited as a narrative.
    """
    from app.analysis.stats import MIN_OCCURRENCES, MIN_RECENT_SAMPLE
    from app.memory import signals

    limit = min(int(args.get("limit") or 10), 25)
    include_thin = bool(args.get("include_low_confidence", False))
    items = signals.listing(
        status=args.get("status"), limit=limit, include_thin=include_thin
    )
    return {
        "note": (
            None if include_thin else
            f"Filtered to signals with enough sample size to mean something "
            f"(cohort >= {MIN_RECENT_SAMPLE} tokens, characteristic seen >= "
            f"{MIN_OCCURRENCES} times). Pass include_low_confidence=true for "
            f"thinner samples — label those speculation or hypothesis, never fact."
        ),
        "signals": [
            {
                "slug": s_["slug"], "name": s_["name"], "category": s_["category"],
                "status": s_["status"], "confidence": s_["confidence"],
                "cohort_threshold_usd": s_["tier"],
                "recent_count": s_["recent_count"], "recent_total": s_["recent_total"],
                "recent_frequency": s_["recent_freq"], "baseline_frequency": s_["baseline_freq"],
                "lift": s_["lift"], "persistence_days": s_["persistence"],
                "thin_sample": s_["thin_sample"],
            }
            for s_ in items
        ],
    }


async def _tool_get_signal(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    from app.memory import signals

    slug = str(args.get("slug") or "").strip()
    found = signals.get(slug) if slug else None
    if found is None:
        return {"found": False}
    return {"found": True, **found}


async def _tool_list_creators(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    """Creator wallets, by lifetime record or by who is busy right now.

    Every launch by every wallet is recorded, so this is complete rather
    than a sample — that is the whole reason creator movements live in the
    local ledger instead of as per-wallet remote documents.
    """
    from app.memory import ledger

    limit = min(int(args.get("limit") or 10), 25)
    window = args.get("window_hours")
    creators = ledger.top_creators(
        limit=limit,
        tracked_only=bool(args.get("tracked_only")),
        window_hours=int(window) if window else None,
    )
    if args.get("winners_only"):
        creators = [c for c in creators if (c.get("winners") or 0) > 0]
    return {
        "ordered_by": "launches in window" if window else "winners, then best result",
        "creators": [
            {
                "wallet": c["wallet"],
                "total_launches": c["launches"],
                "launches_in_window": c.get("recent_launches"),
                "winners": c["winners"],
                "best_market_cap": c["best_market_cap"],
                "best_mint": c["best_mint"],
                "hit_rate": (
                    round(100 * c["winners"] / c["launches"], 2) if c["launches"] else None
                ),
                "tracked": bool(c["tracked"]),
                "dossier": c.get("dossier_path"),
            }
            for c in creators
        ],
    }


async def _tool_get_creator(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    """One wallet's full record: totals, its tokens, and its dossier.

    The dossier is prose Annie wrote about this wallet. Prefer quoting that
    over restating the numbers — the numbers are already in the table above
    it, and the reason the wallet is interesting is usually in the prose.
    """
    from app.memory import ledger, service

    wallet = str(args.get("wallet") or "").strip()
    if not wallet:
        return {"error": "wallet is required"}

    creator = ledger.get_creator(wallet)
    if creator is None:
        return {"found": False, "note": "This wallet has not been seen launching anything."}

    tokens = ledger.creator_tokens(wallet, limit=20)
    dossier = service.read(service.creator_path(wallet))
    recent = ledger.creator_moves(wallet, limit=25)
    return {
        "found": True,
        "wallet": wallet,
        "total_launches": creator["launches"],
        "winners": creator["winners"],
        "best_market_cap": creator["best_market_cap"],
        "best_mint": creator["best_mint"],
        "tracked": bool(creator["tracked"]),
        "first_seen": creator["first_seen"],
        "last_seen": creator["last_seen"],
        "tokens": [
            {
                "mint": t.mint, "symbol": t.symbol, "peak_market_cap": t.peak_market_cap,
                "qualified_at": t.qualified_at,
            }
            for t in tokens
        ],
        "recent_movements": [
            {"at": m["at"], "kind": m["kind"], "mint": m["mint"]} for m in recent
        ],
        "dossier": dossier.body if dossier else None,
    }


async def _tool_get_launchpad(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    slug = str(args.get("slug") or "").strip()
    lp = await agent.repo.get_launchpad(slug) if slug else None
    if lp is None:
        return {"found": False}
    return {
        "found": True, "slug": lp.slug, "name": lp.name, "lifecycle": lp.lifecycle,
        "launch_count": lp.launch_count, "qualified_count": lp.qualified_count,
        "success_rate": lp.success_rate, "growth_rate_7d": lp.growth_rate_7d,
        "is_known": lp.is_known, "first_seen_at": _iso(lp.first_seen_at),
    }


async def _tool_list_research_notes(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    limit = min(int(args.get("limit") or 5), 20)
    notes = await agent.repo.list_research_notes(current_only=True, limit=limit)
    return {
        "notes": [
            {
                "title": n.title, "body": n.body, "claim_type": n.claim_type,
                "confidence": n.confidence, "sample_size": n.sample_size,
                "created_at": _iso(n.created_at),
            }
            for n in notes
        ]
    }


async def _tool_live_token_lookup(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    """Live, on-demand market data for any mint — qualified or not.

    Separate from search_tokens/get_token on purpose: those only ever see
    what's already in the research database, which since the discovery
    redesign only contains tokens that migrated *and* cleared a $100k+ tier.
    A user dropping a random CA and asking "what's this worth right now" is
    asking about the chain, not the database — this calls the market
    provider directly instead of returning "not found" for every token that
    hasn't been through the daily qualification run yet.
    """
    mint = str(args.get("mint") or "").strip()
    if not mint:
        return {"error": "mint is required"}

    resolved = await agent.registry.resolve_market_cap(mint, cross_validate=True)
    if resolved.quote is None:
        return {
            "found": False,
            "mint": mint,
            "note": (
                "No live market data available. Most likely this token hasn't "
                "migrated off its bonding curve to a real DEX pool yet — this "
                "deployment's market data comes from DexScreener, which only "
                "indexes real pools. Could also mean the mint is wrong."
            ),
            "errors": resolved.errors,
        }

    quote = resolved.quote
    return {
        "found": True,
        "mint": mint,
        "source": "live_lookup",
        "note": "Live chain/market data, not a claim from the qualified research database.",
        "provider": resolved.provider,
        "verification_status": resolved.verification_status,
        "disputed": resolved.conflict is not None,
        "price_usd": _money(quote.price_usd),
        "market_cap": _money(quote.market_cap),
        "liquidity_usd": _money(quote.liquidity_usd),
        "volume_24h_usd": _money(quote.volume_24h_usd),
    }


async def _tool_search_memory(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    """Annie's own notebook — what she has learned, in her own words.

    This is the tool to reach for first on almost any question about the
    market, because it is where her actual thinking lives; the ledger tools
    above only hold numbers.

    Retrieval is keys-first: passing a bare contract address or creator
    wallet resolves through the exact-handle index in a single probe rather
    than a text scan. Excerpts come back, not whole files — call
    ``read_memory`` when an excerpt is clearly the right file and you need
    the rest of it.
    """
    from app.memory import index

    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    limit = min(int(args.get("limit") or 5), 12)
    hits = index.search(query, limit=limit, section=args.get("section"))
    return {
        "query": query,
        "matched_by": "exact handle" if hits and hits[0].matched_key else "text relevance",
        "results": [
            {"path": h.path, "title": h.title, "section": h.section, "excerpt": h.snippet}
            for h in hits
        ],
        "note": (
            None if hits else
            "Nothing in memory on this. Say so plainly rather than reasoning "
            "from general knowledge as though it were something you observed."
        ),
    }


async def _tool_read_memory(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    """Open one memory file whole, by path (get the path from search_memory)."""
    from app.memory import service
    from app.memory.paths import MemoryPathError

    try:
        memory = service.read(str(args.get("path") or ""))
    except MemoryPathError as exc:
        return {"error": str(exc)}
    if memory is None:
        return {"found": False}
    return {
        "found": True, "path": memory.path, "title": memory.title,
        "updated": memory.updated, "tags": memory.tags, "content": memory.body,
    }


#: Files Annie must never delete from chat. These four are seeded on boot and
#: every cycle reads them as established context; losing one mid-conversation
#: would quietly change how she thinks until someone noticed. They can still
#: be *rewritten* — emptying one is the intended way to reset a belief.
_UNDELETABLE = frozenset(
    {
        "core/market-model.md",
        "core/whats-working.md",
        "core/open-questions.md",
        "core/watchlist.md",
        "core/instructions.md",
    }
)


def _memory_path_from(args: dict[str, Any], *, default_section: str = "notes") -> str:
    """Turn what someone said in chat into a real memory path.

    People do not talk in paths. They say "make me a file called crowded
    narratives" or "put this in the playbook", so this takes a loose name
    plus an optional section and produces ``section/slug.md``. A path the
    model has already formed properly is passed through untouched.
    """
    from app.memory.paths import SECTIONS, slug

    raw = str(args.get("path") or args.get("name") or args.get("topic") or "").strip()
    if not raw:
        raise ValueError("a name or path is required")

    if "/" in raw:
        return raw  # already sectioned; safe_relpath validates it downstream

    section = str(args.get("section") or default_section).strip().lower()
    if section not in SECTIONS:
        section = default_section
    return f"{section}/{slug(raw)}.md"


async def _tool_create_memory(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    """Create a named memory file because the operator asked for one.

    This is the "make me a file called X" path. It exists because the
    operator wants to be able to direct what Annie keeps — she watches the
    market, but they know things she does not, and a system that can only
    write what it inferred is missing half its memory.

    Creating it empty is allowed and normal: the usual shape of this
    conversation is "make a file called crowded narratives" followed by
    several messages of content, so the file has to exist before the content
    arrives.
    """
    from app.memory import service
    from app.memory.paths import MemoryPathError, safe_relpath

    try:
        path = safe_relpath(_memory_path_from(args))
    except (MemoryPathError, ValueError) as exc:
        return {"created": False, "error": str(exc)}

    if service.exists(path):
        existing = service.read(path)
        return {
            "created": False,
            "path": path,
            "already_exists": True,
            "current_content": (existing.body if existing else "")[:2000],
            "note": "This file already exists — its current content is above. "
                    "Use write_memory to add to it or replace it; do not create it again.",
        }

    body = str(args.get("text") or "").strip()
    title = str(args.get("title") or "").strip()

    memory = await service.write(
        path,
        body=body or "_Created from chat. Nothing written yet._",
        title=title or path.rsplit("/", 1)[-1].removesuffix(".md").replace("-", " ").title(),
        tags=[str(t) for t in (args.get("tags") or [])][:6] or ["from-chat"],
        keys=[str(k) for k in (args.get("keys") or [])][:8],
        importance=max(0.0, min(1.0, float(args.get("importance") or 0.6))),
        source="operator-directed",
    )
    return {
        "created": True,
        "path": memory.path,
        "empty": not body,
        "note": "Created. Tell them the path so they know where it went, and "
                "ask what should go in it if it is empty.",
    }


async def _tool_write_memory(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    """Write into a memory file — appending by default, replacing on request.

    Two different situations use this, and the distinction is who decided:

    * **The operator dictated it.** They said what to write and where. Any
      section is fair game, including ``core/``, because they are allowed to
      correct what Annie believes — that is the point of a notebook you can
      both write in.
    * **Annie noticed something mid-conversation.** Then it belongs in
      ``notes/`` and nowhere else, because a chat turn sees one person's
      question and is not the place to rewrite standing beliefs. The
      scheduled cycle promotes anything durable later, against a full window
      of evidence.

    Appending is the default because it is non-destructive: the failure mode
    of a replace is silently losing prose nobody can get back, and the model
    will not always be sure which one was meant.
    """
    from app.memory import service
    from app.memory.paths import MemoryPathError, safe_relpath

    text = str(args.get("text") or "").strip()
    if not text:
        return {"saved": False, "error": "text is required"}

    try:
        path = safe_relpath(_memory_path_from(args))
    except (MemoryPathError, ValueError) as exc:
        return {"saved": False, "error": str(exc)}

    replace = bool(args.get("replace"))
    existed = service.exists(path)

    try:
        if replace and existed:
            previous = service.read(path)
            memory = await service.write(
                path,
                body=text,
                title=str(args.get("title") or "").strip() or (previous.title if previous else None),
                tags=[str(t) for t in (args.get("tags") or [])][:6],
                keys=[str(k) for k in (args.get("keys") or [])][:8],
                importance=(
                    max(0.0, min(1.0, float(args["importance"])))
                    if args.get("importance") is not None
                    else (previous.importance if previous else 0.6)
                ),
                source="operator-directed",
            )
            return {
                "saved": True, "path": memory.path, "mode": "replaced",
                "replaced_length": len(previous.body) if previous else 0,
                "note": "The previous content is gone. Say so plainly when confirming.",
            }

        memory = await service.append(
            path,
            text,
            heading=_now_heading(),
            title=str(args.get("title") or "").strip() or None,
            tags=[str(t) for t in (args.get("tags") or [])][:6],
            keys=[str(k) for k in (args.get("keys") or [])][:8],
        )
    except MemoryPathError as exc:
        return {"saved": False, "error": str(exc)}

    return {
        "saved": True,
        "path": memory.path,
        "mode": "appended" if existed else "created",
        "note": "Confirm with the path, so they know exactly where it landed.",
    }


async def _tool_remember_instruction(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    """Record a standing instruction — something to keep doing from now on.

    Distinct from ``write_memory`` on purpose. A note is something Annie
    knows; an instruction is something she *does*, every cycle, whether or
    not the current question reminded her of it. They need different
    handling, and collapsing them would mean a directive sitting in
    ``notes/`` where only a matching search would ever surface it.

    ``core/instructions.md`` is loaded whole into every cycle and every
    conversation, so anything recorded here is genuinely standing.
    """
    from app.memory import service

    text = str(args.get("instruction") or "").strip()
    if not text:
        return {"saved": False, "error": "instruction is required"}

    existing = service.read("core/instructions.md")
    body = existing.body if existing else ""
    # The seed placeholder should disappear the moment there is a real one,
    # rather than sitting above the list contradicting it.
    body = body.replace("_No standing instructions yet._", "").rstrip()

    entry = f"- {text}"
    if args.get("applies_to"):
        entry += f"\n  - Applies to: {args['applies_to']}"

    await service.write(
        "core/instructions.md",
        body=f"{body}\n\n{entry}".strip(),
        title="Standing instructions",
        tags=["core", "instructions"],
        importance=1.0,
        confidence="high",
        source="operator-directed",
    )
    return {
        "saved": True,
        "path": "core/instructions.md",
        "instruction": text,
        "note": "Recorded. You will read this at the start of every cycle and "
                "every conversation from now on. Confirm it back to them in "
                "their own words so they can tell you got it right.",
    }


async def _tool_delete_memory(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    """Delete a memory file, when explicitly told to.

    Real deletion. A memory system that only accumulates is a database with
    extra steps, and being told "that was wrong, forget it" is exactly the
    correction that keeps the rest worth reading.

    The four seeded ``core/`` files are refused: every cycle reads them as
    established context, so losing one would quietly change how Annie thinks
    until someone noticed. Emptying one with a replace is the intended way
    to reset a belief.
    """
    from app.memory import service
    from app.memory.paths import MemoryPathError, safe_relpath

    try:
        path = safe_relpath(_memory_path_from(args))
    except (MemoryPathError, ValueError) as exc:
        return {"deleted": False, "error": str(exc)}

    if path in _UNDELETABLE:
        return {
            "deleted": False,
            "path": path,
            "error": f"{path} is one of the four core files every cycle reads. "
                     "It cannot be deleted, but you can replace its contents with "
                     "write_memory (replace=true) to reset it.",
        }

    if not service.exists(path):
        return {"deleted": False, "path": path, "error": "no such memory file"}

    removed = await service.forget(path)
    return {"deleted": removed, "path": path}


async def _tool_list_memory(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    """What files exist, so "add to the file about X" can find the right one.

    Metadata only, never bodies — listing the whole notebook's contents into
    a chat turn is the exact failure this system was rebuilt to avoid.
    """
    from app.memory import service

    section = str(args.get("section") or "").strip().lower() or None
    tree = service.tree()
    sections = {section: tree.get(section, [])} if section else tree

    return {
        "files": [
            {
                "path": entry["path"],
                "title": entry["title"],
                "section": name,
                "updated": entry["updated"],
                "summary": entry["summary"],
            }
            for name, entries in sections.items()
            for entry in entries
        ][:60],
        "total": sum(len(v) for v in sections.values()),
    }


async def _tool_token_idea(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    """Generate launch ideas grounded in current market state and memory.

    Call this when someone asks what to launch, what is working, or for an
    angle. It costs a model call of its own, so do not call it to answer a
    question that ``search_memory`` already covers.
    """
    from app.memory import ideas

    return await ideas.generate(
        agent.registry,
        agent.settings,
        brief=str(args.get("brief") or "").strip(),
        count=min(int(args.get("count") or 3), 4),
    )


def _now_heading() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC (from chat)")


async def _tool_remember_person(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    """Only offered when a turn actually has a platform sender (Telegram or
    Discord, never web chat — see PlatformContext.sender_id). Saves what
    THIS person asked to be called; never infer a name from a platform
    display name or guess one — that belongs in sender_display_name, which
    this never touches."""
    ctx = agent.platform_context
    if ctx is None or not ctx.sender_id:
        return {"saved": False, "error": "No platform sender in this context (web chat has none)."}
    name = str(args.get("name") or "").strip()
    if not name:
        return {"saved": False, "error": "name is required"}
    await agent.repo.set_preferred_name(ctx.platform, ctx.sender_id, name)
    return {"saved": True, "name": name}


async def _tool_manage_discord_channel(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    """Only ever offered when app/bots/discord_bot.py already confirmed the
    bot has Manage Channels in this guild — see app/annie/platform.py. If
    this somehow gets called without that (a stale tool list from a prior
    turn, say), it fails honestly rather than pretending to succeed."""
    if agent.platform_context is None or agent.platform_context.create_channel is None:
        return {"created": False, "error": "Not available in this context — Discord channel creation only."}

    name = str(args.get("name") or "").strip()
    purpose = str(args.get("purpose") or "").strip()
    if not name or not purpose:
        return {"created": False, "error": "Both name and purpose are required."}
    category = args.get("category")

    result = await agent.platform_context.create_channel(name=name, purpose=purpose, category=category)
    return result


async def _tool_create_research_task(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    """Queues a bounded, budgeted background task through the same engine
    autonomous research uses (app/research/runner.py) rather than writing
    anything to token/trend data directly — see the module docstring for why
    this is one of the two deliberate exceptions to "tools only ever read"."""
    question = str(args.get("question") or "").strip()
    if not question:
        return {"error": "question is required"}

    from app.db.enums import ResearchTaskOrigin, ResearchTaskStatus
    from app.db.models.research import ResearchTask
    from app.research.runner import run_research_task

    task = ResearchTask(
        question=question,
        reason=str(args.get("reason") or "Requested in chat.").strip(),
        origin=ResearchTaskOrigin.USER,
        status=ResearchTaskStatus.QUEUED,
        priority=0.75,
    )
    created = await agent.repo.create_research_task(task)

    # Fire-and-forget, same pattern as the API route
    # (app/api/routes/intelligence.py's create_task) and the bots' own
    # replies: the user gets an immediate "started" answer, not a wait for a
    # multi-round research loop. The research_task_sweep scheduled job is
    # the safety net if this never gets a chance to run before a restart.
    asyncio.create_task(
        run_research_task(created.id, repo=agent.repo, registry=agent.registry, settings=agent.settings),
        name=f"research_task_{created.id}",
    )

    return {
        "task_id": created.id,
        "question": created.question,
        "status": created.status,
        "note": "Started in the background — check back shortly, or ask again later and I'll pull the finding.",
    }


async def _tool_web_research(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    if not agent.settings.is_available("web_research"):
        return {"error": "Tavily is not configured in this deployment (TAVILY_API_KEY)."}
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    results = await agent.registry.web_research.search(
        query, max_results=min(int(args.get("max_results") or 5), 8)
    )
    return {
        "results": [
            {"title": r.title, "url": r.url, "snippet": r.snippet, "published_at": _iso(r.published_at)}
            for r in results
        ]
    }


_TOOL_HANDLERS = {
    "dashboard_summary": _tool_dashboard_summary,
    "system_status": _tool_system_status,
    "search_memory": _tool_search_memory,
    "read_memory": _tool_read_memory,
    "list_memory": _tool_list_memory,
    "create_memory": _tool_create_memory,
    "remember_instruction": _tool_remember_instruction,
    "write_memory": _tool_write_memory,
    "delete_memory": _tool_delete_memory,
    "search_tokens": _tool_search_tokens,
    "get_token": _tool_get_token,
    "live_token_lookup": _tool_live_token_lookup,
    "list_signals": _tool_list_signals,
    "get_signal": _tool_get_signal,
    "list_creators": _tool_list_creators,
    "get_creator": _tool_get_creator,
    "get_launchpad": _tool_get_launchpad,
    "list_research_notes": _tool_list_research_notes,
    "token_idea": _tool_token_idea,
    "create_research_task": _tool_create_research_task,
    "remember_person": _tool_remember_person,
    "manage_discord_channel": _tool_manage_discord_channel,
    "web_research": _tool_web_research,
}


def _tool_specs(settings: Settings, platform_context: PlatformContext | None = None) -> list[dict[str, Any]]:
    """The tools offered to the model, in the order it should reach for them.

    Memory first, deliberately. Annie's notebook is where her reasoning
    lives; the ledger tools below it hold numbers she has already reasoned
    about. Answering from the numbers when the notebook has a considered view
    is how she ends up reciting statistics instead of saying what she thinks.
    """
    specs = [
        _spec(
            "search_memory",
            "SEARCH ANNIE'S OWN NOTEBOOK — what she has learned about this market, in her "
            "own words. Reach for this first on almost any question about the market, "
            "creators, narratives or what works. Passing a bare contract address or "
            "creator wallet resolves it directly to the file about it. Returns excerpts.",
            {
                "type": "object", "required": ["query"], "additionalProperties": False,
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "A question, a theme, or an exact mint/wallet/ticker.",
                    },
                    "section": {
                        "type": "string",
                        "enum": ["core", "daily", "weekly", "monthly", "creators",
                                 "tokens", "narratives", "playbook", "notes"],
                        "description": "Optional. core = standing beliefs; playbook = what has "
                                       "actually worked; creators/tokens = per-entity files.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 12},
                },
            },
        ),
        _spec(
            "read_memory",
            "Open one memory file in full, by path. Use after search_memory when an "
            "excerpt is clearly the right file and you need the rest of it.",
            {"type": "object", "required": ["path"], "additionalProperties": False,
             "properties": {"path": {"type": "string", "description": "e.g. core/market-model.md"}}},
        ),
        _spec(
            "list_memory",
            "List what memory files exist, with titles and one-line summaries. Use this "
            "when someone refers to a file by description (\"the file about crowded "
            "narratives\") and you need its actual path before writing to it.",
            {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "section": {
                        "type": "string",
                        "enum": ["core", "daily", "weekly", "monthly", "creators",
                                 "tokens", "narratives", "playbook", "notes"],
                        "description": "Omit for everything.",
                    }
                },
            },
        ),
        _spec(
            "create_memory",
            "CREATE A NEW MEMORY FILE when the operator asks for one — \"make me a file "
            "called X\", \"start a note on Y\", \"create a playbook entry for Z\". "
            "Creating it empty is fine and normal: they will usually tell you what goes "
            "in it over the next few messages, so make the file, tell them the path, and "
            "ask what should go in it. Then use write_memory as they dictate.",
            {
                "type": "object", "required": ["name"], "additionalProperties": False,
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "What they called it, in plain words. Becomes the filename.",
                    },
                    "section": {
                        "type": "string",
                        "enum": ["core", "creators", "tokens", "narratives", "playbook", "notes"],
                        "description": "Where it belongs. Default notes. Use playbook for "
                                       "what works, narratives for a theme, core only if they "
                                       "explicitly mean a standing belief.",
                    },
                    "title": {"type": "string", "description": "Human title. Defaults from the name."},
                    "text": {
                        "type": "string",
                        "description": "Starting content, if they already gave you some. "
                                       "Leave empty if they have not said yet.",
                    },
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "keys": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Mints/wallets/tickers this is about, so it is findable later.",
                    },
                    "importance": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        ),
        _spec(
            "write_memory",
            "WRITE INTO A MEMORY FILE. Two uses:\n"
            "(1) The operator is dictating — they told you what to write and where. Any "
            "section is allowed, including core/, because they are entitled to correct "
            "what you believe. Write what they actually said; do not paraphrase their "
            "instruction into your own summary of it unless they asked you to.\n"
            "(2) You noticed something yourself worth keeping. Then use notes/ and "
            "nothing else — a chat turn is not the place to rewrite standing beliefs, and "
            "the scheduled cycle promotes anything durable later.\n"
            "Appends by default. Only set replace=true when they clearly meant to "
            "overwrite (\"rewrite it\", \"replace that with\", \"start it over\") — a "
            "replace destroys the previous content and cannot be undone.",
            {
                "type": "object", "required": ["text"], "additionalProperties": False,
                "properties": {
                    "text": {"type": "string", "description": "What to write."},
                    "path": {
                        "type": "string",
                        "description": "Existing file path, e.g. playbook/what-worked.md. "
                                       "Get it from list_memory or search_memory.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Alternative to path: a plain name, used with section. "
                                       "Creates the file if it does not exist.",
                    },
                    "section": {
                        "type": "string",
                        "enum": ["core", "daily", "weekly", "monthly", "creators",
                                 "tokens", "narratives", "playbook", "notes"],
                    },
                    "replace": {
                        "type": "boolean",
                        "description": "Default false (append). True overwrites everything "
                                       "already in the file.",
                    },
                    "title": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "keys": {"type": "array", "items": {"type": "string"}},
                    "importance": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        ),
        _spec(
            "remember_instruction",
            "RECORD A STANDING INSTRUCTION — something the operator wants you to keep "
            "doing from now on, not a one-off note. Triggers: \"from now on…\", "
            "\"whenever you see X, write it in Y\", \"always include…\", \"stop "
            "bothering with…\", \"keep track of…\". Anything recorded here is loaded "
            "into every future cycle and every conversation, so it actually persists. "
            "Use write_memory instead for a fact or observation; use this for a rule "
            "about how you should behave.",
            {
                "type": "object", "required": ["instruction"], "additionalProperties": False,
                "properties": {
                    "instruction": {
                        "type": "string",
                        "description": "The instruction in their words, phrased so it still "
                                       "makes sense read cold in three weeks.",
                    },
                    "applies_to": {
                        "type": "string",
                        "description": "Optional: the file, section or situation it applies to, "
                                       "e.g. 'playbook/what-worked.md' or 'the morning brief'.",
                    },
                },
            },
        ),
        _spec(
            "delete_memory",
            "Delete a memory file, when explicitly told to forget something. Being told "
            "\"that was wrong, drop it\" is a real correction and worth acting on. Never "
            "delete on your own initiative. The four seeded core/ files cannot be deleted; "
            "replace their contents instead.",
            {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "path": {"type": "string", "description": "e.g. notes/old-idea.md"},
                    "name": {"type": "string"},
                    "section": {"type": "string"},
                },
            },
        ),
        _spec(
            "dashboard_summary",
            "Where things stand overall: launches seen, what cleared a tier, creators "
            "tracked, memory size. When there is no data it also tells you WHY — use "
            "that, do not just report the zeros. Free to call.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        ),
        _spec(
            "system_status",
            "Is the pipeline actually working? Call this when someone asks why you "
            "have nothing, why nothing is updating, or whether something is broken. "
            "Distinguishes a webhook that is not delivering from a deployment that is "
            "simply new — the raw counts cannot tell those apart.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        ),
        _spec(
            "search_tokens",
            "Tokens that actually moved, from the ledger. Note this does NOT cover the "
            "thousands of launches a day that never traded — those are seen, then "
            "forgotten within 48 hours. For a specific mint that is not here, use "
            "live_token_lookup.",
            {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "hours": {"type": "integer", "minimum": 1, "maximum": 720,
                              "description": "Window to look back over. Default 24."},
                    "qualified_only": {"type": "boolean", "description": "Only ones that cleared a tier."},
                    "min_market_cap_usd": {"type": "number"},
                    "launchpad_slug": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                },
            },
        ),
        _spec(
            "get_token",
            "Everything held about one mint: its ledger row, its memory file if it earned "
            "one, and anywhere else it is mentioned. The memory file is the interesting "
            "part — it has the reasoning, not just the numbers.",
            {"type": "object", "required": ["mint"], "additionalProperties": False,
             "properties": {"mint": {"type": "string"}}},
        ),
        _spec(
            "live_token_lookup",
            "Live, right-now market data (price, market cap, liquidity) for ANY mint, "
            "whether or not Annie has ever seen it. Use whenever someone drops a CA and "
            "asks what it is worth now, or asks about a token get_token does not have.",
            {"type": "object", "required": ["mint"], "additionalProperties": False,
             "properties": {"mint": {"type": "string"}}},
        ),
        _spec(
            "list_creators",
            "Creator wallets — by lifetime record, or by who is launching most right now "
            "(pass window_hours). Every launch by every wallet is recorded, so these "
            "counts are complete, not a sample.",
            {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "window_hours": {"type": "integer", "minimum": 1, "maximum": 720,
                                     "description": "Omit for lifetime totals."},
                    "tracked_only": {"type": "boolean",
                                     "description": "Only wallets Annie has decided to follow."},
                    "winners_only": {"type": "boolean",
                                     "description": "Only wallets with a token that cleared a tier."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                },
            },
        ),
        _spec(
            "get_creator",
            "One wallet in full: totals, its tokens, recent movements, and its dossier. "
            "Quote the dossier over restating the numbers — the reason a wallet is "
            "interesting is usually in the prose.",
            {"type": "object", "required": ["wallet"], "additionalProperties": False,
             "properties": {"wallet": {"type": "string"}}},
        ),
        _spec(
            "list_signals",
            "Characteristics statistically over-represented among tokens that cleared a "
            "tier — themes, name shapes, ticker shapes, launchpads. Defaults to those "
            "with enough sample to mean something.",
            {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "status": {"type": "string",
                               "enum": ["new", "rising", "stable", "declining", "dead"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                    "include_low_confidence": {
                        "type": "boolean",
                        "description": "Default false. True includes cohorts too small for "
                        "significance — label anything cited from those as speculation or "
                        "hypothesis, never fact.",
                    },
                },
            },
        ),
        _spec(
            "get_signal",
            "Full detail and daily series for one signal by slug (get it from list_signals).",
            {"type": "object", "required": ["slug"], "additionalProperties": False,
             "properties": {"slug": {"type": "string"}}},
        ),
        _spec(
            "token_idea",
            "Generate launch ideas grounded in what is winning right now and what memory "
            "says has worked. Use when asked what to launch, what is working, or for an "
            "angle. Costs a model call of its own — do not use it for a question "
            "search_memory already answers.",
            {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "brief": {"type": "string",
                              "description": "Optional steer, e.g. 'something in the AI space'."},
                    "count": {"type": "integer", "minimum": 1, "maximum": 4},
                },
            },
        ),
        _spec(
            "get_launchpad", "Detail for one launchpad by slug.",
            {"type": "object", "required": ["slug"], "additionalProperties": False,
             "properties": {"slug": {"type": "string"}}},
        ),
        _spec(
            "list_research_notes",
            "Prior formal research findings — distinct from the notebook above, which is "
            "Annie's ongoing thinking. Check before commissioning something new.",
            {"type": "object", "additionalProperties": False,
             "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}}},
        ),
        _spec(
            "create_research_task",
            "Start a real, multi-round background investigation — the same engine "
            "autonomous research uses — instead of answering from what the tools above "
            "already show. Use when someone explicitly asks you to look into, dig into or "
            "investigate something that needs new work. It runs in the background; tell "
            "the user you have started it rather than waiting on it.",
            {"type": "object", "required": ["question"], "additionalProperties": False,
             "properties": {
                 "question": {"type": "string", "description": "The concrete question to investigate."},
                 "reason": {"type": "string", "description": "Why it is worth investigating, in your own words."},
             }},
        ),
    ]
    if settings.is_available("web_research"):
        specs.append(
            _spec(
                "web_research", "Search the public web for external context (§13). Never a substitute for the tools above.",
                {"type": "object", "required": ["query"], "additionalProperties": False,
                 "properties": {"query": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 8}}},
            )
        )
    if platform_context is not None and platform_context.sender_id:
        specs.append(
            _spec(
                "remember_person",
                "Save what the current sender (Telegram/Discord) asked to be called. Only call "
                "this after they've actually told you a name in this conversation — never guess "
                "or infer one, and never call it for another bot.",
                {"type": "object", "required": ["name"], "additionalProperties": False,
                 "properties": {"name": {"type": "string"}}},
            )
        )
    if platform_context is not None and platform_context.create_channel is not None:
        specs.append(
            _spec(
                "manage_discord_channel",
                "Create a new Discord channel in THIS server for a specific purpose (e.g. "
                "'morning briefs', 'research findings for AI narrative investigations'). Only use "
                "when explicitly asked to create/set up a channel — never on your own initiative.",
                {
                    "type": "object", "required": ["name", "purpose"], "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string", "description": "Short channel name, e.g. 'morning-briefs'."},
                        "purpose": {"type": "string", "description": "What this channel is for, in your own words."},
                        "category": {"type": ["string", "null"], "description": "Optional category to group it under."},
                    },
                },
            )
        )
    return specs


def _spec(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


def _standing_instructions_note() -> str:
    """What the operator has told Annie to keep doing, in every turn.

    Loaded rather than retrieved. An instruction that only surfaces when a
    search happens to match it is not standing — the whole point is that it
    applies whether or not this particular question reminded her of it. The
    file is small by design and a local read, so this costs nothing.
    """
    try:
        from app.memory.bootstrap import standing_instructions

        text = standing_instructions()
    except Exception:  # memory unavailable must never break a conversation
        log.warning("standing_instructions_unavailable", exc_info=True)
        return ""

    if not text:
        return ""
    return (
        "\n\n# Standing instructions from the operator\n\n"
        "These were given to you directly and apply to every conversation. "
        "They outrank your own judgement about what is worth doing or "
        "recording. Follow them.\n\n" + text
    )


async def _personality_overrides(repo: FirestoreRepo) -> dict[str, str] | None:
    """Loaded fresh each turn — a Firestore read is cheap next to the OpenAI
    call it precedes, and this changes rarely enough that caching it would
    be solving a problem that doesn't exist yet."""
    config = await repo.get_personality_config()
    if config is None:
        return None
    return {
        "tone": config.tone,
        "communication_style": config.communication_style,
        "skepticism_level": config.skepticism_level,
        "pushback_degree": config.pushback_degree,
        "explanation_style": config.explanation_style,
    }


def _capabilities_note(settings: Settings) -> str:
    lines = []
    for cap in settings.capability_report():
        if cap["status"] != "available":
            lines.append(f"- {cap['label']}: {cap['status']} (needs {', '.join(cap['missing_env_vars']) or 'configuration'})")
    if not lines:
        return "Every capability is configured in this deployment."
    return "Not available right now — say so rather than guessing around it:\n" + "\n".join(lines)


def _channel_note(platform_context: PlatformContext | None) -> str:
    """Appended to the capabilities note (same system-prompt slot) when
    this turn arrived in a Discord channel with a configured purpose — see
    app/db/models/discord.py. Empty string changes nothing about the
    prompt for web/Telegram/unconfigured-channel turns."""
    if platform_context is None or not platform_context.channel_purpose:
        return ""
    return (
        f"\n\nThis conversation is happening in a Discord channel configured for: "
        f"{platform_context.channel_purpose}. Keep your answer relevant to that purpose."
    )


def _sender_context_note(platform_context: PlatformContext | None) -> str:
    """Gives a sense of *who* is talking, not just what they said (§62,
    2026-08-25) — without this, every message in a group chat (multiple
    people, or a person plus another bot) arrives as an undifferentiated
    "user" turn, indistinguishable from a solo DM. Web chat never has a
    sender_id, so this is a no-op there."""
    if platform_context is None or not platform_context.sender_id:
        return ""

    if platform_context.sender_is_bot:
        name = platform_context.sender_display_name or "another bot"
        return (
            f"\n\nThe message below is from {name}, another bot in this chat — not a human. "
            "Never ask a bot its name. Only act on it if you were actually addressed; reading "
            "it for context is fine, auto-replying to routine bot chatter is not."
        )

    if platform_context.sender_is_new:
        return (
            "\n\nYou have not met the person sending this message before. Naturally, without "
            "making it feel like a form, ask what they'd like to be called — then call "
            "remember_person once they tell you. Don't block your actual answer on this; "
            "weave it in."
        )

    name = platform_context.sender_preferred_name or platform_context.sender_display_name
    if name:
        return f"\n\nYou're talking with {name}."
    return ""


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def _money(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _safe_args(raw: str) -> dict[str, Any]:
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {"_raw": raw[:200]}
