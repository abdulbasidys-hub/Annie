"""Inspecting and repairing the Helius webhook registration.

The webhook is the single point of failure for the whole system: no
deliveries means no sightings, no winners, no signals, no memory — and it
fails *silently*, because a webhook that stops calling looks exactly like a
market where nothing launched.

Worse, the ways it breaks are all invisible from inside this app:

* **Auto-disabled.** Helius disables a webhook after a sustained high
  failure rate. That happened to this deployment once already, while Railway
  was down, and a disabled webhook stays disabled until someone re-enables
  it. Nothing about it is observable from the receiving end.
* **Wrong URL.** A redeploy onto a new domain leaves the registration
  pointing at the old one.
* **Secret mismatch.** ``HELIUS_WEBHOOK_SECRET`` not matching the
  ``authHeader`` the webhook was created with means every delivery is
  rejected with 401 — identical, from the outside, to no delivery at all.
* **Wrong filter.** The ``transactionTypes`` value is per-launchpad and
  empirically determined (``CREATE`` for Pump.fun, ``CREATE_POOL`` for
  Raydium LaunchLab). A registration missing one silently covers half the
  market.

So this asks Helius directly. Everything here is a management call against
``api.helius.xyz/v0/webhooks`` — not the RPC endpoint, and not on any hot
path; it runs when an operator looks at System Health or presses repair.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from app.providers.helius import KNOWN_LAUNCHPAD_PROGRAMS

log = structlog.get_logger(__name__)

BASE_URL = "https://api.helius.xyz/v0/webhooks"

#: The receiving path, which must match app/main.py's router registration.
WEBHOOK_PATH = "/api/webhooks/helius"

#: Confirmed against real deliveries, not guessed: a Pump.fun creation
#: classifies as ``CREATE``, a Raydium LaunchLab creation as ``CREATE_POOL``.
#: Registering only one of these silently covers half the market — see
#: app/api/routes/webhooks.py for how each was established.
TRANSACTION_TYPES: list[str] = ["CREATE", "CREATE_POOL"]

WEBHOOK_TYPE = "enhanced"


class WebhookError(RuntimeError):
    """A management call to Helius failed."""


async def _call(
    method: str, api_key: str, *, path: str = "", json: Any = None
) -> Any:
    if not api_key.strip():
        raise WebhookError("HELIUS_API_KEY is not set, so the webhook cannot be checked.")
    url = f"{BASE_URL}{path}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.request(
            method, url, params={"api-key": api_key.strip()}, json=json
        )
    if response.status_code >= 400:
        raise WebhookError(
            f"Helius returned {response.status_code} for {method} {path or '/'}: "
            f"{response.text[:300]}"
        )
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def expected_url(base: str) -> str:
    return f"{base.rstrip('/')}{WEBHOOK_PATH}"


async def list_webhooks(api_key: str) -> list[dict[str, Any]]:
    found = await _call("GET", api_key)
    return found if isinstance(found, list) else []


async def inspect(api_key: str, *, base_url: str, secret: str) -> dict[str, Any]:
    """What is registered, and what is wrong with it.

    Returns a verdict rather than raw data. "Here are your webhooks" is not
    an answer to "why am I getting nothing" — the useful output is the list
    of specific mismatches, because each one has a different fix.
    """
    want_url = expected_url(base_url)
    want_programs = set(KNOWN_LAUNCHPAD_PROGRAMS)

    try:
        registered = await list_webhooks(api_key)
    except WebhookError as exc:
        return {
            "ok": False,
            "state": "unreachable",
            "problems": [str(exc)],
            "expected_url": want_url,
            "webhooks": [],
        }

    if not registered:
        return {
            "ok": False,
            "state": "not_registered",
            "problems": [
                "No webhook is registered with this Helius key at all. Nothing will "
                "ever arrive until one is created."
            ],
            "expected_url": want_url,
            "webhooks": [],
        }

    # The one pointing at us, if any. Matching on path rather than exact URL
    # so a trailing slash or scheme difference is reported as a mismatch to
    # fix rather than as "not ours".
    ours = next(
        (w for w in registered if WEBHOOK_PATH in str(w.get("webhookURL", ""))), None
    )
    if ours is None:
        return {
            "ok": False,
            "state": "points_elsewhere",
            "problems": [
                f"{len(registered)} webhook(s) exist on this Helius key, but none "
                f"points at {WEBHOOK_PATH}. They are configured for something else."
            ],
            "expected_url": want_url,
            "webhooks": [_summarise(w, secret) for w in registered],
        }

    problems: list[str] = []

    actual_url = str(ours.get("webhookURL", ""))
    if actual_url.rstrip("/") != want_url.rstrip("/"):
        problems.append(
            f"Registered URL is {actual_url}, but this deployment is at {want_url}. "
            f"Deliveries are going somewhere else."
        )

    types = set(ours.get("transactionTypes") or [])
    missing_types = set(TRANSACTION_TYPES) - types
    if missing_types:
        problems.append(
            f"transactionTypes is missing {sorted(missing_types)}. CREATE is Pump.fun "
            f"and CREATE_POOL is Raydium LaunchLab — without both, half the market is "
            f"invisible."
        )

    accounts = set(ours.get("accountAddresses") or [])
    missing_programs = want_programs - accounts
    if missing_programs:
        problems.append(
            "accountAddresses is missing "
            + ", ".join(f"{p} ({KNOWN_LAUNCHPAD_PROGRAMS[p]})" for p in sorted(missing_programs))
        )

    # Helius returns the authHeader back, so this can be compared rather than
    # guessed at. A mismatch is the nastiest failure here: every delivery is
    # rejected 401 and looks precisely like silence.
    actual_secret = str(ours.get("authHeader") or "")
    if secret.strip() and actual_secret != secret.strip():
        problems.append(
            "authHeader does not match HELIUS_WEBHOOK_SECRET. Every delivery is being "
            "rejected with 401, which from this side is indistinguishable from no "
            "deliveries at all."
        )
    elif not secret.strip():
        problems.append(
            "HELIUS_WEBHOOK_SECRET is not set here, so the receiver rejects everything."
        )

    return {
        "ok": not problems,
        "state": "healthy" if not problems else "misconfigured",
        "problems": problems,
        "expected_url": want_url,
        "webhook_id": ours.get("webhookID"),
        "webhooks": [_summarise(w, secret) for w in registered],
    }


def _summarise(webhook: dict[str, Any], secret: str) -> dict[str, Any]:
    """One webhook, without echoing the secret back into a response body."""
    auth = str(webhook.get("authHeader") or "")
    return {
        "id": webhook.get("webhookID"),
        "url": webhook.get("webhookURL"),
        "transaction_types": webhook.get("transactionTypes") or [],
        "account_addresses": webhook.get("accountAddresses") or [],
        "type": webhook.get("webhookType"),
        "auth_header_set": bool(auth),
        "auth_header_matches": bool(secret.strip()) and auth == secret.strip(),
    }


async def repair(api_key: str, *, base_url: str, secret: str) -> dict[str, Any]:
    """Create the webhook, or correct the existing one in place.

    Editing rather than deleting-and-recreating where possible: Helius issues
    a new ``webhookID`` on create, and an operator who wrote the old one down
    should not silently end up with a different one.

    Refuses to run without a secret. Registering a webhook whose
    ``authHeader`` does not match what the receiver checks would produce a
    registration that looks correct in the Helius dashboard and rejects every
    delivery — worse than no webhook, because it looks fixed.
    """
    if not secret.strip():
        raise WebhookError(
            "HELIUS_WEBHOOK_SECRET must be set before registering a webhook — "
            "otherwise every delivery would be rejected by this app's own check."
        )

    want_url = expected_url(base_url)
    payload = {
        "webhookURL": want_url,
        "transactionTypes": TRANSACTION_TYPES,
        "accountAddresses": sorted(KNOWN_LAUNCHPAD_PROGRAMS),
        "webhookType": WEBHOOK_TYPE,
        "authHeader": secret.strip(),
    }

    registered = await list_webhooks(api_key)
    ours = next(
        (w for w in registered if WEBHOOK_PATH in str(w.get("webhookURL", ""))), None
    )

    if ours is None:
        created = await _call("POST", api_key, json=payload)
        log.info("helius_webhook_created", url=want_url, id=(created or {}).get("webhookID"))
        return {
            "action": "created",
            "webhook_id": (created or {}).get("webhookID"),
            "url": want_url,
        }

    webhook_id = ours.get("webhookID")
    updated = await _call("PUT", api_key, path=f"/{webhook_id}", json=payload)
    log.info("helius_webhook_repaired", url=want_url, id=webhook_id)
    return {
        "action": "repaired",
        "webhook_id": (updated or ours).get("webhookID", webhook_id),
        "url": want_url,
    }
