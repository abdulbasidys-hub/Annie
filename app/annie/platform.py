"""Optional platform-specific capabilities and context threaded through a chat turn.

``AnnieAgent`` is otherwise 100% platform-agnostic (see
``app/annie/service.py``'s module docstring) — web chat never constructs one
of these, so nothing about it changes. Telegram and Discord both build one
now: originally Discord-only (for channel creation and channel-purpose
context), extended 2026-08-25 to carry *sender identity* on both platforms —
who actually sent this message, so a group chat with several people (or
another bot) reads as distinct speakers instead of one undifferentiated
"user" role, and so a returning person doesn't have to re-introduce
themselves every session. See ``app/annie/agent.py``'s ``remember_person``
tool and ``_sender_context_note`` for the other half of this.

``create_channel`` is ``None`` whenever it structurally cannot succeed —
no guild (a DM), or the bot lacks the Manage Channels permission in this
guild — checked once in ``app/bots/discord_bot.py`` before this object is
built, not discovered by trying and failing. A tool the model can see but
that fails on every possible call is worse than a tool that isn't offered:
see ``app/annie/agent.py``'s ``_tool_specs`` for where this gates the
``manage_discord_channel`` tool's very existence for a turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

#: (name, purpose, category|None) -> {"created": bool, "channel_id": str|None,
#: "name": str|None, "error": str|None}
CreateChannelFn = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(slots=True)
class PlatformContext:
    platform: str
    channel_purpose: str | None = None
    create_channel: CreateChannelFn | None = None

    #: Who actually sent this message — never the web chat user (there is no
    #: platform user ID for a browser session), always set for Telegram/Discord.
    sender_id: str | None = None
    sender_display_name: str | None = None
    sender_preferred_name: str | None = None
    sender_is_bot: bool = False
    sender_is_new: bool = False
