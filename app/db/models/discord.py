"""Discord workspace channel configuration.

Firestore layout: ``discord_channels/{channel_id}`` — keyed by Discord's own
channel ID (a natural key, like a token's mint), so a lookup from an
incoming message is always a single ``.get()``, never a query.

This exists so Annie (and the operator) know *why* a channel exists without
relying on its name: ``purpose`` is what routing and prompt context key off
of, not string-matching ``#research-findings``. See
``app/bots/discord_bot.py``'s purpose-aware routing and
``app/annie/agent.py``'s ``manage_discord_channel`` tool, which writes these
records when Annie creates a channel on request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class DiscordChannel:
    channel_id: str = ""
    guild_id: str = ""
    name: str = ""
    purpose: str = ""
    type: str = "text"
    enabled: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None
