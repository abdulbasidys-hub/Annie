"""First-boot setup: the empty notebook Annie starts with.

Called once at startup. It creates the section folders, writes a README that
explains the filing system to whoever opens the directory, and seeds the four
``core/`` files with honest placeholders — placeholders that say "nothing
observed yet", never invented content. A market model asserting things Annie
has not seen would be the single most damaging file in this system, because
every later cycle reads ``core/`` as established belief.

Everything here is idempotent: existing files are never overwritten, so a
redeploy against a healthy Volume changes nothing.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.memory import index, service, snapshot
from app.memory.files import MemoryFile, MemoryStore
from app.memory.paths import SECTIONS, memory_root

log = structlog.get_logger(__name__)

README = """# Annie's memory

This folder is what Annie knows. It is plain markdown on purpose — open any
file and read it; nothing here needs an app to make sense of it.

She writes to it every cycle. What she keeps is deliberately a small
fraction of what she sees: roughly sixteen thousand tokens launch on Solana
every day, and almost none of them are worth remembering. The filtering is
the product. A folder that grew a file per launch would be the data dump
this system was rebuilt to stop being.

## The sections

{sections}

## How she finds things

`annie.db` next to this README is a SQLite file holding two derived things:
a ledger of raw sightings and creator movements (cheap, local, pruned), and
a search index over these markdown files. Both are rebuildable — delete it
and it comes back on the next start. The markdown is the truth.

Retrieval is keys first, text second. Every file declares the exact handles
it is about (mint addresses, creator wallets, tickers) in its `keys:`
header, so looking up a contract address is one index probe, not a scan.
Full-text search is only used for fuzzy questions.

## Reading it from elsewhere

- The website's Memory page browses and renders these files.
- The Telegram/Discord bots search them — ask for a CA or a wallet directly.
- `python -m tools.memory_pull` copies them from the running deployment down
  to your machine.
"""

CORE_SEEDS: dict[str, dict[str, Any]] = {
    "core/market-model.md": {
        "title": "Market model",
        "importance": 1.0,
        "tags": ["core", "model"],
        "body": """How Annie currently understands the Solana memecoin market.

This file is rewritten in place, not appended to — it is what she thinks
*now*, and the daily/weekly files are the record of how she got here.

## What launches

_Nothing observed yet. This fills in after the first full cycle._

## What actually moves

_Nothing observed yet._

## What dies

_Nothing observed yet._

## Standing beliefs

_None yet. A belief only belongs here once it has survived more than one
week's evidence; before that it lives in `notes/` as an observation._
""",
    },
    "core/whats-working.md": {
        "title": "What's working right now",
        "importance": 1.0,
        "tags": ["core", "playbook"],
        "body": """The current, short-lived edge: themes, name shapes, launchpads and
timing that are outperforming *this week*.

Deliberately separate from `core/market-model.md`. The model is slow and
should change reluctantly; this file is supposed to churn, because what
works in memecoins has a half-life measured in days. Anything here older
than two weeks without fresh evidence should be deleted, not defended.

_Nothing observed yet._
""",
    },
    "core/open-questions.md": {
        "title": "Open questions",
        "importance": 0.8,
        "tags": ["core", "questions"],
        "body": """Things Annie has noticed but cannot yet explain, and what evidence
would settle each one.

Kept explicitly so that a pattern she is unsure about does not get quietly
promoted into the market model just because it kept showing up. A question
leaves this file in one of two directions: into `core/market-model.md` with
the evidence that settled it, or deleted because it turned out to be noise.

_Nothing open yet._
""",
    },
    "core/instructions.md": {
        "title": "Standing instructions",
        "importance": 1.0,
        "tags": ["core", "instructions"],
        "body": """Things the operator has told me to keep doing.

This file is different from every other one. It is not what I observed — it
is what I have been *told*, and I read it at the start of every cycle and
every conversation. An instruction here outranks my own judgement about what
is worth writing down.

The operator adds to it by saying so in chat: "from now on, keep track of X",
"whenever you see Y, write it in Z", "stop bothering with W". I write the
instruction here in their words, and then I follow it.

