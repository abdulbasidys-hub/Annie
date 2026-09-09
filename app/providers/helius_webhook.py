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
    """Every webhook on this key — as the *list* endpoint reports them.

    Deliberately not the whole truth, and callers must know it: the list
    response omits ``accountAddresses`` entirely. Reading membership from
    here reports every webhook as having none, which is indistinguishable
    from a real misconfiguration. Use :func:`get_webhook` for the one you
    actually care about.
    """
    found = await _call("GET", api_key)
    return found if isinstance(found, list) else []


async def get_webhook(api_key: str, webhook_id: str) -> dict[str, Any] | None:
    """One webhook, in full.

    ``GET /v0/webhooks`` and ``GET /v0/webhooks/{id}`` do not return the same
    fields. Confirmed against the live API on 2026-09-09: the list omits
    ``accountAddresses``, so a correctly-registered webhook read from the list
    looks like it is filtering on nothing — which is exactly what a broken one
    looks like. Everything that decides whether a registration is healthy has
    to come from here.
    """
    found = await _call("GET", api_key, path=f"/{webhook_id}")
    return found if isinstance(found, dict) else None


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

    # Re-read ours in full. The list entry is missing accountAddresses, so
    # every check below that reads it would otherwise be answering from a
    # field the API never sent.
    detailed = await get_webhook(api_key, str(ours.get("webhookID") or ""))
    if detailed:
        ours = {**ours, **detailed}

    problems: list[str] = []

    # First, because it makes every other field moot. Helius switches a
    # webhook off by itself after sustained delivery failures — a deployment
    # down for a day is enough — and it stays off after the deployment comes
    # back. Nothing about the registration looks wrong; it simply is not
    # running. This was live for eleven days here before anything noticed,
    # because nothing looked at the flag.
    if detailed and detailed.get("active") is False:
        reason = str(detailed.get("disabledReason") or "no reason given")
        since = str(detailed.get("disabledAt") or "unknown")
        problems.append(
            f"Helius has this webhook DISABLED (since {since}: {reason}). It is "
            f"registered correctly and switched off, so nothing is being delivered "
            f"and nothing about the configuration would show why."
        )

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

    disabled = bool(detailed and detailed.get("active") is False)
    if not problems:
        state = "healthy"
    elif disabled:
        # Its own state, because the fix differs: nothing to correct, just
        # switch it back on.
        state = "disabled"
    else:
        state = "misconfigured"

    return {
        "ok": not problems,
        "state": state,
        "problems": problems,
        "expected_url": want_url,
        "webhook_id": ours.get("webhookID"),
        "active": (detailed or {}).get("active"),
        "webhooks": [_summarise({**w, **(detailed or {})} if w is ours else w, secret)
                     for w in registered],
    }


def _summarise(webhook: dict[str, Any], secret: str) -> dict[str, Any]:
    """One webhook, without echoing the secret back into a response body."""
    auth = str(webhook.get("authHeader") or "")
    return {
        "id": webhook.get("webhookID"),
        "url": webhook.get("webhookURL"),
        "transaction_types": webhook.get("transactionTypes") or [],
        "account_addresses": webhook.get("accountAddresses") or [],
        "active": webhook.get("active"),
        "disabled_reason": webhook.get("disabledReason"),
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
        # Explicit, because the most common thing wrong with a webhook that
        # has been running a while is not its configuration but that Helius
        # switched it off after the deployment was unreachable. Correcting
        # the fields without clearing that flag repairs nothing.
        "active": True,
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


def public_base_url() -> str | None:
    """This deployment's externally-reachable base URL, or None.

    Returning None is a real answer, not a failure to try. The registration
    tells Helius where to deliver, so a guessed URL does not degrade to "no
    webhook" — it degrades to "a webhook pointing somewhere wrong", which is
    indistinguishable from silence and survives until someone thinks to look.
    Better to do nothing loudly.

    ``RAILWAY_PUBLIC_DOMAIN`` is the platform's own answer and needs no
    request, which is what makes boot-time reconciliation possible at all;
    ``PUBLIC_BASE_URL`` is the escape hatch for anywhere else.
    """
    import os

    explicit = (os.environ.get("PUBLIC_BASE_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")

    railway = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    if railway:
        return f"https://{railway}"

    return None


async def reconcile(settings, *, base_url: str | None = None) -> dict[str, Any]:
    """Make the live registration match this deployment. Safe to call on every boot.

    Inspects first and writes only when something is actually wrong, so a
    correct deployment restarting costs one GET and changes nothing.

    This exists because the alternative was a button. Every failure this
    repairs — no ``accountAddresses`` so the filter matches nothing, a URL
    left pointing at a previous deployment, a missing transaction type
    covering half the market — presents identically from the receiving end,
    as silence. A fix that depends on someone noticing silence and then
    remembering which button addresses it is not a fix; it is a standing
    invitation to the same outage.

    Never raises. A boot must not fail because a third-party API was briefly
    unreachable, and the next boot reconciles anyway.
    """
    if not settings.is_available("blockchain"):
        return {"action": "skipped", "reason": "HELIUS_API_KEY is not configured"}
    if not settings.helius_webhook_secret.strip():
        # Registering without one would set an authHeader this app then
        # rejects on every delivery: a webhook that looks correct in the
        # Helius dashboard and 401s everything.
        log.warning("webhook_reconcile_skipped", reason="HELIUS_WEBHOOK_SECRET is unset")
        return {"action": "skipped", "reason": "HELIUS_WEBHOOK_SECRET is not set"}

    base = base_url or public_base_url()
    if not base:
        log.warning(
            "webhook_reconcile_skipped",
            reason="cannot determine this deployment's public URL",
            fix="set PUBLIC_BASE_URL, or deploy somewhere that sets RAILWAY_PUBLIC_DOMAIN",
        )
        return {"action": "skipped", "reason": "public base URL unknown"}

    try:
        report = await inspect(settings.helius_api_key, base_url=base, secret=settings.helius_webhook_secret)
    except Exception:
        log.warning("webhook_reconcile_failed", stage="inspect", exc_info=True)
        return {"action": "failed", "reason": "could not read the current registration"}

    if report.get("ok"):
        log.info("webhook_reconciled", action="none", url=expected_url(base))
        return {"action": "none", "url": expected_url(base)}

    # `points_elsewhere` is deliberately included: webhooks on this key that
    # serve something else are left alone (repair only ever touches the one
    # whose URL carries our path), and if none of them is ours, one is created.
    problems = report.get("problems") or []
    try:
        result = await repair(
            settings.helius_api_key, base_url=base, secret=settings.helius_webhook_secret
        )
    except Exception:
        log.error("webhook_reconcile_failed", stage="repair", problems=problems, exc_info=True)
        return {"action": "failed", "reason": "repair call failed", "problems": problems}

    log.warning(
        "webhook_reconciled",
        action=result.get("action"),
        url=result.get("url"),
        fixed=problems,
    )
    return {**result, "fixed": problems}
