"""Reading and writing memory files.

A memory is a markdown file with a small YAML-ish header. Both halves matter
and they are not interchangeable:

* The **body** is what Annie actually thinks, written for a person to read.
  It is the thing you open the folder to see. Nothing in this system ever
  stores a fact only in a header field that the body does not also say in
  words — if the body reads like a database row, the memory is wrong.
* The **header** is machine affordance: the tags and keys that let
  :mod:`app.memory.index` route a lookup straight to this file without
  reading any other, plus the bookkeeping (importance, review dates) that
  decides what gets forgotten.

The header parser is hand-rolled rather than pulling in PyYAML. It supports
exactly what the header needs — ``key: scalar`` and ``key: [a, b, c]`` — and
is forgiving of everything else, because a header that fails to parse must
degrade to "no metadata" and still hand you the body. Losing a memory
because a model wrote a stray colon would be a much worse failure than
ignoring one malformed line.

Writes are atomic (temp file + ``os.replace``) so a process killed mid-write
during a Railway redeploy leaves either the old file or the new one, never a
truncated one.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator

import structlog

from app.memory.paths import MemoryPathError, absolute, memory_root, safe_relpath

log = structlog.get_logger(__name__)

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

#: Header keys with meaning to the rest of the system. Anything else a model
#: writes is preserved verbatim in ``extra`` rather than dropped — a memory
#: is allowed to carry a field this code has not been taught yet.
_KNOWN_KEYS = {
    "title",
    "kind",
    "tags",
    "keys",
    "importance",
    "confidence",
    "created",
    "updated",
    "source",
    "evidence",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str:
    return (value or _utcnow()).isoformat(timespec="seconds")


@dataclass(slots=True)
class MemoryFile:
    """One memory: its path, its header, and the prose."""

    path: str
    title: str = ""
    kind: str = "note"
    body: str = ""
    tags: list[str] = field(default_factory=list)
    #: Exact lookup handles — mints, creator wallets, tickers, narrative
    #: slugs. These are what make retrieval O(1) instead of a text scan; see
    #: :mod:`app.memory.index`.
    keys: list[str] = field(default_factory=list)
    importance: float = 0.5
    confidence: str = "medium"
    created: str = ""
    updated: str = ""
    source: str = ""
    evidence: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def section(self) -> str:
        return self.path.split("/", 1)[0]

    @property
    def content_hash(self) -> str:
        """Hash of the rendered file, used to skip no-op Firestore snapshots."""
        return sha256(self.render().encode("utf-8")).hexdigest()

    def summary(self, limit: int = 240) -> str:
        """First meaningful prose line, for listings and digests."""
        for line in self.body.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped and not stripped.startswith(("-", "*", "|", ">")):
                return stripped[:limit]
        return self.body.strip()[:limit]

    def render(self) -> str:
        header = [
            "---",
            f"title: {self.title}",
            f"kind: {self.kind}",
            f"tags: [{', '.join(self.tags)}]",
            f"keys: [{', '.join(self.keys)}]",
            f"importance: {self.importance:.2f}",
            f"confidence: {self.confidence}",
            f"created: {self.created or _iso(None)}",
            f"updated: {self.updated or _iso(None)}",
        ]
        if self.source:
            header.append(f"source: {self.source}")
        if self.evidence:
            header.append(f"evidence: {self.evidence}")
        for key, value in self.extra.items():
            header.append(f"{key}: {value}")
        header.append("---")
        return "\n".join(header) + "\n\n" + self.body.strip() + "\n"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "section": self.section,
            "title": self.title,
            "kind": self.kind,
            "tags": list(self.tags),
            "keys": list(self.keys),
            "importance": self.importance,
            "confidence": self.confidence,
            "created": self.created,
            "updated": self.updated,
            "source": self.source,
            "evidence": self.evidence,
            "summary": self.summary(),
        }


def _parse_scalar(raw: str) -> Any:
    value = raw.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip("'\"") for item in inner.split(",") if item.strip()]
    return value.strip("'\"")


def parse(path: str, text: str) -> MemoryFile:
    """Split a raw file into header and body. Never raises on bad headers."""
    match = _FRONTMATTER_RE.match(text)
    meta: dict[str, Any] = {}
    body = text
    if match:
        body = text[match.end() :]
        for line in match.group(1).splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, sep, raw = line.partition(":")
            if not sep:
                continue
            meta[key.strip()] = _parse_scalar(raw)

    def as_list(key: str) -> list[str]:
        value = meta.get(key)
        if isinstance(value, list):
            return [str(v) for v in value if str(v).strip()]
        if isinstance(value, str) and value.strip():
            return [v.strip() for v in value.split(",") if v.strip()]
        return []

    try:
        importance = float(meta.get("importance") or 0.5)
    except (TypeError, ValueError):
        importance = 0.5

    return MemoryFile(
        path=path,
        title=str(meta.get("title") or Path(path).stem.replace("-", " ")),
        kind=str(meta.get("kind") or path.split("/", 1)[0]),
        body=body.strip(),
        tags=as_list("tags"),
        keys=as_list("keys"),
        importance=max(0.0, min(1.0, importance)),
        confidence=str(meta.get("confidence") or "medium"),
        created=str(meta.get("created") or ""),
        updated=str(meta.get("updated") or ""),
        source=str(meta.get("source") or ""),
        evidence=str(meta.get("evidence") or ""),
        extra={k: v for k, v in meta.items() if k not in _KNOWN_KEYS},
    )


class MemoryStore:
    """The filesystem side of memory. Stateless — safe to construct anywhere.

    Deliberately knows nothing about the search index or the Firestore
    snapshot. :mod:`app.memory.service` is what composes the three, so that
    a plain file read never drags in a database connection and a test can
    exercise the file format on a temp directory alone.
    """

    def exists(self, relpath: str) -> bool:
        try:
            return absolute(relpath).is_file()
        except MemoryPathError:
            return False

    def read(self, relpath: str) -> MemoryFile | None:
        try:
            target = absolute(relpath)
        except MemoryPathError:
            return None
        if not target.is_file():
            return None
        try:
            return parse(safe_relpath(relpath), target.read_text(encoding="utf-8"))
        except OSError:
            log.warning("memory_read_failed", path=relpath, exc_info=True)
            return None

    def write(self, memory: MemoryFile) -> MemoryFile:
        """Persist a memory, atomically, stamping ``created``/``updated``."""
        target = absolute(memory.path)
        memory.path = safe_relpath(memory.path)
        now = _iso(None)
        existing = self.read(memory.path)
        memory.created = memory.created or (existing.created if existing else now)
        memory.updated = now

        target.parent.mkdir(parents=True, exist_ok=True)
        rendered = memory.render()
        # Temp file in the same directory so os.replace stays an atomic
        # rename rather than a cross-device copy.
        handle, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(rendered)
            os.replace(tmp_name, target)
        except BaseException:
            with_suppressed = Path(tmp_name)
            if with_suppressed.exists():
                with_suppressed.unlink(missing_ok=True)
            raise
        return memory

    def append(self, relpath: str, text: str, *, heading: str | None = None) -> MemoryFile:
        """Add to a memory without rewriting what is already there.

        The append path exists because rewriting a whole file through an LLM
        every cycle is both expensive and lossy — it re-generates prose that
        was already fine and quietly drops details it did not think to carry
        forward. Daily logs and creator dossiers grow by appending; only
        ``core/`` files are rewritten wholesale, because those are supposed
        to be a current view rather than a history.
        """
        memory = self.read(relpath) or MemoryFile(
            path=safe_relpath(relpath), title=Path(relpath).stem.replace("-", " ")
        )
        block = text.strip()
        if heading:
            block = f"### {heading}\n\n{block}"
        memory.body = (memory.body.rstrip() + "\n\n" + block).strip()
        return self.write(memory)

    def delete(self, relpath: str) -> bool:
        try:
            target = absolute(relpath)
        except MemoryPathError:
            return False
        if not target.is_file():
            return False
        target.unlink()
        return True

    def iter_paths(self, section: str | None = None) -> Iterator[str]:
        """Every memory file, as validated relative paths, sorted."""
        root = memory_root()
        base = root / section if section else root
        if not base.is_dir():
            return
        for path in sorted(base.rglob("*.md")):
            rel = path.relative_to(root).as_posix()
            try:
                yield safe_relpath(rel)
            except MemoryPathError:
                # A stray .md the operator dropped in the root, or in a
                # folder Annie does not own. Visible in the directory, simply
                # not part of her memory.
                continue

    def load_all(self, section: str | None = None) -> list[MemoryFile]:
        loaded = [self.read(p) for p in self.iter_paths(section)]
        return [m for m in loaded if m is not None]

    def tree(self) -> dict[str, list[dict[str, Any]]]:
        """Section -> file listings, for the website's memory browser."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for memory in self.load_all():
            grouped.setdefault(memory.section, []).append(memory.to_dict())
        for entries in grouped.values():
            entries.sort(key=lambda e: e.get("updated") or "", reverse=True)
        return grouped