_No standing instructions yet._
""",
    },
    "core/watchlist.md": {
        "title": "Watchlist",
        "importance": 0.9,
        "tags": ["core", "watchlist"],
        "body": """Who and what Annie is actively paying attention to.

Creator wallets that launch often or have produced a winner, narratives
mid-run, and specific tokens still in play. Everything listed here gets
priority in the watch loop — tokens by these creators are re-priced
regardless of market cap.

## Creators

_None tracked yet._

## Narratives

_None tracked yet._

## Tokens

_None tracked yet._
""",
    },
}


async def ensure_memory_ready(*, restore_from_snapshot: bool = True) -> dict[str, Any]:
    """Prepare the memory directory, restore if empty, index, and report.

    Order matters. Restore runs *before* seeding, so a deployment whose
    Volume was wiped gets its real memory back rather than a fresh set of
    empty core files sitting on top of a snapshot that would then never be
    restored (the emptiness check would see the seeds and decline).
    """
    root = memory_root()
    for section in SECTIONS:
        (root / section).mkdir(parents=True, exist_ok=True)

    restored: dict[str, Any] = {"restored": 0, "reason": "skipped"}
    if restore_from_snapshot:
        restored = await snapshot.restore()

    seeded = _seed_files()
    indexed = index.sync_if_stale()

    report = {
        "root": str(root),
        "restored": restored,
        "seeded": seeded,
        "indexed": indexed,
        "index": index.stats(),
    }
    log.info("memory_ready", **{k: v for k, v in report.items() if k != "index"})
    return report


def _seed_files() -> list[str]:
    """Write the README and any missing core file. Never overwrites."""
    store = MemoryStore()
    created: list[str] = []

    readme = memory_root() / "README.md"
    if not readme.exists():
        sections = "\n".join(f"- **`{name}/`** — {desc}" for name, desc in SECTIONS.items())
        readme.write_text(README.format(sections=sections), encoding="utf-8")
        created.append("README.md")

    for path, seed in CORE_SEEDS.items():
        if store.exists(path):
            continue
        store.write(
            MemoryFile(
                path=path,
                title=seed["title"],
                kind="core",
                body=seed["body"],
                tags=list(seed["tags"]),
                importance=float(seed["importance"]),
                confidence="high",
                source="bootstrap",
            )
        )
        created.append(path)

    if created:
        log.info("memory_seeded", files=created)
    return created


def sync_watchlist_section(*, creators: list[str], narratives: list[str], tokens: list[str]) -> None:
    """Rewrite ``core/watchlist.md`` from the ledger's current tracked set.

    Deterministic, so it costs nothing and cannot drift from the ledger the
    watch loop actually prioritises by. The LLM never edits this file; it
    reads it.
    """
    def block(title: str, items: list[str]) -> str:
        if not items:
            return f"## {title}\n\n_None tracked yet._"
        return f"## {title}\n\n" + "\n".join(f"- {item}" for item in items)

    body = "\n\n".join(
        [
            "Who and what Annie is actively paying attention to. Maintained "
            "automatically from the ledger — tokens by these creators are "
            "re-priced regardless of market cap.",
            block("Creators", creators),
            block("Narratives", narratives),
            block("Tokens", tokens),
        ]
    )
    store = MemoryStore()
    existing = store.read("core/watchlist.md")
    memory = existing or MemoryFile(path="core/watchlist.md", title="Watchlist", kind="core")
    memory.body = body
    memory.importance = 0.9
    memory.keys = sorted({*creators, *tokens})
    store.write(memory)
    index.index_file(memory)


def standing_instructions() -> str:
    """What the operator has told Annie to keep doing, as raw text.

    Loaded into every cycle and every chat turn rather than retrieved,
    because an instruction that only surfaces when something happens to
    match it is not a standing instruction. It is small by design — if this
    file ever grows past a screen, the operator is using it as a notebook
    rather than a directive list, and the cycle prompt will say so.
    """
    memory = MemoryStore().read("core/instructions.md")
    if memory is None:
        return ""
    body = memory.body.strip()
    if not body or "_No standing instructions yet._" in body:
        return ""
    return body


__all__ = [
    "ensure_memory_ready",
    "standing_instructions",
    "sync_watchlist_section",
    "service",
]
