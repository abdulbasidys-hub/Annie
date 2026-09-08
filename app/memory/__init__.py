"""Annie's memory: markdown files she writes, plus the cheap machinery around them.

The rewrite this package implements (2026-09-08) reversed the system's centre
of gravity. Before, the source of truth was Firestore and "memory" was a
collection of documents an LLM occasionally summarised. Now the source of
truth is a folder of markdown you can open and read, and the databases are
support:

``files.py`` / ``paths.py``
    The markdown itself, and the rules about where it can live.

``db.py`` / ``ledger.py``
    Local SQLite. Every launch sighted, every creator movement, every price
    check — the high-volume facts that used to be Firestore documents and
    are now free. Aggressively pruned; a sighting that never moved is gone
    in 48 hours.

``index.py``
    Retrieval. Exact-key lookup first, FTS5 second, so a cycle reads six
    relevant excerpts instead of the whole memory.

``signals.py``
    Statistical comparison over the ledger — what is over-represented among
    tokens that actually won, versus baseline.

``digest.py`` / ``learn.py`` / ``rollup.py`` / ``ideas.py``
    The intelligence layer. Deterministic Python does all the counting and
    ranking for free; one bounded LLM call per cycle turns that into edits
    to the memory files.

``snapshot.py``
    Firestore mirror of the markdown, written only on change, read only when
    the local directory is empty. This is the only Firestore traffic the
    package generates.

``service.py``
    The single door in. Callers outside this package use it, never the
    modules above, so no write can land on disk without also being indexed.
"""

from __future__ import annotations

__all__ = [
    "bootstrap",
    "db",
    "digest",
    "files",
    "ideas",
    "index",
    "learn",
    "ledger",
    "paths",
    "rollup",
    "service",
    "signals",
    "snapshot",
]
