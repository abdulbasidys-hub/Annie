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
`telegram_allowlist` setting to restrict it. Not applied to another bot's
messages (see below) — there is no separate bot allowlist yet, and a bot
already has to be deliberately added to the same group/DM to reach Annie
at all.

**Responds in DMs always, and in a group only when actually addressed** —
mentioned by @username, or replying to one of Annie's own messages — same
gate Discord uses, so she doesn't answer every message in every group she's
added to. This also covers another bot's messages (§62, 2026-08-25: "I want
her to be able to read the other bot's messages and maybe respond when I
wants her to") — her own messages are the only ones unconditionally ignored.

**Group Privacy mode matters here and code cannot change it.** By default a
new Telegram bot only receives group messages that start with "/" or reply
to its own messages — everything else, including another bot's ambient
chatter, never reaches `getUpdates` at all regardless of what this file
does. To let Annie actually see a group's full conversation (so she can be
asked "what did that bot just post"), disable Group Privacy for this bot via
@BotFather: `/mybots` -> select the bot -> Bot Settings -> Group Privacy ->
Turn off.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import httpx
import structlog

from app.annie.platform import PlatformContext
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
#: A session quiet longer than this starts fresh automatically (§62,
#: 2026-08-25 — "even when the conversation has shifted it repeats it").
#: /new (below) is the reliable complement for "right now", not "probably
#: enough time has passed".
SESSION_MAX_AGE = timedelta(hours=3)


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
        self._bot_id: int | None = None
        self._bot_username: str | None = None

    async def run(self) -> None:
        """The poll loop. Runs until :meth:`stop` is called."""
        await self._identify_self()
        offset = await self._repo.get_bot_offset("telegram")
        log.info("telegram_bot_started", bot_id=self._bot_id, username=self._bot_username)

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
                sender = message.get("from") or {}
                if self._bot_id is not None and sender.get("id") == self._bot_id:
                    continue  # self-loop guard, same as Discord's own-message check
                # Fire-and-forget: one slow tool call in Annie's agent loop
                # must not stall the poll loop and delay every other chat.
                asyncio.create_task(self._handle(message))

    async def _identify_self(self) -> None:
        try:
            response = await self._client.get("/getMe")
            response.raise_for_status()
            result = response.json().get("result") or {}
            self._bot_id = result.get("id")
            self._bot_username = result.get("username")
        except httpx.HTTPError as exc:
            log.warning("telegram_get_me_failed", error=str(exc))

    def _addressed(self, message: dict[str, Any]) -> tuple[bool, str]:
        """Whether this message actually addresses Annie, and the text with
        any @mention stripped. DMs always count; a group message counts only
        if it @mentions her by username or replies to one of her own
        messages — mirrors Discord's DM-or-mentioned gate."""
        chat_type = (message.get("chat") or {}).get("type")
        text = message.get("text") or ""
        if chat_type == "private":
            return True, text

        reply_to = message.get("reply_to_message") or {}
        if self._bot_id is not None and (reply_to.get("from") or {}).get("id") == self._bot_id:
            return True, text

        if self._bot_username and f"@{self._bot_username}" in text:
            return True, text.replace(f"@{self._bot_username}", "").strip()

        return False, text

    async def _handle(self, message: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        sender = message.get("from") or {}
        sender_id = sender.get("id")
        sender_is_bot = bool(sender.get("is_bot"))

        addressed, text = self._addressed(message)
        if not addressed or not text:
            return

        if not sender_is_bot and not await is_allowed(self._repo, "telegram", sender_id):
            log.info("telegram_message_rejected", sender_id=sender_id)
            await self._send(chat_id, "This bot is restricted right now — you're not on the allowed list.")
            return

        if text.strip().lower() in ("/new", "/reset"):
            await self._repo.clear_bot_session("telegram", str(chat_id))
            await self._send(chat_id, "Starting fresh — no memory of what we were just talking about.")
            return

        try:
            await self._client.post(
                "/sendChatAction", json={"chat_id": chat_id, "action": "typing"}
            )
            display_name = sender.get("first_name") or sender.get("username")
            existing = await self._repo.get_platform_user("telegram", str(sender_id)) if sender_id else None
            is_new = existing is None and not sender_is_bot
            profile = None
            if sender_id:
                profile = await self._repo.touch_platform_user(
                    "telegram", str(sender_id), display_name=display_name, is_bot=sender_is_bot
                )
            platform_context = PlatformContext(
                platform="telegram",
                sender_id=str(sender_id) if sender_id else None,
                sender_display_name=profile.platform_display_name if profile else display_name,
                sender_preferred_name=profile.preferred_name if profile else None,
                sender_is_bot=sender_is_bot,
                sender_is_new=is_new,
            )

            conversation_id = await self._repo.get_bot_session(
                "telegram", str(chat_id), max_age=SESSION_MAX_AGE
            )
            convo, reply = await ask_annie(
                repo=self._repo,
                registry=self._registry,
                settings=self._settings,
                conversation_id=conversation_id,
                user_message=text,
                platform_context=platform_context,
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
