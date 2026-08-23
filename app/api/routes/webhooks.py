"""Helius webhook receiver — the primary discovery mechanism (§5, §20).

See app/pipeline/discovery.py's module docstring for why: Pump.fun's real
transaction volume (~150+ tx/sec, confirmed by direct measurement — 1000
signatures covered 6 seconds) makes signature polling structurally unable
to cover a real time window. Helius pushes a ``CREATE`` event here the
instant one happens instead. The webhook itself is registered once against
Helius's REST API (a one-time setup call, not something this route does —
see README for how), filtered to ``transactionTypes: ["CREATE"]`` and
scoped to the launchpad program IDs in ``KNOWN_LAUNCHPAD_PROGRAMS``.

Confirmed against a real Helius delivery on 2026-08-22: a genuine Pump.fun
create transaction classifies as ``type: "CREATE"``, ``source: "PUMP_FUN"``
in Helius's enhanced parser — not ``TOKEN_MINT``, which was the initial
(wrong) guess and produced zero deliveries. The mint always showed up in
``tokenTransfers[0].mint`` on the real payload, so the extraction order
below (``tokenTransfers`` first, ``accountData`` fallback) needed no change
once the filter was corrected.

**Authenticated by a shared secret, not a session.** Helius calls this
directly — there is no logged-in user, so this route is deliberately NOT
behind ``app.auth.require_auth`` (see its registration in app/main.py). The
secret is whatever was supplied as `authHeader` when the webhook was
created; anyone who doesn't send it back gets a 401 without their payload
being read.

**Payload parsing is defensive, not confident.** Helius's enhanced-webhook
schema for `CREATE` isn't pinned down in one authoritative place the way
`getSignaturesForAddress` is — confirmed correct against one real delivery,
not an official spec. This checks the most likely
locations for a mint address in order (`tokenTransfers`, then
`accountData[].tokenBalanceChanges`) and logs — never crashes — on a shape
it doesn't recognise. If mints stop appearing despite the webhook visibly
firing (check `requests_24h` for "helius" on System Health), the fix is
almost certainly here: log a raw payload once and adjust the extraction to
match what Helius is actually sending.

**A Firestore failure on one event must not fail the whole delivery.** Once
the ``CREATE`` filter above started actually working, real production volume
hit Firestore for the first time — and surfaced a ``RESOURCE_EXHAUSTED /
429 Quota exceeded`` from ``create_discovered_token``'s read, which was an
unhandled exception that 500'd the entire request (dropping every other
event in that delivery, not just the one that failed). Each event's write is
now wrapped individually, logged as ``helius_webhook_write_failed`` rather
than crashing. Repeated occurrences of that log line mean Firestore's plan
quota was hit (Firebase Console -> Firestore Database -> Usage tab shows
today's reads/writes against the plan's cap) — not a bug in this handler.
The Spark (free) plan's fixed daily caps do not fit a webhook that fires on
every real Pump.fun creation; Blaze (pay-as-you-go) removes the cap, and the
actual per-operation cost at this volume is cents, not dollars.
"""

from __future__ import annotations

import hmac
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.config import Settings, get_settings
from app.db.repo import FirestoreRepo, get_repo
from app.pipeline.discovery import record_launch
from app.providers.helius import KNOWN_LAUNCHPAD_PROGRAMS
from app.providers.types import Provenance, TokenLaunch

log = structlog.get_logger(__name__)
router = APIRouter()


def _verify_secret(settings: Settings, authorization: str | None) -> None:
    configured = settings.helius_webhook_secret.strip()
    if not configured:
        raise HTTPException(status_code=401, detail="Webhook secret not configured.")
    given = (authorization or "").strip()
    if given.lower().startswith("bearer "):
        given = given[7:].strip()
    if not hmac.compare_digest(given, configured):
        raise HTTPException(status_code=401, detail="Invalid webhook secret.")


@router.post("/helius")
async def helius_webhook(
    request: Request,
    repo: FirestoreRepo = Depends(get_repo),
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    _verify_secret(settings, authorization)

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body was not valid JSON.")

    events = payload if isinstance(payload, list) else [payload]

    created = 0
    unparsed = 0
    failed = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        launch = _parse_token_mint(event)
        if launch is None:
            unparsed += 1
            log.warning(
                "helius_webhook_unparsed",
                signature=event.get("signature"),
                keys=sorted(event.keys()),
            )
            continue
        try:
            if await record_launch(repo, launch):
                created += 1
        except Exception:
            # A Firestore-side failure (quota, transient outage) on one event
            # must not crash the whole batch or bubble up as an unhandled 500 —
            # that would both drop every other event in this delivery and give
            # Helius an ambiguous failure signal to retry against. Logged, not
            # silent: check for repeated `helius_webhook_write_failed` entries,
            # which almost always means Firestore quota was exceeded (Firebase
            # Console -> Firestore -> Usage) rather than a code bug here.
            failed += 1
            log.error("helius_webhook_write_failed", signature=launch.signature, exc_info=True)

    log.info(
        "helius_webhook_received", events=len(events), created=created, unparsed=unparsed, failed=failed
    )
    return {"received": len(events), "created": created, "unparsed": unparsed, "failed": failed}


def _parse_token_mint(event: dict[str, Any]) -> TokenLaunch | None:
    signature = event.get("signature")
    timestamp = event.get("timestamp")
    launched_at = (
        datetime.fromtimestamp(timestamp, tz=timezone.utc) if isinstance(timestamp, (int, float)) else None
    )
    fee_payer = event.get("feePayer") if isinstance(event.get("feePayer"), str) else None

    mint: str | None = None
    for transfer in event.get("tokenTransfers") or []:
        if isinstance(transfer, dict) and transfer.get("mint"):
            mint = transfer["mint"]
            break
    if mint is None:
        for account in event.get("accountData") or []:
            if not isinstance(account, dict):
                continue
            for change in account.get("tokenBalanceChanges") or []:
                if isinstance(change, dict) and change.get("mint"):
                    mint = change["mint"]
                    break
            if mint:
                break
    if not mint:
        return None

    account_addresses = {
        a.get("account") for a in (event.get("accountData") or []) if isinstance(a, dict)
    }
    launchpad_slug = next(
        (slug for program_id, slug in KNOWN_LAUNCHPAD_PROGRAMS.items() if program_id in account_addresses),
        None,
    )

    return TokenLaunch(
        mint=mint,
        provenance=Provenance(
            provider="helius",
            operation="webhook_token_mint",
            observed_at=datetime.now(timezone.utc),
            raw_reference=signature,
        ),
        creator_wallet=fee_payer,
        launchpad_slug=launchpad_slug,
        launched_at=launched_at,
        signature=signature,
    )
