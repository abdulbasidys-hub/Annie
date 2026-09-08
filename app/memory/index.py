"""Annie's retrieval system: how she finds the right memory without reading all of them.

The requirement this answers is "during every cycle she shouldn't have to
search through all files, but should find what she's looking for easily". The
naive version of memory — load every file into the prompt — is the thing that
turns a memory system back into a data dump and puts the cost you moved off
Firestore straight into AI tokens instead. So retrieval here is a **two-tier
lookup, cheapest first**:

1. **Keys** (``doc_keys``). Every memory declares the exact handles it is
   about: mint addresses, creator wallets, tickers, narrative slugs. A cycle
   almost always knows the handle it cares about — it is holding the mint —
   so this is a single indexed lookup that reads one row and one file. No
   scanning, no ranking, no model involved.

2. **Full text** (``docs_fts``, FTS5 + porter stemming). Only consulted when
   the question is fuzzy ("what have I learned about cat memes") or when the
   key lookup came back empty. Ranked by BM25, blended with the memory's own
   ``importance`` so a durable lesson outranks a passing note that happened
   to repeat the query word more times.

Both tiers are derived state, rebuilt from the markdown files by
:func:`reindex_all`. The files are the truth; if the index and the files ever
disagree, the index is wrong and gets thrown away. That is what keeps this
honest: you can delete ``annie.db``, restart, and lose nothing but a few
seconds of rebuild.

Retrieval deliberately returns *excerpts*, not whole files
(:func:`search`'s ``snippet``). A creator dossier can run to thousands of
words after a month of appends; feeding all of it to a model to answer "is
this wallet worth watching" is the same mistake as feeding it every token.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

import structlog

from app.memory import db
from app.memory.files import MemoryFile, MemoryStore

log = structlog.get_logger(__name__)

#: Words never worth an FTS query term — they match everything and rank
#: nothing. Kept tiny on purpose; the porter tokenizer handles the rest.
_QUERY_STOPWORDS = frozenset(
    "the a an and or of on in to for with is it this that what which who "
    "how why when where do does did have has had about".split()
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}")

#: A base58 Solana address. Used to auto-derive keys from a memory's body so
#: a mint mentioned in prose is findable even if the model forgot to list it
#: in the header — the operator's "write CA and creator to memory so I can
#: request it" requirement must not depend on an LLM remembering a field.
_ADDRESS_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


@dataclass(slots=True)
class Hit:
    path: str
    title: str
    section: str
    snippet: str
    score: float
    matched_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "title": self.title,
            "section": self.section,
            "snippet": self.snippet,
            "score": round(self.score, 4),
            "matched_key": self.matched_key,
        }


# -----------------------------------------------------------------------------
# Indexing
# -----------------------------------------------------------------------------


def derive_keys(memory: MemoryFile) -> list[str]:
    """The handles this memory should be findable by.

    Union of the declared ``keys`` header, the tags, the filename stem (a
    creator dossier is named for its wallet, a token file for its mint), and
    any Solana address appearing in the body. Lowercased for lookup, since
    a user typing a wallet into chat will not match its case reliably.
    """
    found = {k.strip().lower() for k in memory.keys if k.strip()}
    found.update(t.strip().lower() for t in memory.tags if t.strip())
    stem = memory.path.rsplit("/", 1)[-1].removesuffix(".md")
    if stem:
        found.add(stem.lower())
    for address in _ADDRESS_RE.findall(memory.body):
        found.add(address.lower())
    return sorted(k for k in found if len(k) >= 2)


def index_file(memory: MemoryFile) -> None:
    """(Re)index one memory. Called on every write, so it must stay cheap."""
    db.execute(
        """
        INSERT INTO docs (path, section, title, kind, tags, importance, updated, hash, body)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            section = excluded.section, title = excluded.title, kind = excluded.kind,
            tags = excluded.tags, importance = excluded.importance,
            updated = excluded.updated, hash = excluded.hash, body = excluded.body
        """,
        (
            memory.path, memory.section, memory.title, memory.kind,
            " ".join(memory.tags), memory.importance, memory.updated,
            memory.content_hash, memory.body,
        ),
    )

    db.execute("DELETE FROM doc_keys WHERE path = ?", (memory.path,))
    keys = derive_keys(memory)
    if keys:
        db.executemany(
            "INSERT OR REPLACE INTO doc_keys (key, path, weight) VALUES (?, ?, ?)",
            [(key, memory.path, memory.importance + 0.5) for key in keys],
        )

    if db.fts_available():
        db.execute("DELETE FROM docs_fts WHERE path = ?", (memory.path,))
        db.execute(
            "INSERT INTO docs_fts (path, title, tags, body) VALUES (?, ?, ?, ?)",
            (memory.path, memory.title, " ".join(memory.tags), memory.body),
        )


def drop_file(path: str) -> None:
    db.execute("DELETE FROM docs WHERE path = ?", (path,))
    db.execute("DELETE FROM doc_keys WHERE path = ?", (path,))
    if db.fts_available():
        db.execute("DELETE FROM docs_fts WHERE path = ?", (path,))


def reindex_all(store: MemoryStore | None = None) -> int:
    """Rebuild the whole index from the markdown files.

    Run on boot (cheap — a few hundred files at most) and available as an
    operator action. This is the escape hatch that makes the index safe to
    treat as disposable: any corruption, any schema change, any hand-edit of
    a file outside the app is fixed by calling this.
    """
    store = store or MemoryStore()
    db.execute("DELETE FROM docs")
    db.execute("DELETE FROM doc_keys")
    if db.fts_available():
        db.execute("DELETE FROM docs_fts")

    count = 0
    for memory in store.load_all():
        index_file(memory)
        count += 1
    db.kv_set("index_rebuilt_at", db.utcnow_iso())
    log.info("memory_reindexed", files=count)
    return count


def sync_if_stale(store: MemoryStore | None = None) -> int:
    """Reindex only files whose content hash no longer matches the index.

    Cheaper than a full rebuild and the right thing after a Firestore
    restore or a hand-edit: it notices files added, changed and deleted
    outside the app without re-reading what has not moved.
    """
    store = store or MemoryStore()
    known = {r["path"]: r["hash"] for r in db.query("SELECT path, hash FROM docs")}
    seen: set[str] = set()
    changed = 0
    for memory in store.load_all():
        seen.add(memory.path)
        if known.get(memory.path) != memory.content_hash:
            index_file(memory)
            changed += 1
    for stale in set(known) - seen:
        drop_file(stale)
        changed += 1
    return changed


# -----------------------------------------------------------------------------
# Retrieval
# -----------------------------------------------------------------------------


def by_key(key: str, *, limit: int = 5) -> list[Hit]:
    """Tier 1: exact-handle lookup. One index probe, no scanning."""
    needle = (key or "").strip().lower()
    if not needle:
        return []
    rows = db.query(
        """
        SELECT d.path, d.title, d.section, d.body, k.weight
          FROM doc_keys k JOIN docs d ON d.path = k.path
         WHERE k.key = ?
         ORDER BY k.weight DESC
         LIMIT ?
        """,
        (needle, limit),
    )
    return [
        Hit(
            path=r["path"], title=r["title"], section=r["section"],
            snippet=_excerpt(r["body"], needle), score=float(r["weight"]), matched_key=needle,
        )
        for r in rows
    ]


def search(
    text: str, *, limit: int = 6, section: str | None = None, min_importance: float = 0.0
) -> list[Hit]:
    """Tier 2: ranked full-text search, importance-weighted.

    Tries the exact-key path first because a query is very often a bare mint
    or wallet pasted into chat, and answering that with a text search would
    be both slower and worse.
    """
    query_text = (text or "").strip()
    if not query_text:
        return []

    direct = by_key(query_text, limit=limit)
    if direct:
        return direct

    terms = [
        t.lower() for t in _TOKEN_RE.findall(query_text) if t.lower() not in _QUERY_STOPWORDS
    ]
    if not terms:
        return []

    if db.fts_available():
        hits = _fts_search(terms, limit=limit * 3)
    else:
        hits = _like_search(terms, limit=limit * 3)

    if section:
        hits = [h for h in hits if h.section == section]
    if min_importance > 0:
        allowed = {
            r["path"]
            for r in db.query("SELECT path FROM docs WHERE importance >= ?", (min_importance,))
        }
        hits = [h for h in hits if h.path in allowed]
    return hits[:limit]


def _fts_search(terms: list[str], *, limit: int) -> list[Hit]:
    # OR rather than AND: a memory matching two of three terms strongly is
    # usually more relevant than one matching all three weakly, and bm25
    # already handles the ranking. Terms are quoted so a stray FTS operator
    # in user text is data, not syntax.
    expression = " OR ".join(f'"{t}"' for t in terms)
    try:
        rows = db.query(
            """
            SELECT f.path,
                   d.title, d.section, d.importance,
                   snippet(docs_fts, 3, '', '', ' … ', 24) AS snip,
                   bm25(docs_fts, 0.0, 4.0, 2.0, 1.0) AS rank
              FROM docs_fts f JOIN docs d ON d.path = f.path
             WHERE docs_fts MATCH ?
             ORDER BY rank
             LIMIT ?
            """,
            (expression, limit),
        )
    except Exception:
        log.warning("memory_fts_query_failed", terms=terms, exc_info=True)
        return _like_search(terms, limit=limit)

    hits: list[Hit] = []
    for row in rows:
        # bm25 returns negative numbers, better matches more negative.
        # Flip it, then let importance nudge durable memories upward.
        relevance = -float(row["rank"])
        score = relevance * (0.6 + 0.8 * float(row["importance"] or 0.5))
        hits.append(
            Hit(
                path=row["path"], title=row["title"], section=row["section"],
                snippet=(row["snip"] or "").strip(), score=score,
            )
        )
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits


def _like_search(terms: list[str], *, limit: int) -> list[Hit]:
    """Fallback when SQLite was built without FTS5. Correct, just cruder."""
    clause = " OR ".join(["LOWER(body) LIKE ? OR LOWER(title) LIKE ?"] * len(terms))
    params: list[Any] = []
    for term in terms:
        params.extend([f"%{term}%", f"%{term}%"])
    params.append(limit)
    rows = db.query(
        f"SELECT path, title, section, body, importance FROM docs WHERE {clause} "
        f"ORDER BY importance DESC LIMIT ?",
        params,
    )
    return [
        Hit(
            path=r["path"], title=r["title"], section=r["section"],
            snippet=_excerpt(r["body"], terms[0]), score=float(r["importance"] or 0.5),
        )
        for r in rows
    ]


def recall(
    *, keys: Iterable[str] = (), topics: Iterable[str] = (), per_key: int = 2, per_topic: int = 3,
    budget: int = 10,
) -> list[Hit]:
    """What a cycle calls: "give me what I know that bears on these things".

    Takes the handles the cycle is holding (mints, wallets) plus the topics
    it noticed (themes, launchpads), returns a deduplicated, budget-capped
    set of excerpts. ``budget`` is the guard against the failure mode this
    whole design exists to prevent — a cycle quietly widening until it is
    feeding the entire memory to the model again.
    """
    seen: dict[str, Hit] = {}
    for key in keys:
        for hit in by_key(key, limit=per_key):
            seen.setdefault(hit.path, hit)
    for topic in topics:
        for hit in search(topic, limit=per_topic):
            seen.setdefault(hit.path, hit)

    ranked = sorted(seen.values(), key=lambda h: h.score, reverse=True)
    return ranked[:budget]


def core_context(limit: int = 6) -> list[Hit]:
    """The standing beliefs. Always in front of Annie, never searched for.

    ``core/`` is small and load-bearing — it is what she currently thinks —
    so it is loaded wholesale rather than retrieved. Everything else has to
    earn its way into the prompt through :func:`recall`.
    """
    rows = db.query(
        "SELECT path, title, section, body, importance FROM docs WHERE section = 'core' "
        "ORDER BY importance DESC LIMIT ?",
        (limit,),
    )
    return [
        Hit(path=r["path"], title=r["title"], section=r["section"], snippet=r["body"] or "",
            score=float(r["importance"] or 1.0))
        for r in rows
    ]


def stats() -> dict[str, Any]:
    rows = db.query("SELECT section, COUNT(*) AS n FROM docs GROUP BY section ORDER BY n DESC")
    return {
        "files": int(db.scalar("SELECT COUNT(*) FROM docs")),
        "keys": int(db.scalar("SELECT COUNT(*) FROM doc_keys")),
        "sections": {r["section"]: int(r["n"]) for r in rows},
        "fts": db.fts_available(),
        "rebuilt_at": db.kv_get("index_rebuilt_at"),
    }


def _excerpt(body: str, needle: str, *, width: int = 260) -> str:
    """A window around the first mention, so callers get context not a prefix."""
    text = (body or "").strip()
    if not text:
        return ""
    position = text.lower().find((needle or "").lower())
    if position < 0:
        return text[:width]
    start = max(0, position - width // 3)
    end = min(len(text), start + width)
    prefix = "… " if start > 0 else ""
    suffix = " …" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"
