"""Did the watch-batch fix actually work?

One number decides it: how many coins cross $100k per day.

Before the fix that was 4 to 7, against 36,697 launches — about 8% of what a
market this size really produces. The cause was arithmetic rather than
design: ~9,200 tokens are under six hours old at any moment, and a batch of
900 every ten minutes re-checked each of them about every 102 minutes, while
a Pump.fun coin's whole run is closer to 30. She was sampling on a cycle
longer than the events she was trying to catch.

Run this with no arguments to see where things stand:

    python -m tools.coverage_check

It prints the daily qualification counts either side of the change, so the
question "is this working" has an answer rather than an impression. If the
number has not moved off single digits, the fix did not work and the design
is wrong — which is worth knowing quickly rather than slowly.
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter

import httpx

API = "https://annie-production-2b45.up.railway.app"

#: When the batch went from 900 to 9,000.
FIX_DEPLOYED = "2026-09-17"

#: What the rate was before it, measured. Single digits a day.
BASELINE = {
    "2026-09-13": 7,
    "2026-09-14": 1,
    "2026-09-15": 9,
    "2026-09-16": 6,
    "2026-09-17": 7,
}


async def main() -> int:
    from app.config import get_settings

    settings = get_settings()
    async with httpx.AsyncClient(timeout=90) as client:
        auth = await client.post(
            f"{API}/api/auth/login",
            json={"username": settings.auth_username, "password": settings.auth_password},
        )
        if auth.status_code != 200:
            print("Could not sign in — check AUTH_USERNAME / AUTH_PASSWORD.")
            return 1
        payload = auth.json()
        token = payload.get("access_token") or payload.get("token")
        headers = {"Authorization": f"Bearer {token}"}

        stats = (await client.get(f"{API}/api/ledger/stats", headers=headers)).json()
        ledger = stats["ledger"]

        tokens = (
            await client.get(
                f"{API}/api/tokens?limit=200&hours=2160&qualified_only=true&min_market_cap=0",
                headers=headers,
            )
        ).json()
        items = tokens.get("items") or []
        by_day = Counter(str(t.get("qualified_at"))[:10] for t in items if t.get("qualified_at"))

    print("=" * 58)
    print("  DID THE COVERAGE FIX WORK?")
    print("=" * 58)
    print(f"\n  launches seen in 24h : {ledger['sightings_24h']:,}")
    print(f"  crossed a tier in 24h: {ledger['qualified_24h']}")
    print(f"  on the watchlist     : {ledger['watching']:,}")

    print("\n  Crossings per day")
    print("  " + "-" * 44)
    for day in sorted(by_day):
        n = by_day[day]
        era = "before" if day <= FIX_DEPLOYED else "after "
        bar = "#" * min(n, 50)
        print(f"   {day}  {era}  {n:>4}  {bar}")

    after = [n for d, n in by_day.items() if d > FIX_DEPLOYED]
    if not after:
        print("\n  No full day since the fix yet. Come back tomorrow.")
        return 0

    best = max(after)
    was = max(BASELINE.values())
    print(f"\n  Best day before: {was}   Best day after: {best}")

    # Deliberately blunt. The point of a test is that it can fail.
    if best >= 40:
        print("\n  WORKING. Coverage is where a market this size should be.")
    elif best >= 15:
        print("\n  BETTER, NOT FIXED. Real improvement, still short of the")
        print("  40-100/day a 36,000-launch market should produce.")
    else:
        print("\n  NOT FIXED. Still single digits. The batch size was not the")
        print("  problem, or not the only one — the detection design needs")
        print("  replacing rather than tuning.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
