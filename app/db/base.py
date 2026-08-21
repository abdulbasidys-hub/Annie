"""Shared helpers for the Firestore data layer.

Two correctness rules carried over from the original Postgres design
(``Numeric`` columns, timezone-aware timestamps) and re-implemented for a
document store that has neither:

**Money is a string in Firestore, ``Decimal`` everywhere in Python.** Firestore
numbers are IEEE-754 doubles — exactly the representation error the original
schema used ``Numeric(20, 2)`` to avoid ("a token must not drift across the
$100k line through representation error"). Storing the decimal's ``str()``
instead preserves it exactly; :func:`money_to_doc` / :func:`money_from_doc`
are the only places this conversion happens.

**Ratios stay native floats.** A proportion in [0, 1] needs about six decimal
places of precision (§26); a double has ~15-17 significant digits, so there is
no meaningful precision loss and no reason to pay the string-parsing cost on
every trend read.

**Timestamps are native Firestore ``Timestamp`` values**, round-tripped as
timezone-aware ``datetime`` by the client library directly — better than SQL
here, no helper needed.
"""

from __future__ import annotations

import dataclasses
import re
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar

T = TypeVar("T")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def money_to_doc(value: Decimal | None) -> str | None:
    """``Decimal`` -> exact string for storage. ``None`` means unknown, not 0."""
    if value is None:
        return None
    return str(value)


def money_from_doc(value: Any) -> Decimal | None:
    """Storage string (or legacy number) -> ``Decimal``. Never raises on bad data."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, max_length: int = 60) -> str:
    """Lowercase, ASCII, hyphen-joined identifier suitable as a Firestore doc ID.

    Used for every collection keyed by a natural name (launchpads, narratives,
    trends) instead of an autoincrement integer — Firestore has no sequence
    primitive, and a deterministic slug makes "insert if absent" a single
    ``.get()`` instead of a query, which is both simpler and race-free for the
    common case of two enrichment workers seeing the same new value at once.
    """
    normalised = unicodedata.normalize("NFKD", text)
    normalised = "".join(c for c in normalised if not unicodedata.combining(c))
    slug = _SLUG_RE.sub("-", normalised.lower()).strip("-")
    return (slug or "unknown")[:max_length]


def doc_id_safe(value: str, *, max_length: int = 300) -> str:
    """Sanitise an arbitrary string (a mint, a wallet) for use as a doc ID.

    Firestore forbids ``/`` in a document ID and a few other edge cases (`.`,
    `..`, and IDs matching ``__.*__``). Solana addresses are base58 and never
    hit these, but provider text (URLs, free-form strings) sometimes ends up
    here, so this is defensive rather than load-bearing for the happy path.
    """
    cleaned = value.strip().replace("/", "_")
    if cleaned in ("", ".", "..") or (cleaned.startswith("__") and cleaned.endswith("__")):
        cleaned = f"id-{cleaned}" if cleaned else "id-empty"
    return cleaned[:max_length]


# -----------------------------------------------------------------------------
# Generic dataclass <-> Firestore document conversion
# -----------------------------------------------------------------------------
# Every model in app/db/models/ is a plain dataclass with an optional
# ``_MONEY_FIELDS`` class attribute naming which fields are ``Decimal``. This
# is the one place that distinction is applied, so a model definition stays a
# flat list of fields rather than 20 repetitions of the same conversion.


def to_doc(obj: Any) -> dict[str, Any]:
    """Dataclass instance -> a dict ready for ``DocumentReference.set()``."""
    money_fields: frozenset[str] = getattr(type(obj), "_MONEY_FIELDS", frozenset())
    out: dict[str, Any] = {}
    for f in dataclasses.fields(obj):
        if f.name.startswith("_"):
            continue
        value = getattr(obj, f.name)
        out[f.name] = money_to_doc(value) if f.name in money_fields else value
    return out


def from_doc(cls: type[T], doc_id: str, doc: dict[str, Any], **overrides: Any) -> T:
    """A Firestore document dict -> a dataclass instance.

    ``doc_id`` is threaded in explicitly because the document's own ID is not
    part of its data — Firestore keys the collection, not the payload — so
    every model's dataclass has an id-like field that must come from the
    caller, not from ``doc``.
    """
    money_fields: frozenset[str] = getattr(cls, "_MONEY_FIELDS", frozenset())
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):  # type: ignore[arg-type]
        if f.name.startswith("_") or f.name in overrides:
            continue
        if f.name not in doc:
            continue
        value = doc[f.name]
        kwargs[f.name] = money_from_doc(value) if f.name in money_fields else value
    kwargs.update(overrides)
    return cls(**kwargs)  # type: ignore[call-arg]
