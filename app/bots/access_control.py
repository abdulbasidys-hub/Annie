"""Operator-editable allowlist for who can chat with Annie on Telegram/Discord.

Firestore-backed (via the existing `Setting` mechanism — keys
`telegram_allowlist`/`discord_allowlist`, each a JSON array of user-ID
strings), not an env var — consistent with this system's "manage everything
from the website" pattern (Settings page, System Health's pipeline buttons):
changing who's allowed to talk to Annie shouldn't need a redeploy.

**An empty allowlist means open to everyone** — the same "no access
restriction" default this system always had (see the bot modules' original
docstrings). Access control only starts restricting once the operator adds
the first ID. This is deliberate: turning the feature on must never be able
to silently lock the operator out of their own bot before they've added
themselves to the list.

A short in-process cache avoids a Firestore read on every single incoming
message. Changing the allowlist takes up to CACHE_TTL_SECONDS to take
effect, not instantly — an acceptable trade for an infrequent operator
action that isn't on a latency-sensitive path.
"""

from __future__ import annotations

import time

from app.db.repo import FirestoreRepo

CACHE_TTL_SECONDS = 60

#: provider -> (cached_at_monotonic, allowlist). Module-level on purpose: both
#: bots share one process (app/main.py's `_start_bots`) so one cache per
#: provider is correct, not one per bot instance.
_cache: dict[str, tuple[float, list[str]]] = {}


async def is_allowed(repo: FirestoreRepo, provider: str, user_id: object) -> bool:
    """True if `user_id` may use this bot — always true when the allowlist is empty."""
    allowlist = await _get_allowlist(repo, provider)
    if not allowlist:
        return True
    return str(user_id) in allowlist


async def _get_allowlist(repo: FirestoreRepo, provider: str) -> list[str]:
    key = f"{provider}_allowlist"
    now = time.monotonic()
    cached = _cache.get(key)
    if cached is not None and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    setting = await repo.get_setting(key)
    raw = setting.value if setting is not None else None
    ids = [str(v) for v in raw] if isinstance(raw, list) else []
    _cache[key] = (now, ids)
    return ids
