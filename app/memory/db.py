"""The SQLite file behind memory: connection, schema, and the write budget.

This is the piece that makes the whole rewrite affordable. Firestore charges
per document read and per document write; SQLite on an attached volume
charges nothing at all. So every high-volume *fact* — every launch sighted,
every creator movement, every price check — lives here, and Firestore is left
holding only the small operational state that genuinely needs to be shared
across processes (settings, bot sessions, conversations) plus a snapshot of
the markdown memory.

Concretely, what moved out of Firestore:

===========================  ==========================================
Was (Firestore)              Is now
===========================  ==========================================
``tokens`` (~16k docs/day)   ``sightings`` (SQLite, pruned after 48h)
``tokens/*/features``        computed in memory, never stored per token
``tokens/*/milestones``      ``moves`` rows
``creators``                 ``creators`` + ``moves``
``trends`` + observations    ``signals`` + ``signal_points``
===========================  ==========================================

**Threading.** FastAPI runs this app on one event loop; SQLite calls here are
short (single-statement, indexed) so they run inline rather than through a
thread pool — a few hundred microseconds is not worth an executor hop, and
WAL mode plus ``check_same_thread=False`` keeps the connection usable from
whichever worker thread FastAPI hands us. The one long operation, the full
FTS rebuild, is explicitly offloaded by its caller.

**Durability.** Everything in here is derivable: sightings come back from the
webhook stream within minutes, signals recompute from sightings, and the FTS
index rebuilds from the markdown files. Losing this file costs Annie her
short-term working state, never her memory — which is exactly the split we
want, because it is the working state that is enormous and the memory that is
precious.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

import structlog

from app.memory.paths import db_path

log = structlog.get_logger(__name__)

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_conn_path: str | None = None


SCHEMA: tuple[str, ...] = (
    # -- The launch stream ----------------------------------------------------
    # One row per mint Annie has seen. This is the table that used to be
    # 16,000 Firestore documents a day. Rows that never do anything are
    # deleted by prune_sightings() — the deliberate act of forgetting that
    # separates this from a database.
    """
    CREATE TABLE IF NOT EXISTS sightings (
        mint            TEXT PRIMARY KEY,
        symbol          TEXT,
        name            TEXT,
        creator         TEXT,
        launchpad       TEXT,
        signature       TEXT,
        first_seen      TEXT NOT NULL,
        last_seen       TEXT NOT NULL,
        last_checked    TEXT,
        checks          INTEGER NOT NULL DEFAULT 0,
        market_cap      REAL,
        peak_market_cap REAL,
        liquidity       REAL,
        volume_24h      REAL,
        tier            REAL,
        qualified_at    TEXT,
        status          TEXT NOT NULL DEFAULT 'watching',
        theme           TEXT,
        notes           TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sightings_status ON sightings(status, last_seen DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sightings_check ON sightings(status, last_checked)",
    "CREATE INDEX IF NOT EXISTS idx_sightings_creator ON sightings(creator)",
    "CREATE INDEX IF NOT EXISTS idx_sightings_peak ON sightings(peak_market_cap DESC)",
    # -- Creators -------------------------------------------------------------
    # The one thing the operator asked to keep completely, not filter:
    # "saving every creator's movement so we can identify high token-creating
    # creators". Every launch by every wallet lands in `moves`; the rollup
    # lives on `creators`. Both are free here in a way they could never be at
    # Firestore's per-write price.
    """
    CREATE TABLE IF NOT EXISTS creators (
        wallet          TEXT PRIMARY KEY,
        first_seen      TEXT NOT NULL,
        last_seen       TEXT NOT NULL,
        launches        INTEGER NOT NULL DEFAULT 0,
        winners         INTEGER NOT NULL DEFAULT 0,
        best_market_cap REAL,
        best_mint       TEXT,
        tracked         INTEGER NOT NULL DEFAULT 0,
        dossier_path    TEXT,
        note            TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_creators_launches ON creators(launches DESC)",
    "CREATE INDEX IF NOT EXISTS idx_creators_winners ON creators(winners DESC, best_market_cap DESC)",
    "CREATE INDEX IF NOT EXISTS idx_creators_tracked ON creators(tracked, last_seen DESC)",
    """
    CREATE TABLE IF NOT EXISTS moves (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        wallet      TEXT NOT NULL,
        mint        TEXT,
        kind        TEXT NOT NULL,
        market_cap  REAL,
        detail      TEXT,
        at          TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_moves_wallet ON moves(wallet, at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_moves_at ON moves(at DESC)",
    # -- Signals (what the Firestore `trends` collection used to hold) --------
    """
    CREATE TABLE IF NOT EXISTS signals (
        slug            TEXT PRIMARY KEY,
        name            TEXT NOT NULL,
        category        TEXT,
        namespace       TEXT,
        key             TEXT,
        value           TEXT,
        tier            REAL,
        status          TEXT NOT NULL DEFAULT 'new',
        confidence      TEXT,
        recent_count    INTEGER,
        recent_total    INTEGER,
        recent_freq     REAL,
        baseline_freq   REAL,
        lift            REAL,
        p_value         REAL,
        persistence     INTEGER NOT NULL DEFAULT 0,
        first_seen      TEXT NOT NULL,
        last_seen       TEXT NOT NULL,
        peak_freq       REAL,
        memory_path     TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status, recent_freq DESC)",
    "CREATE INDEX IF NOT EXISTS idx_signals_tier ON signals(tier, status)",
    """
    CREATE TABLE IF NOT EXISTS signal_points (
        slug        TEXT NOT NULL,
        day         TEXT NOT NULL,
        count       INTEGER,
        total       INTEGER,
        freq        REAL,
        PRIMARY KEY (slug, day)
    )
    """,
    # -- The search system ----------------------------------------------------
    # Two tables, on purpose, answering two different questions:
    #
    #   docs + doc_keys  -> "which file is about THIS exact mint/wallet/word"
    #                       an O(1) index lookup, no scanning
    #   docs_fts         -> "which files are about roughly this idea"
    #                       FTS5 ranked full text, only consulted when the
    #                       key lookup came back empty
    #
    # That ordering is the whole answer to "she shouldn't have to search
    # through all files every cycle": a cycle almost always knows the exact
    # handle it cares about (a mint, a wallet, a theme name), so the common
    # path never touches the text index at all.
    """
    CREATE TABLE IF NOT EXISTS docs (
        path        TEXT PRIMARY KEY,
        section     TEXT,
        title       TEXT,
        kind        TEXT,
        tags        TEXT,
        importance  REAL,
        updated     TEXT,
        hash        TEXT,
        body        TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_docs_section ON docs(section, updated DESC)",
    "CREATE INDEX IF NOT EXISTS idx_docs_importance ON docs(importance DESC)",
    """
    CREATE TABLE IF NOT EXISTS doc_keys (
        key     TEXT NOT NULL,
        path    TEXT NOT NULL,
        weight  REAL NOT NULL DEFAULT 1.0,
        PRIMARY KEY (key, path)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_doc_keys_path ON doc_keys(path)",
    # -- Bookkeeping ----------------------------------------------------------
    # Counters the Firestore budget guard reads (app/db/budget.py) and the
    # scheduler's last-run state, both moved off Firestore so that merely
    # *measuring* cost does not cost anything.
    # -- Launch ideas ---------------------------------------------------------
    # Kept structured as well as written to the playbook. The markdown is what
    # Annie reads back on later cycles; this is what the Ideas page renders as
    # cards, and parsing her prose back into fields to do that would be both
    # fragile and pointless when the generator already had the structure.
    """
    CREATE TABLE IF NOT EXISTS ideas (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        generated_at TEXT NOT NULL,
        day          TEXT NOT NULL,
        origin       TEXT NOT NULL,
        brief        TEXT,
        payload      TEXT NOT NULL,
        memory_path  TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ideas_day ON ideas(day DESC, generated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ideas_origin ON ideas(origin, generated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS counters (
        name    TEXT NOT NULL,
        day     TEXT NOT NULL,
        value   INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (name, day)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kv (
        key     TEXT PRIMARY KEY,
        value   TEXT,
        updated TEXT
    )
    """,
)

#: FTS5 is a compile-time option. It is present in every CPython wheel we
#: deploy on, but a hostile build could omit it — in which case search falls
#: back to LIKE matching over ``docs.body`` rather than the app failing to
#: start. Recorded here so the fallback is a known state, not a mystery.
FTS_SCHEMA: tuple[str, ...] = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
        path UNINDEXED,
        title,
        tags,
        body,
        tokenize = 'porter unicode61'
    )
    """,
)

_fts_available = True


def utcnow_iso() -> str:
    """A UTC ISO-8601 stamp, microseconds included.

    Timestamps are stored as text and compared lexicographically, so the
    format has to be identical everywhere or ordering silently stops matching
    chronology. Microseconds are kept rather than truncated to seconds
    because truncating both the stored value *and* a query bound makes a
    strict ``<`` comparison exclude everything written in the current second
    — which is exactly what happened: a token that qualified in the same
    second a cycle started was dropped from that cycle's own window.
    """
    return datetime.now(timezone.utc).isoformat()


def today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


#: Columns added to an existing table after it shipped, as
#: ``table -> {column: declaration}``.
#:
#: ``CREATE TABLE IF NOT EXISTS`` is a no-op on a database that already has
#: the table, so a new column in :data:`SCHEMA` reaches a fresh deployment and
#: never reaches the one with the data in it. On a Railway Volume holding a
#: live ledger that is the difference between a working feature and a column
#: that does not exist in the only place it matters.
#:
#: Additive only, and deliberately so — no drops, no renames, no type changes.
#: Anything that could lose a row does not belong in a migration that runs
#: unattended on every boot.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "sightings": {
        # When metadata was last looked up, whether or not anything came back.
        # A token DAS has nothing for has to be distinguishable from one never
        # tried, or the unresolvable ones sit at the front of the queue and
        # crowd out real winners on every pass.
        "metadata_checked_at": "TEXT",
        # Same idea for the deployer walk, which is far more expensive and so
        # runs in much smaller batches.
        "deployer_checked_at": "TEXT",
        # The links from the token's own metadata. Kept because what a
        # launch *built* is evidence in its own right: the site a winner
        # shipped says more about what is working than its ticker does.
        "website": "TEXT",
        "twitter": "TEXT",
        "telegram": "TEXT",
    },
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # table not created yet; SCHEMA will have made it above
        for name, declaration in columns.items():
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            log.info("sqlite_column_added", table=table, column=name)


def connect() -> sqlite3.Connection:
    """The process-wide connection, created and migrated on first use.

    Reconnects if :func:`app.memory.paths.db_path` has changed underneath us,
    which only happens in tests that point the memory root at a temp
    directory — but getting that wrong silently would make every test share
    one database, so it is checked rather than assumed.
    """
    global _conn, _conn_path, _fts_available

    target = str(db_path())
    with _lock:
        if _conn is not None and _conn_path == target:
            return _conn
        if _conn is not None:
            try:
                _conn.close()
            except sqlite3.Error:
                pass

        conn = sqlite3.connect(target, check_same_thread=False, timeout=15.0)
        conn.row_factory = sqlite3.Row
        # WAL lets the read-heavy API surface run while a cycle is writing;
        # NORMAL synchronous is the right trade for data that is all
        # reconstructible (see the module docstring).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")

        for statement in SCHEMA:
            conn.execute(statement)
        _add_missing_columns(conn)
        try:
            for statement in FTS_SCHEMA:
                conn.execute(statement)
            _fts_available = True
        except sqlite3.OperationalError:
            _fts_available = False
            log.warning("sqlite_fts5_unavailable", detail="memory search falls back to LIKE matching")
        conn.commit()

        _conn, _conn_path = conn, target
        log.info("memory_db_ready", path=target, fts=_fts_available)
        return conn


def fts_available() -> bool:
    connect()
    return _fts_available


def close() -> None:
    global _conn, _conn_path
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            finally:
                _conn, _conn_path = None, None


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    conn = connect()
    cursor = conn.execute(sql, tuple(params))
    conn.commit()
    return cursor


def executemany(sql: str, rows: Iterable[Iterable[Any]]) -> None:
    conn = connect()
    conn.executemany(sql, [tuple(r) for r in rows])
    conn.commit()


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, tuple(params)).fetchall()


def query_one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return connect().execute(sql, tuple(params)).fetchone()


def scalar(sql: str, params: Iterable[Any] = (), default: Any = 0) -> Any:
    row = query_one(sql, params)
    if row is None or row[0] is None:
        return default
    return row[0]


# -----------------------------------------------------------------------------
# Small key/value + counter helpers
# -----------------------------------------------------------------------------


def kv_get(key: str, default: str | None = None) -> str | None:
    row = query_one("SELECT value FROM kv WHERE key = ?", (key,))
    return row["value"] if row else default


def kv_set(key: str, value: str) -> None:
    execute(
        "INSERT INTO kv(key, value, updated) VALUES(?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated = excluded.updated",
        (key, value, utcnow_iso()),
    )


def counter_add(name: str, amount: int = 1, *, day: str | None = None) -> int:
    """Increment today's counter and return the new total."""
    stamp = day or today_utc()
    execute(
        "INSERT INTO counters(name, day, value) VALUES(?, ?, ?) "
        "ON CONFLICT(name, day) DO UPDATE SET value = value + excluded.value",
        (name, stamp, amount),
    )
    return int(scalar("SELECT value FROM counters WHERE name = ? AND day = ?", (name, stamp)))


def counter_get(name: str, *, day: str | None = None) -> int:
    return int(scalar("SELECT value FROM counters WHERE name = ? AND day = ?", (name, day or today_utc())))


def counters_today() -> dict[str, int]:
    rows = query("SELECT name, value FROM counters WHERE day = ?", (today_utc(),))
    return {r["name"]: int(r["value"]) for r in rows}


def prune_counters(keep_days: int = 14) -> int:
    """Counters are a rolling window; older days are noise."""
    cursor = execute(
        "DELETE FROM counters WHERE day < date('now', ?)", (f"-{int(keep_days)} days",)
    )
    return cursor.rowcount or 0
