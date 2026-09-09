"""The learning step: one bounded model call that edits Annie's memory.

Everything before this point in a cycle is free. This is the only paid part,
and it is deliberately shaped like a person closing their notebook at the end
of a session rather than like an ETL job:

* It sees a **digest**, never raw data — a few thousand characters covering
  what moved, who moved it, and what she already wrote about any of it.
* It returns **edits**, not a document — a small set of appends, rewrites and
  deletions against specific files, capped at :data:`MAX_EDITS`.
* It is explicitly allowed, and encouraged, to **return nothing**. Most
  six-hour windows in this market contain no durable lesson. A system that
  must produce a finding every cycle produces mostly noise, and noise in
  memory is worse than a gap because later cycles read it as evidence.

The deletions matter as much as the writes. "Forget this, it turned out to be
wrong" is how a memory stays smaller than the market it is watching, and it
is the operation a database-shaped system never performs.

**Guardrails.** The model chooses paths, and paths are untrusted:
:func:`app.memory.paths.safe_relpath` bounds them to known sections, deletes
are refused for ``core/`` (which is rewritten, never removed) and for
anything outside the sections it is allowed to prune, and every edit is
applied individually so one malformed entry cannot lose the rest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

from app.annie import voice
from app.config import Settings
from app.memory import digest as digest_module
from app.memory import service
from app.memory.paths import MemoryPathError, safe_relpath
from app.providers.registry import ProviderRegistry

log = structlog.get_logger(__name__)

#: Hard cap on edits per cycle. Not a formatting preference — it is the
#: mechanism that stops memory growing linearly with time. Four cycles a day
#: at six edits is already 24 memory changes daily; more than that is not
#: learning, it is transcription.
MAX_EDITS = 6

#: Sections the model may delete from. ``core/`` is excluded because those
#: four files are rewritten in place and their absence would break every
#: later cycle's context; ``daily``/``weekly``/``monthly`` are excluded
#: because they are the historical record that rollups read.
DELETABLE_SECTIONS = frozenset({"notes", "narratives", "tokens", "playbook"})

SYSTEM_PROMPT = """You are Annie, an agent who watches the Solana memecoin market
continuously and keeps a notebook.

You have just been shown one window of market activity, plus the parts of your
own notebook that relate to it. Decide what — if anything — is worth changing
in the notebook.

Think like a trader who has been watching this market for months, not like a
process that logs events. Specifically:

- Most windows teach you nothing. Returning zero edits is the correct and
  common answer. Do not manufacture a lesson to fill the response.
- Write about *patterns and causes*, not counts. "Cat-themed names are
  outperforming dogs 3:1 among $250k+ tokens this week, third week running"
  is memory. "47 tokens qualified" is not — that number is already recorded
  deterministically and does not need you.
- A single window is never enough to change a standing belief. Repetition
  across windows is. If something looks true but you have seen it once, put
  it in `notes/` or `core/open-questions.md`, not `core/market-model.md`.
- Delete things. If a note you wrote is contradicted by what you are now
  seeing, or has gone stale, remove it. A smaller notebook you trust beats a
  larger one you do not.
- When you write about a token, always include its contract address, and the
  creator wallet if known. When you write about a creator, always include the
  wallet. These are how the notebook gets searched later.
- Never state as fact something the digest does not show. If you are
  inferring, say so in the text.
- If the digest ends with standing instructions from the operator, those are
  not suggestions. They were given to you directly and they outrank your own
  judgement about what is worth keeping. Follow them this cycle, even when
  the thing they ask for would not have met your own bar.

Edit operations:
- `append`: add a paragraph to an existing file (or create it). Use for
  `daily/`, `creators/`, `tokens/`, `narratives/` — anything cumulative.
- `rewrite`: replace a file's whole body. Use ONLY for `core/` files, which
  are meant to reflect what you currently think rather than a history.
- `delete`: remove a file that is wrong or stale. Allowed in
  notes/, narratives/, tokens/, playbook/ only.

