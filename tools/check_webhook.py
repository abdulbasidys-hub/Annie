"""Check — and optionally fix — the Helius webhook registration.

    python -m tools.check_webhook --url https://your-app.up.railway.app
    python -m tools.check_webhook --url https://... --repair

The webhook is the single point of failure for the whole system, and every
way it breaks is invisible from the receiving end:

* Helius **auto-disables** a webhook after a sustained run of failures. That
  has already happened to this deployment once, while Railway was down. A
  disabled webhook stays disabled until someone acts.
* A redeploy onto a new domain leaves it **pointing at the old URL**.
* An ``authHeader`` that no longer matches ``HELIUS_WEBHOOK_SECRET`` makes
  every delivery a **401**, which from this side is identical to silence.
* A ``transactionTypes`` list missing ``CREATE`` or ``CREATE_POOL`` silently
  covers **half the market**.

There is also a panel for this on System Health, which is easier. This exists
for the case where the deployment is not reachable yet, or where you want to
see the raw registration.

Reads ``HELIUS_API_KEY`` and ``HELIUS_WEBHOOK_SECRET`` from ``.env`` or the
environment. ``--repair`` is the only thing here that changes anything.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


async def run(args: argparse.Namespace) -> int:
    from app.providers import helius_webhook

    env = {**_load_env(Path(args.env)), **os.environ}
    api_key = env.get("HELIUS_API_KEY", "")
    secret = env.get("HELIUS_WEBHOOK_SECRET", "")

    if not api_key:
        print("HELIUS_API_KEY is not set. Nothing can be checked without it.", file=sys.stderr)
        return 2

    base = (args.url or env.get("VITE_API_BASE_URL") or "").strip()
    if not base:
        print(
            "No deployment URL. Pass --url https://your-app.up.railway.app "
            "(or set VITE_API_BASE_URL in .env).",
            file=sys.stderr,
        )
        return 2

    report = await helius_webhook.inspect(api_key, base_url=base, secret=secret)

    print(f"\nExpecting deliveries at: {report['expected_url']}")
    print(f"State: {report['state']}\n")

    if report["webhooks"]:
        print("Registered on this Helius key:")
        for w in report["webhooks"]:
            print(f"  {w['id']}")
            print(f"    url      {w['url']}")
            print(f"    types    {', '.join(w['transaction_types']) or '(none)'}")
            print(f"    programs {len(w['account_addresses'])}")
            print(
                "    secret   "
                + (
                    "matches"
                    if w["auth_header_matches"]
                    else "set but does NOT match" if w["auth_header_set"] else "not set"
                )
            )
        print()

    if report["ok"]:
        print("Nothing wrong with the registration.")
        print(
            "If deliveries are still not arriving, the webhook may have been "
            "auto-disabled by Helius after a run of failures — that state is not "
            "exposed in this listing. Re-saving it with --repair clears that."
        )
        if not args.repair:
            return 0
    else:
        print("Problems:")
        for problem in report["problems"]:
            print(f"  - {problem}")
        print()

    if not args.repair:
        print("Re-run with --repair to create or correct it.")
        return 1

    try:
        result = await helius_webhook.repair(api_key, base_url=base, secret=secret)
    except helius_webhook.WebhookError as exc:
        print(f"\nRepair failed: {exc}", file=sys.stderr)
        return 1

    print(f"\n{result['action'].title()}: {result['webhook_id']} -> {result['url']}")
    print("Deliveries should begin within a minute or two. Watch 'Launches last "
          "hour' on System Health, or grep the server logs for "
          "`helius_webhook_received`.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", help="The deployment's public base URL.")
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Create the webhook, or correct the existing one in place.",
    )
    parser.add_argument("--env", default=".env", help="Path to the .env file to read.")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
