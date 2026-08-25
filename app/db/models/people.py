"""Who Annie is actually talking to, across Telegram/Discord (§62).

Firestore layout: ``platform_users/{platform}_{user_id}`` — keyed by platform
plus the platform's own user ID, so a lookup from an incoming message is
always a single ``.get()``, never a query. Exists so a group chat with
several people (or another bot) reads as several distinct speakers instead
of one undifferentiated "user" role, and so a returning person doesn't have
to re-introduce themselves every session.

``preferred_name`` is what the person told Annie to call them (only Annie
sets it, via the ``remember_person`` tool, once she's actually asked and
gotten an answer — never inferred from a platform display name). Until then
``platform_display_name`` (Discord's display name / Telegram's first name)
is the best available fallback, clearly distinguished so a citation never
implies more familiarity than actually exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class PlatformUser:
    platform: str = ""  # "telegram" | "discord"
    user_id: str = ""
    platform_display_name: str | None = None
    preferred_name: str | None = None
    is_bot: bool = False
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    message_count: int = 0

    @property
    def doc_id(self) -> str:
        return f"{self.platform}_{self.user_id}"

    @property
    def best_name(self) -> str | None:
        return self.preferred_name or self.platform_display_name