Sections available: core (standing beliefs — market-model, whats-working,
open-questions), daily, weekly, monthly, creators (one file per wallet),
tokens (one per notable mint), narratives, playbook (what has actually
worked), notes (loose thinking)."""

EDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["headline", "edits", "watch"],
    "properties": {
        "headline": {
            "type": "string",
            "description": "One sentence on what this window actually showed, "
            "in your own voice — this is the line that opens the brief the "
            "operator reads, so it should sound like you and not like a "
            "status field. Say 'nothing notable' when that is the truth.",
        },
        "edits": {
            "type": "array",
            "maxItems": MAX_EDITS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["op", "path", "title", "text", "tags", "keys", "importance"],
                "properties": {
                    "op": {"type": "string", "enum": ["append", "rewrite", "delete"]},
                    "path": {
                        "type": "string",
                        "description": "section/name.md, e.g. creators/7xKX....md",
                    },
                    "title": {"type": "string"},
                    "text": {
                        "type": "string",
                        "description": "Markdown prose. Empty for delete.",
                    },
                    "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 8,
                        "description": "Exact lookup handles: mints, creator wallets, tickers.",
                    },
                    "importance": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        },
        "watch": {
            "type": "object",
            "additionalProperties": False,
            "required": ["creators", "narratives"],
            "properties": {
                "creators": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
                "narratives": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
            },
        },
    },
}


@dataclass(slots=True)
class LearnResult:
    headline: str = ""
    applied: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    watch_creators: list[str] = field(default_factory=list)
    watch_narratives: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    skipped: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "applied": self.applied,
            "rejected": self.rejected,
            "watch_creators": self.watch_creators,
            "watch_narratives": self.watch_narratives,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "skipped": self.skipped,
        }


async def learn_from_window(
    registry: ProviderRegistry,
    settings: Settings,
    *,
    window_hours: int = 6,
    now: datetime | None = None,
) -> LearnResult:
    """Run one cycle's learning pass. At most one model call."""
    now = now or datetime.now(timezone.utc)

    if not settings.is_available("ai"):
        return LearnResult(skipped="ai not configured")

    digest = digest_module.build(window_hours=window_hours, now=now)
    if digest.is_empty:
        # Nothing moved. Costing a model call to be told so is exactly the
        # kind of spend this rewrite exists to remove.
        log.info("learn_skipped_quiet_window", window_hours=window_hours)
        return LearnResult(headline="Quiet window — nothing moved.", skipped="quiet window")

    client = await registry.reasoning.raw_client()
    try:
        response = await client.chat.completions.create(
            model=settings.openai_reasoning_model,
            messages=[
                {"role": "system", "content": voice.prefix(SYSTEM_PROMPT)},
                {"role": "user", "content": digest.render()},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "memory_edits", "strict": True, "schema": EDIT_SCHEMA},
            },
            max_completion_tokens=2000,
            temperature=0.3,
            reasoning_effort="none",
        )
    except Exception as exc:
        log.error("learn_call_failed", exc_info=True)
        return LearnResult(skipped=f"model call failed: {exc}"[:300])

    usage = getattr(response, "usage", None)
    try:
        payload = json.loads(response.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        log.warning("learn_response_unparseable")
        payload = {}

    result = LearnResult(
        headline=str(payload.get("headline") or "").strip(),
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
    )

    for edit in (payload.get("edits") or [])[:MAX_EDITS]:
        outcome = await _apply_edit(edit, now=now)
        (result.applied if outcome.get("ok") else result.rejected).append(outcome)

    watch = payload.get("watch") or {}
    result.watch_creators = [str(w) for w in (watch.get("creators") or [])][:10]
    result.watch_narratives = [str(n) for n in (watch.get("narratives") or [])][:10]

    log.info(
        "learn_complete",
        applied=len(result.applied),
        rejected=len(result.rejected),
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )
    return result


async def _apply_edit(edit: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """Validate and apply one edit. Never raises — a bad edit is reported, not fatal."""
    op = str(edit.get("op") or "").lower()
    raw_path = str(edit.get("path") or "")

    try:
        path = safe_relpath(raw_path)
    except MemoryPathError as exc:
        return {"ok": False, "path": raw_path, "op": op, "reason": str(exc)}

    section = path.split("/", 1)[0]
    text = str(edit.get("text") or "").strip()
    title = str(edit.get("title") or "").strip()
    tags = [str(t) for t in (edit.get("tags") or []) if str(t).strip()]
    keys = [str(k) for k in (edit.get("keys") or []) if str(k).strip()]
    try:
        importance = max(0.0, min(1.0, float(edit.get("importance") or 0.5)))
    except (TypeError, ValueError):
        importance = 0.5

    try:
        if op == "delete":
            if section not in DELETABLE_SECTIONS:
                return {
                    "ok": False, "path": path, "op": op,
                    "reason": f"deleting from {section}/ is not allowed "
                              f"(only {sorted(DELETABLE_SECTIONS)})",
                }
            removed = await service.forget(path)
            return {"ok": removed, "path": path, "op": op,
                    "reason": None if removed else "file did not exist"}

        if not text:
            return {"ok": False, "path": path, "op": op, "reason": "empty text"}

        if op == "rewrite":
            if section != "core":
                # Appending is right for everything cumulative; a rewrite of
                # a creator dossier or a daily log would silently discard
                # history the model was not shown and could not restore.
                return {
                    "ok": False, "path": path, "op": op,
                    "reason": "rewrite is only allowed for core/ files; use append",
                }
            await service.write(
                path, body=text, title=title or None, tags=tags, keys=keys,
                importance=max(importance, 0.8), source="cycle-learning",
                evidence=f"window ending {now.isoformat(timespec='minutes')}",
            )
            return {"ok": True, "path": path, "op": op, "reason": None}

        if op == "append":
            await service.append(
                path, text,
                heading=now.strftime("%Y-%m-%d %H:%M UTC"),
                title=title or None, tags=tags, keys=keys, importance=importance,
            )
            return {"ok": True, "path": path, "op": op, "reason": None}

        return {"ok": False, "path": path, "op": op, "reason": f"unknown op {op!r}"}

    except Exception as exc:
        log.warning("memory_edit_failed", path=path, op=op, exc_info=True)
        return {"ok": False, "path": path, "op": op, "reason": str(exc)[:200]}
