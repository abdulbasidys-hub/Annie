"""Discord bot integration (§62).

Runs as a background asyncio task in the same process as the API — see
`telegram_bot.py`'s module docstring for the trade-off against a separate
service. Responds to direct messages always, and to messages in a server
channel only when @mentioned, so it doesn't answer every message in every
channel it happens to be invited into.

**Access is controlled by an operator-editable allowlist**
(`app/bots/access_control.py`) — same mechanism and same open-by-default
behavior as the Telegram bot; see that module's docstring.

**Requires "Message Content Intent" enabled for this bot in the Discord
Developer Portal** (Application -> Bot page). Without it, `message.content`
arrives as an empty string for every message regardless of what was
actually typed — this is a privileged intent Discord requires you to
opt into per-bot, and it cannot be set from code.
"""

from __future__ import annotations

import discord
import structlog

from app.annie.service import ask_annie
from app.bots.access_control import is_allowed
from app.config import Settings
from app.db.repo import FirestoreRepo
from app.providers.registry import ProviderRegistry

log = structlog.get_logger(__name__)

#: Discord's own limit is 2000 characters; headroom for the same reason as
#: Telegram's MAX_MESSAGE_LENGTH.
MAX_MESSAGE_LENGTH = 1900


def build_discord_client(
    repo: FirestoreRepo, registry: ProviderRegistry, settings: Settings
) -> discord.Client:
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        log.info("discord_bot_started", user=str(client.user))

    @client.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return

        is_dm = isinstance(message.channel, discord.DMChannel)
        mentioned = client.user is not None and client.user in message.mentions
        if not is_dm and not mentioned:
            return

        if not await is_allowed(repo, "discord", message.author.id):
            log.info("discord_message_rejected", sender_id=message.author.id)
            await _send(message.channel, "This bot is restricted right now — you're not on the allowed list.")
            return

        text = message.content
        if mentioned and client.user is not None:
            text = text.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()
        if not text:
            return

        channel_id = str(message.channel.id)
        try:
            async with message.channel.typing():
                conversation_id = await repo.get_bot_session("discord", channel_id)
                convo, reply = await ask_annie(
                    repo=repo,
                    registry=registry,
                    settings=settings,
                    conversation_id=conversation_id,
                    user_message=text,
                )
                await repo.set_bot_session("discord", channel_id, convo.id)
                await _send(message.channel, reply.content)
        except Exception as exc:
            log.warning("discord_handle_failed", channel_id=channel_id, error=str(exc))
            await _send(message.channel, "Something went wrong on my end — try again in a moment.")

    return client


async def _send(channel: "discord.abc.Messageable", text: str) -> None:
    text = text or "(empty reply)"
    for i in range(0, len(text), MAX_MESSAGE_LENGTH):
        await channel.send(text[i : i + MAX_MESSAGE_LENGTH])
