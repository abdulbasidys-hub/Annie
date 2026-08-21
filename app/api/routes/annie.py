"""Annie's chat routes (§62).

Thin HTTP layer: conversation persistence and capability guarding live here,
the actual reasoning lives in :mod:`app.annie.agent`. The capability guard is
what matters most — without ``OPENAI_API_KEY`` this returns a 503 naming the
variable rather than a generic failure or, worse, a canned reply that reads
like Annie answering.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.schemas import ChatRequest, ConversationDetail, ConversationSummary, Page
from app.config import Settings, get_settings
from app.db.models.research import Message
from app.db.repo import FirestoreRepo, get_repo
from app.providers.registry import ProviderRegistry, get_registry

router = APIRouter()


@router.get("/conversations", response_model=Page[ConversationSummary])
async def list_conversations(repo: FirestoreRepo = Depends(get_repo)) -> dict[str, Any]:
    rows = await repo.list_conversations(limit=50)
    items = [
        {
            "id": c.id, "title": c.title, "message_count": c.message_count,
            "last_message_at": c.last_message_at, "created_at": c.created_at,
        }
        for c in rows
    ]
    return {"items": items, "total": len(items), "limit": 50, "offset": 0}


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: str, repo: FirestoreRepo = Depends(get_repo)
) -> dict[str, Any]:
    convo = await repo.get_conversation(conversation_id)
    if convo is None:
        raise HTTPException(status_code=404, detail=f"No conversation {conversation_id}")

    messages = await repo.list_messages(conversation_id)
    return {
        "id": convo.id, "title": convo.title, "message_count": convo.message_count,
        "last_message_at": convo.last_message_at, "created_at": convo.created_at,
        "messages": [_message_out(m) for m in messages],
    }


@router.post("/chat")
async def chat(
    body: ChatRequest,
    repo: FirestoreRepo = Depends(get_repo),
    registry: ProviderRegistry = Depends(get_registry),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Send a message to Annie.

    Raises ``CapabilityUnavailable`` when AI is not configured; the handler in
    :mod:`app.main` renders that as a 503 naming ``OPENAI_API_KEY``. The
    actual work is shared with the Telegram/Discord bots — see
    :mod:`app.annie.service`.
    """
    from app.annie.service import ask_annie

    convo, message = await ask_annie(
        repo=repo,
        registry=registry,
        settings=settings,
        conversation_id=body.conversation_id,
        user_message=body.message,
    )
    return {"conversation_id": convo.id, "message": _message_out(message)}


def _message_out(m: Message) -> dict[str, Any]:
    return {
        "id": m.id, "role": m.role, "content": m.content, "claim_type": m.claim_type,
        "confidence": m.confidence, "citations": m.citations, "tool_calls": m.tool_calls,
        "created_at": m.created_at, "model": m.model, "cost_usd": m.cost_usd,
        "latency_ms": m.latency_ms,
    }
