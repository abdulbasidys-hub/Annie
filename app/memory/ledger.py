"""The ledger: what Annie has seen, who launched it, and what moved.

This replaces the ``tokens``, ``tokens/*/features``, ``tokens/*/milestones``
and ``creators`` Firestore collections with local SQLite. The behavioural
change that matters is not the storage engine, it is **attention**:

* Everything gets *sighted*. A launch costs one cheap local insert, so there
  is no reason to filter at the door and no risk of missing something.
* Almost nothing gets *watched*. Only sightings that clear
  :data:`WATCH_FLOOR_USD` or belong to a tracked creator keep getting
  re-priced, because re-pricing is the only part with a real (API) cost.
* Almost nothing gets *remembered*. Only tokens that actually reach a tier
  get a markdown memory. :func:`prune` deletes the rest after
  ``watch_ttl_hours`` — a memecoin that has not moved in two days is not
  going to, and a human watching the market drops it from attention too.

Creator movements are the deliberate exception to the forgetting. The
operator's instruction was explicit — save every creator's movement so
high-frequency launchers can be identified and tracked from then on — so
:func:`record_launch` writes a ``moves`` row for every single launch and
never prunes wallets, only the token rows around them. At roughly 16k
launches a day that is ~6M rows a year in a file measured in hundreds of
megabytes: free here, and completely impossible at Firestore's per-write
price, which is the entire reason this table lives in SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Sequence

import structlog

from app.memory import db

log = structlog.get_logger(__name__)

#: Below this market cap a token is not worth spending an API call to
#: re-check. Well under the $100k qualification floor so a token climbing
#: toward it is still followed, but far enough above the noise that the
#: thousands of launches that never trade at all drop out immediately.
WATCH_FLOOR_USD = 15_000.0

#: A wallet becomes "tracked" once it has launched this many times. Tracked
#: creators get their tokens re-priced ahead of everything else and get a
#: dossier written — this is the "identify high token-creating creators and
#: track them from then on" mechanism.
#:
#: Set high deliberately. On Pump.fun, launching a handful of tokens is
#: unremarkable — bot wallets spray dozens a day — so a low threshold would
#: mark most of the market as tracked and make the distinction meaningless.
#: Twenty-five is "this wallet is running an operation", which is the thing
#: actually worth following.
TRACK_AFTER_LAUNCHES = 25

#: …or once any one of its tokens has reached this. One real winner is a
#: stronger signal than a dozen dead launches.
TRACK_AFTER_MARKET_CAP = 100_000.0

STATUS_WATCHING = "watching"
STATUS_QUALIFIED = "qualified"
STATUS_FADED = "faded"
STATUS_DEAD = "dead"


def _now() -> str:
    return db.utcnow_iso()


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class Sighting:
    mint: str
    symbol: str | None = None
    name: str | None = None
    creator: str | None = None
    launchpad: str | None = None
    signature: str | None = None
    first_seen: str = ""
    last_seen: str = ""
    last_checked: str | None = None
    checks: int = 0
    market_cap: float | None = None
    peak_market_cap: float | None = None
    liquidity: float | None = None
    volume_24h: float | None = None
    tier: float | None = None
    qualified_at: str | None = None
    status: str = STATUS_WATCHING
    theme: str | None = None
    notes: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> "Sighting":
        return cls(**{k: row[k] for k in row.keys() if k in cls.__slots__})

    def to_dict(self) -> dict[str, Any]:
        return {slot: getattr(self, slot) for slot in self.__slots__}


# -----------------------------------------------------------------------------
# Writing
# -----------------------------------------------------------------------------


def record_launch(
    *,
    mint: str,
    creator: str | None = None,
    launchpad: str | None = None,
    symbol: str | None = None,
    name: str | None = None,
    signature: str | None = None,
    launched_at: datetime | None = None,
) -> bool:
    """Record one launch. Returns ``True`` if this mint is new to Annie.

    Two writes, both local: the sighting itself, and — when a creator wallet
    is known — the creator rollup plus one immutable ``moves`` row. The move
    row is the thing that makes "this wallet has launched 47 times this week"
    answerable later without having kept 47 token documents.

    Never overwrites richer state with poorer state: a second sighting of a
    mint already qualified only touches ``last_seen``.
    """
    now = _now()
    seen_at = launched_at.astimezone(timezone.utc).isoformat() if launched_at else now

    # SQLite reports rowcount 1 for both the insert and the upsert-update
    # branch, so novelty is asked before the write rather than inferred from
    # the cursor afterwards. One extra indexed local lookup, no ambiguity —
    # and this must be right, because it decides whether the creator gets a
    # `moves` row (a second row for a redelivered webhook event would inflate
    # exactly the launch counts the tracking decision is based on).
    is_new = db.query_one("SELECT 1 FROM sightings WHERE mint = ?", (mint,)) is None

    db.execute(
        """
        INSERT INTO sightings (mint, symbol, name, creator, launchpad, signature,
                               first_seen, last_seen, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(mint) DO UPDATE SET
            last_seen = excluded.last_seen,
            symbol    = COALESCE(sightings.symbol, excluded.symbol),
            name      = COALESCE(sightings.name, excluded.name),
            creator   = COALESCE(sightings.creator, excluded.creator),
            launchpad = COALESCE(sightings.launchpad, excluded.launchpad)
        """,
        (mint, symbol, name, creator, launchpad, signature, seen_at, now, STATUS_WATCHING),
    )

    if creator and is_new:
        record_creator_move(wallet=creator, mint=mint, kind="launch", at=now)
    return is_new


def record_creator_move(
    *,
    wallet: str,
    mint: str | None = None,
    kind: str = "launch",
    market_cap: float | Decimal | None = None,
    detail: str | None = None,
    at: str | None = None,
) -> None:
    """Append one immutable movement and refresh the wallet's rollup.

    ``kind`` is free text by design — ``launch``, ``qualified``, ``peak``,
    and whatever a later cycle decides is worth marking. Constraining it to
    an enum would mean a migration every time Annie notices a new kind of
    behaviour, and nothing downstream branches on unknown values.
    """
    stamp = at or _now()
    cap = _f(market_cap)

    db.execute(
        "INSERT INTO moves (wallet, mint, kind, market_cap, detail, at) VALUES (?, ?, ?, ?, ?, ?)",
        (wallet, mint, kind, cap, detail, stamp),
    )
    db.execute(
        """
        INSERT INTO creators (wallet, first_seen, last_seen, launches, winners, best_market_cap, best_mint)
        VALUES (?, ?, ?, ?, 0, ?, ?)
        ON CONFLICT(wallet) DO UPDATE SET
            last_seen = excluded.last_seen,
            launches  = creators.launches + excluded.launches
        """,
        (wallet, stamp, stamp, 1 if kind == "launch" else 0, cap, mint if cap else None),
    )
    if cap is not None:
        db.execute(
            """
            UPDATE creators
               SET best_market_cap = ?, best_mint = ?
             WHERE wallet = ? AND (best_market_cap IS NULL OR best_market_cap < ?)
            """,
            (cap, mint, wallet, cap),
        )
    _maybe_track(wallet)


def _maybe_track(wallet: str) -> None:
    """Promote a wallet to tracked once it earns it. Never demotes.

    Demotion would make the tracked set flap around the threshold and lose
    the dossier history that made the wallet interesting in the first place.
    """
    db.execute(
        """
        UPDATE creators SET tracked = 1
         WHERE wallet = ? AND tracked = 0
           AND (launches >= ? OR COALESCE(best_market_cap, 0) >= ?)
        """,
        (wallet, TRACK_AFTER_LAUNCHES, TRACK_AFTER_MARKET_CAP),
    )


def record_price(
    *,
    mint: str,
    market_cap: float | Decimal | None,
    liquidity: float | Decimal | None = None,
    volume_24h: float | Decimal | None = None,
    tier: float | Decimal | None = None,
) -> dict[str, Any]:
    """Fold one price observation into a sighting.

    Returns ``{"newly_qualified": bool, "new_peak": bool}`` so the caller can
    decide whether this deserves a memory file or a creator move, rather than
    re-reading the row to find out.
    """
    cap = _f(market_cap)
    now = _now()
    row = db.query_one("SELECT peak_market_cap, status, creator FROM sightings WHERE mint = ?", (mint,))
    if row is None:
        return {"newly_qualified": False, "new_peak": False}

    previous_peak = _f(row["peak_market_cap"]) or 0.0
    new_peak = cap is not None and cap > previous_peak
    tier_value = _f(tier)
    newly_qualified = bool(tier_value) and row["status"] != STATUS_QUALIFIED

    db.execute(
        """
        UPDATE sightings
           SET market_cap      = COALESCE(?, market_cap),
               liquidity       = COALESCE(?, liquidity),
               volume_24h      = COALESCE(?, volume_24h),
               peak_market_cap = CASE WHEN ? THEN ? ELSE peak_market_cap END,
               tier            = CASE WHEN ? IS NOT NULL AND (tier IS NULL OR tier < ?) THEN ? ELSE tier END,
               qualified_at    = CASE WHEN ? THEN ? ELSE qualified_at END,
               status          = CASE WHEN ? THEN ? ELSE status END,
               last_checked    = ?,
               checks          = checks + 1
         WHERE mint = ?
        """,
        (
            cap, _f(liquidity), _f(volume_24h),
            1 if new_peak else 0, cap,
            tier_value, tier_value, tier_value,
            1 if newly_qualified else 0, now,
            1 if newly_qualified else 0, STATUS_QUALIFIED,
            now, mint,
        ),
    )

    if newly_qualified and row["creator"]:
        record_creator_move(
            wallet=row["creator"], mint=mint, kind="qualified", market_cap=cap,
            detail=f"reached ${tier_value:,.0f} tier" if tier_value else None,
        )
        db.execute(
            "UPDATE creators SET winners = winners + 1 WHERE wallet = ?", (row["creator"],)
        )
    return {"newly_qualified": newly_qualified, "new_peak": new_peak}


def mark_checked(mints: Sequence[str]) -> None:
    """Stamp mints the price provider returned nothing for.

    Without this a mint with no DexScreener pair (never traded, which is most
    of them) would be picked by ``due_for_check`` forever, since that query
    orders by ``last_checked``. Counting the miss is what lets
    :func:`prune` eventually drop it.
    """
    if not mints:
        return
    now = _now()
    db.executemany(
        "UPDATE sightings SET last_checked = ?, checks = checks + 1 WHERE mint = ?",
        [(now, m) for m in mints],
    )


def set_theme(mint: str, theme: str | None) -> None:
    db.execute("UPDATE sightings SET theme = ? WHERE mint = ?", (theme, mint))


# -----------------------------------------------------------------------------
# Reading
# -----------------------------------------------------------------------------


def due_for_check(limit: int, *, tracked_first: bool = True) -> list[Sighting]:
    """Which mints to re-price on this pass, in priority order.

    The ordering is the cost control. Newest-first was the old pipeline's
    rule and it is right for the head of the stream, but it starves anything
    that started climbing an hour after launch. This orders by, in turn:
    tracked-creator tokens, then already-moving tokens, then never-checked
    ones, then longest-since-checked. Everything below
    :data:`WATCH_FLOOR_USD` that has already been checked a few times and
    stayed flat falls off the end naturally.
    """
    rows = db.query(
        """
        SELECT s.* FROM sightings s
        LEFT JOIN creators c ON c.wallet = s.creator
        WHERE s.status IN (?, ?)
        ORDER BY
            CASE WHEN ? AND COALESCE(c.tracked, 0) = 1 THEN 0 ELSE 1 END,
            CASE WHEN COALESCE(s.market_cap, 0) >= ? THEN 0 ELSE 1 END,
            CASE WHEN s.last_checked IS NULL THEN 0 ELSE 1 END,
            s.last_checked ASC,
            s.last_seen DESC
        LIMIT ?
        """,
        (STATUS_WATCHING, STATUS_QUALIFIED, 1 if tracked_first else 0, WATCH_FLOOR_USD, limit),
    )
    return [Sighting.from_row(r) for r in rows]


def get_sighting(mint: str) -> Sighting | None:
    row = db.query_one("SELECT * FROM sightings WHERE mint = ?", (mint,))
    return Sighting.from_row(row) if row else None


def movers(
    *, since_hours: int = 24, min_market_cap: float = WATCH_FLOOR_USD, limit: int = 50
) -> list[Sighting]:
    """The tokens that actually did something in the window.

    This is the input to every intelligence step — the ~40 rows a cycle
    reasons about, out of the ~16,000 it saw. Ordered by peak rather than
    current cap so a token that ran and retraced still shows up; that round
    trip is often the more interesting story.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    rows = db.query(
        """
        SELECT * FROM sightings
         WHERE last_seen >= ? AND COALESCE(peak_market_cap, 0) >= ?
         ORDER BY peak_market_cap DESC
         LIMIT ?
        """,
        (cutoff, min_market_cap, limit),
    )
    return [Sighting.from_row(r) for r in rows]


def qualified_in_window(
    start: datetime, end: datetime, *, min_tier: float = 0.0, end_inclusive: bool = True
) -> list[Sighting]:
    """Tokens that cleared a tier inside ``[start, end]``.

    ``end_inclusive`` defaults to True, and that default is load-bearing.
    Almost every caller means "everything up to right now" and passes
    ``datetime.now()`` — but the clock has finite resolution (coarse on
    Windows, ~1-15ms), so a token qualified moments earlier can carry a
    timestamp *identical* to that bound. With an exclusive end it was dropped
    from that window, and because the next window starts at the same instant,
    it was dropped from that one too: silently invisible to signals forever.
    Confirmed by watching the count come back as 2, 6, 9 and 10 out of 10 on
    successive runs of the same code.

    Adjacent windows that must not overlap — the baseline-versus-recent pair
    in :mod:`app.memory.signals` — pass ``end_inclusive=False`` so a token
    exactly on the boundary is counted once, in the later window.
    """
    comparison = "<=" if end_inclusive else "<"
    rows = db.query(
        f"""
        SELECT * FROM sightings
         WHERE qualified_at IS NOT NULL AND qualified_at >= ? AND qualified_at {comparison} ?
           AND COALESCE(peak_market_cap, 0) >= ?
         ORDER BY peak_market_cap DESC
        """,
        (
            start.astimezone(timezone.utc).isoformat(),
            end.astimezone(timezone.utc).isoformat(),
            min_tier,
        ),
    )
    return [Sighting.from_row(r) for r in rows]


def top_creators(*, limit: int = 25, tracked_only: bool = False, window_hours: int | None = None) -> list[dict[str, Any]]:
    """Highest-volume / most successful launchers.

    ``window_hours`` switches from lifetime totals to "who is busy right
    now", counted from ``moves`` — the question that actually matters when
    deciding who to watch this week.
    """
    if window_hours:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
        rows = db.query(
            """
            SELECT c.wallet, c.launches, c.winners, c.best_market_cap, c.best_mint,
                   c.tracked, c.first_seen, c.last_seen, c.dossier_path,
                   COUNT(m.id) AS recent_launches
              FROM creators c
              JOIN moves m ON m.wallet = c.wallet AND m.kind = 'launch' AND m.at >= ?
             WHERE (? = 0 OR c.tracked = 1)
             GROUP BY c.wallet
             ORDER BY recent_launches DESC, c.best_market_cap DESC
             LIMIT ?
            """,
            (cutoff, 1 if tracked_only else 0, limit),
        )
    else:
        rows = db.query(
            """
            SELECT wallet, launches, winners, best_market_cap, best_mint, tracked,
                   first_seen, last_seen, dossier_path, launches AS recent_launches
              FROM creators
             WHERE (? = 0 OR tracked = 1)
             ORDER BY winners DESC, best_market_cap DESC, launches DESC
             LIMIT ?
            """,
            (1 if tracked_only else 0, limit),
        )
    return [dict(r) for r in rows]


def get_creator(wallet: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM creators WHERE wallet = ?", (wallet,))
    return dict(row) if row else None


def creator_moves(wallet: str, *, limit: int = 100) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM moves WHERE wallet = ? ORDER BY at DESC LIMIT ?", (wallet, limit)
    )
    return [dict(r) for r in rows]


def creator_tokens(wallet: str, *, limit: int = 50) -> list[Sighting]:
    rows = db.query(
        "SELECT * FROM sightings WHERE creator = ? ORDER BY COALESCE(peak_market_cap, 0) DESC LIMIT ?",
        (wallet, limit),
    )
    return [Sighting.from_row(r) for r in rows]


def set_dossier_path(wallet: str, path: str) -> None:
    db.execute("UPDATE creators SET dossier_path = ? WHERE wallet = ?", (path, wallet))


def stats() -> dict[str, Any]:
    """Counts for the dashboard and for the cycle digest's header."""
    day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    return {
        "sightings_total": int(db.scalar("SELECT COUNT(*) FROM sightings")),
        "sightings_24h": int(
            db.scalar("SELECT COUNT(*) FROM sightings WHERE first_seen >= ?", (day_ago,))
        ),
        "watching": int(
            db.scalar("SELECT COUNT(*) FROM sightings WHERE status = ?", (STATUS_WATCHING,))
        ),
        "qualified_total": int(
            db.scalar("SELECT COUNT(*) FROM sightings WHERE qualified_at IS NOT NULL")
        ),
        "qualified_24h": int(
            db.scalar("SELECT COUNT(*) FROM sightings WHERE qualified_at >= ?", (day_ago,))
        ),
        "creators_total": int(db.scalar("SELECT COUNT(*) FROM creators")),
        "creators_tracked": int(db.scalar("SELECT COUNT(*) FROM creators WHERE tracked = 1")),
        "moves_24h": int(db.scalar("SELECT COUNT(*) FROM moves WHERE at >= ?", (day_ago,))),
        # The freshness signal. A webhook that silently stops — a rotated
        # secret, a deleted Helius webhook, a changed transaction type —
        # otherwise looks exactly like a quiet market.
        "last_sighting_at": db.scalar("SELECT MAX(first_seen) FROM sightings", default=None),
        "launchpads": [
            dict(r)
            for r in db.query(
                "SELECT launchpad, COUNT(*) AS n FROM sightings WHERE first_seen >= ? "
                "AND launchpad IS NOT NULL GROUP BY launchpad ORDER BY n DESC",
                (day_ago,),
            )
        ],
    }


# -----------------------------------------------------------------------------
# Forgetting
# -----------------------------------------------------------------------------


def prune(
    *, ttl_hours: int = 48, keep_moves_days: int = 400, keep_qualified_days: int = 150
) -> dict[str, int]:
    """Drop what stopped mattering. The deliberate act of forgetting.

    What is kept, and why:

    * Anything that ever qualified — kept for ``keep_qualified_days``. It is
      evidence, but evidence with an expiry: the signals engine compares a
      7-day window against a 90-day baseline, so a qualifier from five months
      ago is in no window and changes no number. "Kept forever" was the
      original rule and it was written when a qualifier was rare; at real
      Solana volume roughly one token a minute clears the $100k floor, which
      is about half a million rows a year that nothing reads.
    * Any qualifier Annie actually wrote a memory about — kept regardless of
      age, found by asking the search index whether its mint is a key. Those
      are the ones a file points at, and a page whose contract address no
      longer resolves to a row is a broken memory.
    * Anything that ever traded above :data:`WATCH_FLOOR_USD` — kept, because
      it did something, even if it did not do enough.
    * Every ``creators`` row, and every ``moves`` row for over a year — kept,
      because "who launches a lot, and did any of it work" is a question
      about history and these rows are tiny.

    Everything else — the overwhelming majority, tokens that appeared, never
    traded, and died — is deleted once ``ttl_hours`` has passed. That is the
    difference between a memory and a database.

    A tracked creator's *dead* tokens are pruned like anyone else's. Shielding
    them was the obvious-looking rule and it is wrong: a wallet is tracked
    precisely because it launches a lot, so exempting its tokens would exempt
    the largest share of the junk and leave the ledger growing without bound.
    The creator's history survives in ``moves`` and in their dossier, which is
    where it belongs — the pattern is the subject, and keeping ten thousand
    dead token rows is not how you record a pattern.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=ttl_hours)).isoformat()
    dropped = db.execute(
        """
        DELETE FROM sightings
         WHERE qualified_at IS NULL
           AND last_seen < ?
           AND COALESCE(peak_market_cap, 0) < ?
        """,
        (cutoff, WATCH_FLOOR_USD),
    ).rowcount or 0

    # Ordered after the cheap delete so the expensive one sees fewer rows.
    # The NOT IN is over doc_keys, which is small (one row per indexed key)
    # and primary-keyed on the key column — this is an index probe per row,
    # not a scan, and it runs once a cycle.
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_qualified_days)).isoformat()
    expired = db.execute(
        """
        DELETE FROM sightings
         WHERE qualified_at IS NOT NULL
           AND qualified_at < ?
           AND mint NOT IN (SELECT key FROM doc_keys)
        """,
        (stale_cutoff,),
    ).rowcount or 0

    move_cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_moves_days)).isoformat()
    old_moves = db.execute("DELETE FROM moves WHERE at < ?", (move_cutoff,)).rowcount or 0

    faded = db.execute(
        """
        UPDATE sightings SET status = ?
         WHERE status = ? AND last_seen < ? AND COALESCE(peak_market_cap, 0) < ?
        """,
        (STATUS_FADED, STATUS_WATCHING, cutoff, WATCH_FLOOR_USD),
    ).rowcount or 0

    points = db.execute(
        "DELETE FROM signal_points WHERE day < date('now', '-120 days')"
    ).rowcount or 0
    db.prune_counters()
    db.execute("PRAGMA optimize")

    log.info(
        "ledger_pruned",
        sightings=dropped,
        expired_qualifiers=expired,
        faded=faded,
        moves=old_moves,
        points=points,
    )
    return {
        "sightings_dropped": dropped,
        "expired_qualifiers": expired,
        "faded": faded,
        "moves_dropped": old_moves,
        "points_dropped": points,
    }


def vacuum() -> None:
    """Reclaim file space after a large prune. Rare, and never inside a cycle."""
    conn = db.connect()
    conn.isolation_level = None
    try:
        conn.execute("VACUUM")
    finally:
        conn.isolation_level = ""
