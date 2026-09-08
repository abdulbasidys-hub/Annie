"""End-to-end smoke test of the memory system against a throwaway directory.

Run it with ``python -m tools.memory_smoke``. It creates a temp memory root,
drives the whole loop — ingest launches, price them, qualify one, write
memories, compute signals, build a digest, search — and prints what each step
cost. No network, no Firestore, no model.

It exists because the important claim of this rewrite ("a cycle is cheap") is
a claim about *sizes*, and sizes are only believable when measured. The
digest character count it prints is the real input a cycle would send.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone


def main() -> int:
    # Windows consoles default to cp1252 and this script prints file content.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    root = tempfile.mkdtemp(prefix="annie-smoke-")
    os.environ["ANNIE_MEMORY_DIR"] = root
    os.environ.setdefault("AUTH_SECRET", "smoke-test-secret")
    os.environ.setdefault("FIREBASE_PROJECT_ID", "smoke-test")

    from app.config import get_settings

    get_settings.cache_clear()

    from app.memory import bootstrap, db, digest, index, ledger, service, signals
    from app.memory.paths import memory_root

    print(f"memory root: {memory_root()}")

    # -- bootstrap ------------------------------------------------------------
    seeded = bootstrap._seed_files()
    print(f"seeded {len(seeded)} files: {seeded}")
    index.reindex_all()

    # -- ingest ---------------------------------------------------------------
    now = datetime.now(timezone.utc)
    creators = [f"Wa11et{i:038d}" for i in range(1, 6)]
    for i in range(200):
        ledger.record_launch(
            mint=f"Mint{i:040d}",
            creator=creators[i % len(creators)],
            launchpad="pumpfun" if i % 3 else "raydium-launchlab",
            symbol=f"CAT{i}" if i % 4 == 0 else f"DOG{i}",
            name=("Quantum Cat " if i % 4 == 0 else "Space Dog ") + str(i),
            launched_at=now - timedelta(minutes=i),
        )
    stats = ledger.stats()
    print(f"ingested: {stats['sightings_total']} sightings, {stats['creators_total']} creators")
    assert stats["sightings_total"] == 200, stats

    # Re-ingesting the same mints must not inflate launch counts.
    before = ledger.get_creator(creators[0])["launches"]
    ledger.record_launch(mint="Mint" + "0" * 36 + "0000", creator=creators[0])
    after = ledger.get_creator(creators[0])["launches"]
    print(f"duplicate ingest guard: launches {before} -> {after}")
    assert before == after, "a repeat sighting inflated the creator's launch count"

    # -- pricing / qualification ---------------------------------------------
    due = ledger.due_for_check(50)
    print(f"due for check: {len(due)}")
    for n, sighting in enumerate(due[:30]):
        cap = 400_000 if n < 4 else 2_000
        ledger.record_price(
            mint=sighting.mint, market_cap=cap, liquidity=25_000,
            tier=250_000 if cap >= 250_000 else None,
        )
    qualified = ledger.qualified_in_window(now - timedelta(days=1), now + timedelta(minutes=1))
    print(f"qualified: {len(qualified)}")
    assert qualified, "nothing qualified"

    # -- memory writes --------------------------------------------------------
    async def write_memories() -> None:
        from app.memory.rollup import update_creator_dossier, write_token_memory

        for token in qualified:
            await write_token_memory(token.mint)
        for wallet in creators[:2]:
            await update_creator_dossier(wallet, reason="smoke test")
        await service.write(
            "notes/cat-theme.md",
            body="Cat-themed names are showing up disproportionately among the "
                 "tokens clearing $250k this window. Worth another week before "
                 "promoting to the market model.",
            title="Cat theme",
            tags=["theme", "cat"],
            keys=["cat", "quantum cat"],
            importance=0.6,
            mirror=False,
        )

    asyncio.run(write_memories())
    print(f"memory files: {index.stats()['files']} ({index.stats()['keys']} keys)")

    # -- retrieval ------------------------------------------------------------
    target = qualified[0].mint
    hits = index.by_key(target)
    print(f"key lookup for CA {target[:12]}: {len(hits)} hit(s) -> {[h.path for h in hits]}")
    assert hits, "a qualified token's CA did not resolve to its memory file"

    wallet_hits = index.by_key(creators[0])
    print(f"key lookup for wallet: {len(wallet_hits)} hit(s) -> {[h.path for h in wallet_hits]}")
    assert wallet_hits, "a tracked creator's wallet did not resolve to its dossier"

    text_hits = index.search("cat theme showing up")
    print(f"text search: {len(text_hits)} hit(s) -> {[h.path for h in text_hits]}")
    assert text_hits, "full-text search found nothing"

    # -- signals --------------------------------------------------------------
    run = signals.recompute()
    print(f"signals: {run.to_dict()}")

    # -- digest ---------------------------------------------------------------
    built = digest.build(window_hours=24)
    rendered = built.render()
    print("-" * 70)
    print(rendered[:1500])
    print("-" * 70)
    print(f"digest: {len(rendered)} chars, approx {len(rendered)//4} input tokens")
    print(f"would call model: {not built.is_empty}")

    # -- forgetting -----------------------------------------------------------
    pruned = ledger.prune(ttl_hours=0)
    print(f"prune: {pruned}")
    remaining = ledger.stats()
    print(f"after prune: {remaining['sightings_total']} sightings kept "
          f"({remaining['qualified_total']} qualified, {remaining['creators_total']} creators intact)")
    assert remaining["qualified_total"] == len(qualified), "prune deleted qualified tokens"
    assert remaining["creators_total"] == len(creators), "prune deleted creators"

    print(f"\nfirestore ops used: {db.counters_today()}")
    print("\nOK — full loop ran with zero Firestore operations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
