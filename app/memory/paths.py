"""Where Annie's memory physically lives, and how a path is allowed to look.

Two rules this module exists to enforce, both learned the expensive way:

1. **One root, resolved once.** Every reader and writer goes through
   :func:`memory_root`, so "where is memory" is never answered differently
   in two places. On Railway that root must be an attached Volume mount —
   the container filesystem is wiped on redeploy — which is why the
   ``ANNIE_MEMORY_DIR`` setting exists rather than a hardcoded ``./memory``.

2. **No path escapes the root.** Memory paths arrive from an LLM
   (``app/memory/learn.py`` asks the model which file to edit) and from HTTP
   query strings. Both are untrusted. :func:`safe_relpath` normalises and
   rejects anything that would resolve outside the root or outside the known
   section list, so the worst a confused model can do is write a junk note
   into ``notes/``, never touch ``/etc`` or the app's own source.

The section layout below is the whole filing system. It is deliberately
small and human-shaped — the point of this rewrite is that you can open the
folder and read what Annie thinks, so there is no directory here whose
purpose needs explaining.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from app.config import get_settings

#: The directory names Annie is allowed to write into, each with the kind of
#: thinking that belongs there. Anything not listed is rejected by
#: :func:`safe_relpath` — an LLM inventing a new top-level folder every week
#: is exactly how a memory turns back into an unsearchable data dump.
SECTIONS: dict[str, str] = {
    "core": (
        "Annie's standing beliefs about the market. Few files, rewritten in "
        "place rather than appended to — this is what she currently thinks, "
        "not a history of what she once thought."
    ),
    "daily": "One file per day, written at the end-of-day cycle.",
    "weekly": "One file per ISO week, summarising that week's dailies.",
    "monthly": "One file per calendar month, summarising that month's weeks.",
    "creators": (
        "One dossier per tracked creator wallet. Only wallets that earned "
        "attention — repeat launchers and anyone who has produced a winner."
    ),
    "tokens": (
        "One file per notable token: the ones that actually moved. Holds the "
        "CA and the creator wallet so they can be looked up by either."
    ),
    "narratives": "One file per live narrative/theme Annie is tracking.",
    "playbook": (
        "What has actually worked. The section a launch idea is generated "
        "from — patterns with evidence behind them, not observations."
    ),
    "notes": "Loose thinking that has not earned a home in another section yet.",
}

#: Files Annie always has, created on first boot by
#: :mod:`app.memory.bootstrap`. Everything else she creates herself.
CORE_FILES: tuple[str, ...] = (
    "core/market-model.md",
    "core/whats-working.md",
    "core/open-questions.md",
    "core/watchlist.md",
)

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


class MemoryPathError(ValueError):
    """A memory path was outside the root, or not shaped like one."""


def memory_root() -> Path:
    """The directory holding memory files, the SQLite database and nothing else.

    Created on first access. A missing volume is not an error here — the
    directory is simply created inside the container and the deployment runs
    with memory that does not survive a redeploy, which
    :func:`durability_report` surfaces on System Health rather than hiding.
    """
    configured = (get_settings().annie_memory_dir or "").strip()
    root = Path(configured) if configured else Path.cwd() / "memory"
    root.mkdir(parents=True, exist_ok=True)
    return root


def db_path() -> Path:
    """The single SQLite file: creator ledger, launch sightings, search index.

    Deliberately inside the memory root rather than somewhere separate — it
    is derived from and rebuildable against the markdown files, so the two
    must live or die together. A volume that has the files but not the index
    rebuilds in seconds (:func:`app.memory.index.reindex_all`); a volume with
    an index but no files would be a memory with no content.
    """
    return memory_root() / "annie.db"


def safe_relpath(raw: str) -> str:
    """Normalise an untrusted memory path, or raise.

    Accepts ``section/name.md`` and ``section/sub/name.md`` (one level of
    nesting, used by ``daily/2026/…`` style grouping). Rejects absolute
    paths, ``..`` anywhere, backslashes, unknown sections, and any segment
    that is not plain ``[A-Za-z0-9._-]``. Adds a ``.md`` suffix when the
    caller omitted one, because "the model forgot the extension" is a
    formatting slip, not a reason to lose a memory.
    """
    if not isinstance(raw, str):
        raise MemoryPathError("Memory path must be a string.")
    candidate = raw.strip().replace("\\", "/").strip("/")
    if not candidate:
        raise MemoryPathError("Memory path is empty.")
    if ".." in candidate.split("/"):
        raise MemoryPathError(f"Memory path {raw!r} tries to escape the memory root.")

    parts = [p for p in candidate.split("/") if p]
    if len(parts) < 2:
        raise MemoryPathError(
            f"Memory path {raw!r} needs a section — one of {sorted(SECTIONS)}."
        )
    if len(parts) > 3:
        raise MemoryPathError(f"Memory path {raw!r} nests too deeply (max section/sub/name).")
    if parts[0] not in SECTIONS:
        raise MemoryPathError(
            f"Unknown memory section {parts[0]!r}. Allowed: {sorted(SECTIONS)}."
        )
    for part in parts:
        if not _SEGMENT_RE.match(part):
            raise MemoryPathError(f"Memory path segment {part!r} is not a safe filename.")

    if not parts[-1].endswith(".md"):
        parts[-1] = f"{parts[-1]}.md"
    return "/".join(parts)


def absolute(relpath: str) -> Path:
    """Resolve a validated relative path against the root, checked again.

    The second check is not redundant with :func:`safe_relpath`: a symlink
    inside the memory directory could still point outside it, and this is the
    only place that can see the resolved result.
    """
    root = memory_root().resolve()
    target = (root / safe_relpath(relpath)).resolve()
    if root != target and root not in target.parents:
        raise MemoryPathError(f"Resolved memory path {target} is outside {root}.")
    return target


def slug(text: str, *, max_length: int = 60) -> str:
    """A filename-safe slug for a wallet, mint, ticker or narrative name.

    Mints and wallets are base58 and already safe; this exists for the
    narrative/theme case where the text is arbitrary.
    """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-").lower()
    return (cleaned or "untitled")[:max_length]


def durability_report() -> dict[str, object]:
    """Whether this deployment's memory will survive a redeploy.

    Reported on System Health so "my memory vanished" is a visible
    configuration state, not a mystery. The volume check is deliberately
    crude — a configured ``ANNIE_MEMORY_DIR`` outside the app's own working
    directory is the signal, since that is exactly what mounting a Railway
    Volume produces and there is no portable way to ask "am I a mount".
    """
    settings = get_settings()
    configured = (settings.annie_memory_dir or "").strip()
    root = memory_root()
    try:
        outside_cwd = Path.cwd().resolve() not in root.resolve().parents
    except OSError:
        outside_cwd = False
    return {
        "root": str(root),
        "configured": bool(configured),
        "looks_like_volume": bool(configured and outside_cwd),
        "writable": os.access(root, os.W_OK),
        "note": (
            "Memory is on a configured directory outside the app tree — this is "
            "what an attached Railway Volume looks like."
            if configured and outside_cwd
            else "ANNIE_MEMORY_DIR is unset, so memory lives in the container's "
            "own filesystem and will be lost on redeploy. It is mirrored to "
            "Firestore and restored on boot, but attaching a Volume and "
            "pointing ANNIE_MEMORY_DIR at it is the durable setup."
        ),
    }
