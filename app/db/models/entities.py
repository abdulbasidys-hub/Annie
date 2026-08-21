"""Launchpads, DEXes, creators — the *subjects* of the research (§5, §17, §18).

Firestore collections, keyed by natural id (never an autoincrement integer,
which Firestore has no primitive for):

* ``launchpads/{slug}``
* ``dexes/{slug}``
* ``creators/{wallet}``

Build.md §5 is emphatic that nothing may hardcode Pump.fun as the origin of
success, so launchpad documents are created on first sighting by discovery,
never seeded from an enum in code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.db.enums import LaunchpadLifecycle


@dataclass(slots=True)
class Launchpad:
    slug: str
    name: str = ""
    website: str | None = None
    program_ids: list[str] = field(default_factory=list)
    ecosystem: str = "solana"

    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None

    is_known: bool = False
    discovered_by: str | None = None

    lifecycle: str = LaunchpadLifecycle.EMERGING
    launch_count: int = 0
    qualified_count: int = 0
    counts_by_tier: dict[str, int] = field(default_factory=dict)
    success_rate: float | None = None
    market_share: float | None = None
    growth_rate_7d: float | None = None
    growth_rate_30d: float | None = None
    median_minutes_to_first_milestone: int | None = None
    stats_computed_at: datetime | None = None

    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class Dex:
    slug: str
    name: str = ""
    program_ids: list[str] = field(default_factory=list)
    ecosystem: str = "solana"
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    is_known: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class Narrative:
    """A recurring theme discovered across names, tickers, descriptions (§16).

    Not yet populated by any pipeline stage in this deployment — the trend
    engine's ``token.theme`` feature (§16's seed vocabulary, see
    :mod:`app.analysis.features`) already stands in as a lightweight
    narrative signal for trend detection. A dedicated narrative-clustering
    stage that fills this collection in is listed as unbuilt in README's
    "picking up the unfinished work".
    """

    slug: str
    label: str = ""
    description: str | None = None
    category: str | None = None
    keywords: list[str] = field(default_factory=list)
    is_emergent: bool = True
    merged_into_slug: str | None = None

    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None

    token_count: int = 0
    qualified_count: int = 0
    share_of_qualified: float | None = None
    baseline_share: float | None = None
    stats_computed_at: datetime | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class Creator:
    wallet: str
    first_launch_at: datetime | None = None
    last_launch_at: datetime | None = None

    total_launches: int = 0
    wins_100k: int = 0
    wins_250k: int = 0
    wins_500k: int = 0
    wins_1m: int = 0

    success_rate: float | None = None
    best_market_cap: Decimal | None = None

    median_hours_between_launches: float | None = None
    launchpad_history: list[dict] = field(default_factory=list)
    primary_launchpad_slug: str | None = None
    is_repeat_winner: bool = False

    stats_computed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    _MONEY_FIELDS = frozenset({"best_market_cap"})
