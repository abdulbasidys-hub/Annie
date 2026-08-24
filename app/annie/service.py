"""Shared entry point for asking Annie something.

Used by the web chat route (`app/api/routes/annie.py`) and both bot
integrations (`app/bots/`) — one place that creates/loads a conversation,
runs the agent, and persists both messages, so behavior can't drift between
"Annie on the web" and "Annie on Telegram" (§62 treats these as the same
assistant on different surfaces, not three).
"""

from __future__ import annotations

from app.annie.platform import PlatformContext
from app.config import Settings
from app.db.models.research import Conversation, Message
from app.db.repo import FirestoreRepo
from app.providers.registry import ProviderRegistry


async def ask_annie(
    *,
    repo: FirestoreRepo,
    registry: ProviderRegistry,
    settings: Settings,
    conversation_id: str | None,
    user_message: str,
    platform_context: PlatformContext | None = None,
) -> tuple[Conversation, Message]:
    """Run one turn. Raises ``CapabilityUnavailable`` if AI isn't configured.

    ``platform_context`` is Discord-only (see app/annie/platform.py) — every
    other caller omits it and nothing about this function's behavior changes.
    """
    settings.require("ai")

    convo = await repo.get_conversation(conversation_id) if conversation_id else None
    if convo is None:
        convo = await repo.create_conversation()

    await repo.add_message(convo.id, Message(role="user", content=user_message))

    from app.annie.agent import AnnieAgent  # imported late: pulls the OpenAI client

    agent = AnnieAgent(repo=repo, registry=registry, settings=settings, platform_context=platform_context)
    reply = await agent.respond(conversation_id=convo.id, user_message=user_message)

    message = await repo.add_message(
        convo.id,
        Message(
            role="annie",
            content=reply.content,
            claim_type=reply.claim_type,
            confidence=reply.confidence,
            citations=reply.citations,
            tool_calls=reply.tool_calls,
            model=reply.model,
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
            cost_usd=reply.cost_usd,
            latency_ms=reply.latency_ms,
        ),
    )
    return convo, message
