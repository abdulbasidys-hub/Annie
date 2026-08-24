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
The tools only ever *read* — nothing Annie can call writes to the database,
so a model that misuses a tool can produce a wrong answer, never wrong data.

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
        personality_overrides = await _personality_overrides(self.repo)
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": persona.system_prompt(
                    capabilities_note=capabilities_note + channel_note,
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
        """The structured finishing call — see module docstring."""
        prompt_messages = messages + [
            {
                "role": "user",
                "content": (
                    "Restate your answer as the required JSON object. Do not add new "
                    "claims not already grounded in the tool results above — if you did "
                    "not check something, its claim_type must reflect that "
                    "(hypothesis/speculation), not fact."
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
    _, total = await agent.repo.list_tokens(limit=1)
    _, qualified = await agent.repo.list_tokens(qualified_only=True, limit=1)
    trends = await agent.repo.all_trends()
    return {
        "tokens_collected": total,
        "tokens_qualified": qualified,
        "trends_active": sum(1 for t in trends if t.status != "dead"),
        "trends_rising": sum(1 for t in trends if t.status == "rising"),
        "trends_new": sum(1 for t in trends if t.status == "new"),
        "trends_declining": sum(1 for t in trends if t.status == "declining"),
    }


async def _tool_search_tokens(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    limit = min(int(args.get("limit") or 10), 25)
    tokens, total = await agent.repo.list_tokens(
        qualified_only=bool(args.get("qualified_only", True)),
        launchpad_slug=args.get("launchpad_slug"),
        limit=limit,
    )
    min_tier = args.get("min_tier_usd")
    if min_tier:
        floor = Decimal(str(min_tier))
        tokens = [t for t in tokens if t.peak_market_cap is not None and t.peak_market_cap >= floor]
    return {
        "total_matching": total,
        "tokens": [
            {
                "mint": t.mint, "name": t.name, "symbol": t.symbol,
                "qualified_at": _iso(t.qualified_at),
                "qualified_market_cap": _money(t.qualified_market_cap),
                "peak_market_cap": _money(t.peak_market_cap),
                "launchpad_slug": t.launchpad_slug, "creator_wallet": t.creator_wallet,
            }
            for t in tokens
        ],
    }


async def _tool_get_token(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    mint = str(args.get("mint") or "").strip()
    token = await agent.repo.get_token(mint) if mint else None
    if token is None:
        return {"found": False}
    features = await agent.repo.token_features(mint)
    milestones = await agent.repo.token_milestones(mint)
    return {
        "found": True,
        "mint": token.mint, "name": token.name, "symbol": token.symbol,
        "description": token.description, "launchpad_slug": token.launchpad_slug,
        "creator_wallet": token.creator_wallet, "launched_at": _iso(token.launched_at),
        "is_qualified": token.is_qualified, "qualified_at": _iso(token.qualified_at),
        "qualified_market_cap": _money(token.qualified_market_cap),
        "peak_market_cap": _money(token.peak_market_cap),
        "verification_status": token.verification_status,
        "qualification_evidence": token.qualification_evidence,
        "themes": [f.value for f in features if f.namespace == "token" and f.key == "theme"],
        "milestones": [
            {"kind": m.kind, "threshold_usd": _money(m.threshold_usd), "reached_at": _iso(m.reached_at)}
            for m in milestones
        ],
    }


async def _tool_list_trends(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    limit = min(int(args.get("limit") or 10), 25)
    status = args.get("status")
    trends, _ = await agent.repo.list_trends(status=status, limit=10000)
    trends.sort(key=lambda t: abs(t.change or 0), reverse=True)
    return {
        "trends": [
            {
                "slug": t.slug, "name": t.name, "category": t.category, "status": t.status,
                "maturity": t.maturity, "confidence": t.confidence,
                "cohort_threshold_usd": _money(t.cohort_threshold_usd),
                "recent_count": t.recent_count, "recent_total": t.recent_total,
                "recent_frequency": t.recent_frequency, "baseline_frequency": t.baseline_frequency,
                "change": t.change, "persistence_days": t.persistence_days,
            }
            for t in trends[:limit]
        ]
    }


async def _tool_get_trend(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    slug = str(args.get("slug") or "").strip()
    trend = await agent.repo.get_trend(slug) if slug else None
    if trend is None:
        return {"found": False}
    evidence = trend.evidence or {}
    return {
        "found": True,
        "slug": trend.slug, "name": trend.name, "description": trend.description,
        "status": trend.status, "maturity": trend.maturity, "confidence": trend.confidence,
        "cohort_threshold_usd": _money(trend.cohort_threshold_usd),
        "recent_count": trend.recent_count, "recent_total": trend.recent_total,
        "recent_frequency": trend.recent_frequency, "baseline_frequency": trend.baseline_frequency,
        "change": trend.change, "p_value": trend.p_value, "effect_size": trend.effect_size,
        "persistence_days": trend.persistence_days, "caveats": evidence.get("caveats", []),
        "first_detected_at": _iso(trend.first_detected_at),
    }


async def _tool_list_creators(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    limit = min(int(args.get("limit") or 10), 25)
    creators, _ = await agent.repo.list_creators(limit=limit)
    if args.get("repeat_winners_only"):
        creators = [c for c in creators if c.is_repeat_winner]
    return {
        "creators": [
            {
                "wallet": c.wallet, "total_launches": c.total_launches,
                "wins_100k": c.wins_100k, "wins_1m": c.wins_1m,
                "success_rate": c.success_rate, "is_repeat_winner": c.is_repeat_winner,
            }
            for c in creators
        ]
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


async def _tool_search_memories(agent: AnnieAgent, args: dict[str, Any]) -> dict[str, Any]:
    """Annie's durable work memory — not conversation history, not research
    findings (use list_research_notes for those). "Store extensively,
    retrieve selectively": this is called on demand, never pre-loaded into
    every turn, same discipline as every other tool here."""
    limit = min(int(args.get("limit") or 5), 15)
    type_ = args.get("type")
    memories = await agent.repo.active_memories_by_importance(type_=type_, limit=limit)
    for m in memories:
        await agent.repo.touch_memory_used(m.id)
    return {
        "memories": [
            {
                "id": m.id, "type": m.type, "title": m.title, "content": m.content,
                "confidence": m.confidence, "importance": m.importance, "tags": m.tags,
                "created_at": _iso(m.created_at),
            }
            for m in memories
        ]
    }


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
    "search_tokens": _tool_search_tokens,
    "get_token": _tool_get_token,
    "live_token_lookup": _tool_live_token_lookup,
    "list_trends": _tool_list_trends,
    "get_trend": _tool_get_trend,
    "list_creators": _tool_list_creators,
    "get_launchpad": _tool_get_launchpad,
    "list_research_notes": _tool_list_research_notes,
    "search_memories": _tool_search_memories,
    "manage_discord_channel": _tool_manage_discord_channel,
    "web_research": _tool_web_research,
}


def _tool_specs(settings: Settings, platform_context: PlatformContext | None = None) -> list[dict[str, Any]]:
    specs = [
        _spec(
            "dashboard_summary", "Overall counts: tokens collected/qualified, active trend counts.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        ),
        _spec(
            "search_tokens", "Search/list tokens with optional filters.",
            {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "qualified_only": {"type": "boolean", "description": "Default true."},
                    "min_tier_usd": {"type": "string", "description": "e.g. '1000000' for $1M+."},
                    "launchpad_slug": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                },
            },
        ),
        _spec(
            "get_token", "Full detail for one token by mint address, FROM THE RESEARCH DATABASE ONLY "
            "(qualified tokens that migrated and cleared a $100k+ tier). Returns not-found for anything "
            "still pending the daily qualification run — use live_token_lookup for that instead.",
            {"type": "object", "required": ["mint"], "additionalProperties": False,
             "properties": {"mint": {"type": "string"}}},
        ),
        _spec(
            "live_token_lookup", "Live, right-now market data (price, market cap, liquidity) for ANY "
            "mint/contract address, whether or not it's in the research database yet. Use this whenever "
            "someone drops a CA and asks what it's worth, or asks about a token get_token doesn't find.",
            {"type": "object", "required": ["mint"], "additionalProperties": False,
             "properties": {"mint": {"type": "string"}}},
        ),
        _spec(
            "list_trends", "List trends, optionally filtered by status.",
            {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "status": {"type": "string", "enum": ["new", "rising", "stable", "declining", "dead"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                },
            },
        ),
        _spec(
            "get_trend", "Full detail for one trend by slug (get the slug from list_trends first).",
            {"type": "object", "required": ["slug"], "additionalProperties": False,
             "properties": {"slug": {"type": "string"}}},
        ),
        _spec(
            "list_creators", "List creator wallets, optionally filtered to repeat winners.",
            {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "repeat_winners_only": {"type": "boolean"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                },
            },
        ),
        _spec(
            "get_launchpad", "Detail for one launchpad by slug.",
            {"type": "object", "required": ["slug"], "additionalProperties": False,
             "properties": {"slug": {"type": "string"}}},
        ),
        _spec(
            "list_research_notes", "Prior findings from Research Memory (§29) — check before calling something new.",
            {"type": "object", "additionalProperties": False,
             "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}}},
        ),
        _spec(
            "search_memories", "Annie's own accumulated work memory — durable lessons, recurring "
            "observations, and daily activity logs (distinct from research findings above). Check "
            "this for context on patterns you've noticed before or how the ecosystem has behaved.",
            {"type": "object", "additionalProperties": False,
             "properties": {
                 "type": {"type": "string", "enum": ["long_term", "daily_log"], "description": "Omit for both."},
                 "limit": {"type": "integer", "minimum": 1, "maximum": 15},
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
