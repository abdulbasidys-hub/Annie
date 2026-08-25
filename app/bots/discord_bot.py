"""Discord bot integration (§62) and workspace (channel management).

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

**Workspace channels**: a message in a channel configured with a purpose
(`app/db/models/discord.py`'s `DiscordChannel`, created via the
`manage_discord_channel` agent tool or manually) gets that purpose folded
into Annie's context for the turn (`app/annie/platform.py`). The bot only
ever *offers* Annie the ability to create a channel when it has actually
confirmed the "Manage Channels" permission in that guild first — never by
trying and catching a permission error, per this codebase's "don't assume
permissions the bot doesn't have" rule.
"""

from __future__ import annotations

from typing import Any

import discord
import structlog

from app.annie.platform import PlatformContext
from app.annie.service import ask_annie
from app.bots.access_control import is_allowed
from app.config import Settings
from app.db.base import slugify
from app.db.models.discord import DiscordChannel
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
                platform_context = await _build_platform_context(repo, message)
                convo, reply = await ask_annie(
                    repo=repo,
                    registry=registry,
                    settings=settings,
                    conversation_id=conversation_id,
                    user_message=text,
                    platform_context=platform_context,
                )
                await repo.set_bot_session("discord", channel_id, convo.id)
                await _send(message.channel, reply.content)
        except Exception as exc:
            log.warning("discord_handle_failed", channel_id=channel_id, error=str(exc))
            await _send(message.channel, "Something went wrong on my end — try again in a moment.")

    return client


async def _build_platform_context(repo: FirestoreRepo, message: "discord.Message") -> PlatformContext:
    """Look up this channel's configured purpose (if any), and decide
    whether Annie may be offered channel-creation this turn — gated on the
    bot actually holding Manage Channels in this specific guild, checked
    now rather than assumed."""
    configured = await repo.get_discord_channel(str(message.channel.id))
    channel_purpose = configured.purpose if configured and configured.enabled else None

    create_channel = None
    guild = message.guild
    if guild is not None and guild.me is not None and guild.me.guild_permissions.manage_channels:
        async def _create(*, name: str, purpose: str, category: str | None = None) -> dict[str, Any]:
            return await _create_channel(guild, repo, name=name, purpose=purpose, category=category)

        create_channel = _create

    return PlatformContext(platform="discord", channel_purpose=channel_purpose, create_channel=create_channel)


async def _create_channel(
    guild: "discord.Guild", repo: FirestoreRepo, *, name: str, purpose: str, category: str | None
) -> dict[str, Any]:
    """Idempotency guard, not just a nice-to-have: two independent processes
    can both hold a live Discord Gateway connection during a rolling
    redeploy (Discord, unlike Telegram, does not reject a second connection
    with the same bot token), so the same "create this channel" request can
    genuinely arrive twice — confirmed as the likely cause of a real
    incident (2026-08-25) where one request produced two channels with
    different IDs. `app/main.py`'s shutdown sequence now closes the old
    Gateway session explicitly to shrink that window, but a name check here
    is what actually stops it from producing a duplicate channel even if the
    window isn't fully closed.
    """
    channel_name = slugify(name, max_length=90) or "annie-channel"
    existing = discord.utils.get(guild.text_channels, name=channel_name)
    if existing is not None:
        log.info("discord_channel_already_exists", guild_id=guild.id, channel_id=existing.id, name=channel_name)
        return {"created": False, "channel_id": str(existing.id), "name": existing.name, "error": "Already exists."}
    try:
        category_obj = None
        if category:
            category_obj = discord.utils.get(guild.categories, name=category)
            if category_obj is None:
                category_obj = await guild.create_category(category)
        channel = await guild.create_text_channel(channel_name, category=category_obj, topic=purpose[:1024])
    except discord.Forbidden:
        log.warning("discord_channel_create_forbidden", guild_id=guild.id, name=channel_name)
        return {"created": False, "error": "Missing the Manage Channels permission in this server."}
    except discord.HTTPException as exc:
        log.warning("discord_channel_create_failed", guild_id=guild.id, name=channel_name, error=str(exc))
        return {"created": False, "error": f"Discord rejected the request: {exc}"}

    await repo.create_discord_channel(
        DiscordChannel(
            channel_id=str(channel.id), guild_id=str(guild.id), name=channel.name,
            purpose=purpose, type="text", enabled=True,
        )
    )
    log.info("discord_channel_created", guild_id=guild.id, channel_id=channel.id, name=channel.name)
    return {"created": True, "channel_id": str(channel.id), "name": channel.name, "error": None}


async def _send(channel: "discord.abc.Messageable", text: str) -> None:
    text = text or "(empty reply)"
    for i in range(0, len(text), MAX_MESSAGE_LENGTH):
        await channel.send(text[i : i + MAX_MESSAGE_LENGTH])


async def send_channel_message(bot_token: str, channel_id: str, text: str) -> bool:
    """Send a message to a channel by ID over Discord's REST API, without
    needing the live Gateway `discord.Client` — used by
    `app/scheduling/jobs.py`'s Morning Brief job, which runs as its own
    background task, not inside the bot's event loop. Same approach the
    Telegram bot already uses (plain HTTP, no SDK needed for a one-shot
    send). Returns False rather than raising — a failed delivery should
    not crash the scheduler."""
    import httpx

    text = text or "(empty)"
    ok = True
    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        headers={"Authorization": f"Bot {bot_token}"},
        timeout=15,
    ) as client:
        for i in range(0, len(text), MAX_MESSAGE_LENGTH):
            chunk = text[i : i + MAX_MESSAGE_LENGTH]
            try:
                response = await client.post(f"/channels/{channel_id}/messages", json={"content": chunk})
                response.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("discord_rest_send_failed", channel_id=channel_id, error=str(exc))
                ok = False
    return ok
