"""Delete the Firestore collections the 2026-09-08 rewrite retired.

    python -m tools.firestore_cleanup                 # dry run — counts only
    python -m tools.firestore_cleanup --delete        # actually delete
    python -m tools.firestore_cleanup --delete --only tokens

Why this exists: downgrading to Spark caps *storage* as well as operations
(1 GiB), and the retired collections are where nearly all of it went —
``tokens`` alone accumulated tens of thousands of documents, each with a
``features`` subcollection of 13-47 more. Nothing reads them any more, but
they still occupy the quota and still show up in the console.

**Deletes cost quota too** (20,000/day on Spark), so this batches, counts as
it goes, and stops at a limit you control. Run it across a few days rather
than trying to clear everything in one pass; it is resumable by design —
just run it again.

Safety:

* Dry run by default. Nothing is deleted without ``--delete``.
* Only the collections in :data:`RETIRED` are touched, and that list is
  explicit — never a wildcard over whatever happens to exist.
* ``settings``, ``bot_sessions``, ``conversations``, ``research_*``,
  ``reports``, ``narratives``, ``launchpads``, ``personality``,
  ``discord_channels`` and ``memory_files`` are deliberately absent. Those
  are still live.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

#: Collections nothing reads any more, with what replaced each.
RETIRED: dict[str, str] = {
    "tokens": "app/memory/ledger.py's `sightings` table (local SQLite, pruned at 48h)",
    "creators": "the ledger's `creators` + `moves` tables",
    "trends": "the ledger's `signals` table (app/memory/signals.py)",
    "anomalies": "surfaced in the cycle digest instead of stored",
    "data_quality": "still written; listed here only if you want a clean slate",
    "memories": "markdown files under ANNIE_MEMORY_DIR",
    "consolidation_runs": "the cycle's own result, kept in local scheduler state",
    "tool_calls": "structlog output only",
    "audit_log": "structlog output only",
    "pipeline_runs": "kept — remove this line if you want it cleared too",
}

#: Subcollections that hang off `tokens/{mint}` and must be deleted with it.
TOKEN_SUBCOLLECTIONS = ("features", "milestones")

#: Firestore's own batch ceiling.
BATCH_SIZE = 400


async def _count(db, name: str) -> int:
    total = 0
    async for _ in db.collection(name).select([]).stream():
        total += 1
    return total


async def _delete_collection(db, name: str, *, limit: int, dry_run: bool) -> int:
    """Delete up to ``limit`` documents. Returns how many were removed."""
    deleted = 0
    while deleted < limit:
        page = [
            snap
            async for snap in db.collection(name)
            .select([])
            .limit(min(BATCH_SIZE, limit - deleted))
            .stream()
        ]
        if not page:
            break

        if dry_run:
            deleted += len(page)
            if len(page) < BATCH_SIZE:
                break
            continue

        batch = db.batch()
        for snap in page:
            if name == "tokens":
                # A document's subcollections are NOT removed with it —
                # deleting the parent would orphan them where nothing can
                # find them again, so they go first.
                for sub in TOKEN_SUBCOLLECTIONS:
                    async for child in snap.reference.collection(sub).select([]).stream():
                        batch.delete(child.reference)
            batch.delete(snap.reference)
        await batch.commit()
        deleted += len(page)
        print(f"    …{deleted}")
    return deleted


async def run(args: argparse.Namespace) -> int:
    from app.db.firestore import dispose_client, get_client

    db = get_client()
    targets = args.only or list(RETIRED)
    unknown = [t for t in targets if t not in RETIRED]
    if unknown:
        print(f"Not a retired collection: {', '.join(unknown)}", file=sys.stderr)
        print(f"Known: {', '.join(RETIRED)}", file=sys.stderr)
        return 2

    mode = "DELETING" if args.delete else "DRY RUN — nothing will be deleted"
    print(f"\n{mode}\n")

    total = 0
    for name in targets:
        print(f"  {name}  ({RETIRED[name]})")
        removed = await _delete_collection(
            db, name, limit=args.limit - total, dry_run=not args.delete
        )
        total += removed
        print(f"    {removed} document(s){'' if args.delete else ' would be deleted'}\n")
        if total >= args.limit:
            print(f"  Reached the {args.limit}-document limit for this run.")
            print("  Run again tomorrow — deletes count against the daily quota too.\n")
            break

    if not args.delete:
        print(f"Total: {total} document(s). Re-run with --delete to remove them.\n")
    else:
        print(f"Deleted {total} document(s).\n")

    await dispose_client()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--delete", action="store_true", help="Actually delete. Off by default.")
    parser.add_argument(
        "--only", action="append", help="Limit to these collections (repeatable)."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=15000,
        help="Stop after this many documents. Deletes count against the plan's "
        "20,000/day cap, so this leaves headroom for the app itself.",
    )
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
