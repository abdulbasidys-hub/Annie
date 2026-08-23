"""Telegram bot integration (§62).

Long-polling against Telegram's `getUpdates`, not a webhook: no public
inbound URL to configure, and it works identically from local dev or any
host. Started as a background asyncio task from `app/main.py`'s lifespan, in
the same process as the API — see README's deploy notes for the trade-off
against running this as a separate service.

**Access is controlled by an operator-editable allowlist**
(`app/bots/access_control.py`), not code — an empty allowlist (the default)
means every Telegram user who finds this bot can chat with Annie, at your
OpenAI cost, same as this system's original design. Add IDs to the
`telegram_allowlist` setting to restrict it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

from app.annie.service import ask_annie
from app.bots.access_control import is_allowed
from app.config import Settings
from app.db.repo import FirestoreRepo
from app.providers.registry import ProviderRegistry

log = structlog.get_logger(__name__)

API_BASE = "https://api.telegram.org"
POLL_TIMEOUT_S = 30
#: Telegram's own limit is 4096 characters; left some headroom rather than
#: cutting it exactly at the wire, so this survives a future prompt tweak
#: that adds a few characters of framing around the reply.
MAX_MESSAGE_LENGTH = 4000


class TelegramBot:
    def __init__(
        self, token: str, repo: FirestoreRepo, registry: ProviderRegistry, settings: Settings
    ) -> None:
        self._repo = repo
        self._registry = registry
        self._settings = settings
        self._stopped = False
        self._client = httpx.AsyncClient(
            base_url=f"{API_BASE}/bot{token}",
            timeout=httpx.Timeout(POLL_TIMEOUT_S + 10),
        )

    async def run(self) -> None:
        """The poll loop. Runs until :meth:`stop` is called."""
        offset = await self._repo.get_bot_offset("telegram")
        log.info("telegram_bot_started")

        while not self._stopped:
            try:
                response = await self._client.get(
                    "/getUpdates",
                    params={"timeout": POLL_TIMEOUT_S, "offset": offset, "allowed_updates": ["message"]},
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                # A network blip here must not kill the bot for the rest of
                # the process's life — back off and keep polling.
                log.warning("telegram_poll_failed", error=str(exc))
                await asyncio.sleep(5)
                continue

            for update in payload.get("result", []):
                offset = update["update_id"] + 1
                await self._repo.set_bot_offset("telegram", offset)

                message = update.get("message")
                if not message or "text" not in message:
                    continue
                # Fire-and-forget: one slow tool call in Annie's agent loop
                # must not stall the poll loop and delay every other chat.
                asyncio.create_task(self._handle(message))

    async def _handle(self, message: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        text = message["text"]
        sender_id = (message.get("from") or {}).get("id")

        if not await is_allowed(self._repo, "telegram", sender_id):
            log.info("telegram_message_rejected", sender_id=sender_id)
            await self._send(chat_id, "This bot is restricted right now — you're not on the allowed list.")
            return

        try:
            await self._client.post(
                "/sendChatAction", json={"chat_id": chat_id, "action": "typing"}
            )
            conversation_id = await self._repo.get_bot_session("telegram", str(chat_id))
            convo, reply = await ask_annie(
                repo=self._repo,
                registry=self._registry,
                settings=self._settings,
                conversation_id=conversation_id,
                user_message=text,
            )
            await self._repo.set_bot_session("telegram", str(chat_id), convo.id)
            await self._send(chat_id, reply.content)
        except Exception as exc:
            log.warning("telegram_handle_failed", chat_id=chat_id, error=str(exc))
            await self._send(chat_id, "Something went wrong on my end — try again in a moment.")

    async def _send(self, chat_id: int, text: str) -> None:
        text = text or "(empty reply)"
        for i in range(0, len(text), MAX_MESSAGE_LENGTH):
            chunk = text[i : i + MAX_MESSAGE_LENGTH]
            try:
                await self._client.post("/sendMessage", json={"chat_id": chat_id, "text": chunk})
            except httpx.HTTPError as exc:
                log.warning("telegram_send_failed", chat_id=chat_id, error=str(exc))

    async def stop(self) -> None:
        self._stopped = True
        await self._client.aclose()
