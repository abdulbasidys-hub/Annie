"""Optional platform-specific capabilities threaded through a chat turn.

``AnnieAgent`` is otherwise 100% platform-agnostic (see
``app/annie/service.py``'s module docstring) — web chat and Telegram never
construct one of these, so nothing about them changes. Discord is the one
surface that needs to hand the agent a *real* action (create a channel) and
some *context* (which channel's purpose this message arrived in), so this
is a narrow, optional extension point rather than a change to the agent's
core shape.

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
