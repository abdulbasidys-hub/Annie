"""The receiving route, end to end: HTTP in, a row in the ledger out.

This path was rewritten during the memory migration — it used to do a
Firestore read and write per event and now writes one local SQLite row — and
nothing tested it afterwards. `test_pipeline_new.py` covers `stream.ingest_*`
directly and `test_webhook_check.py` covers the registration; between them sat
the actual HTTP handler, which is the only part Helius touches.

The payloads here are the shapes confirmed against real deliveries, quoted in
`app/api/routes/webhooks.py`: a Pump.fun creation arrives as `type: "CREATE"`,
`source: "PUMP_FUN"`, and a Raydium LaunchLab creation as `type:
"CREATE_POOL"`, `source: "RAYDIUM_LAUNCHLAB"`, both with the mint in
`tokenTransfers[0].mint`.
"""

from __future__ import annotations

import pytest

from app.memory import ledger

MINT = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
CREATOR = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"
PUMP_FUN = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
LAUNCHLAB = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
SOL = "So11111111111111111111111111111111111111112"

SECRET = "test-webhook-secret"


def _event(mint=MINT, *, program=PUMP_FUN, signature="sig-1", transfers=None):
    return {
        "signature": signature,
        "type": "CREATE",
        "source": "PUMP_FUN",
        "timestamp": 1_757_000_000,
        "feePayer": CREATOR,
        "tokenTransfers": transfers
        if transfers is not None
        else [{"mint": mint, "fromUserAccount": "", "toUserAccount": CREATOR}],
        "accountData": [{"account": program, "tokenBalanceChanges": []}],
    }


@pytest.fixture
def client(isolated_memory, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    monkeypatch.setenv("HELIUS_WEBHOOK_SECRET", SECRET)
    get_settings.cache_clear()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _post(client, body, *, secret=SECRET):
    headers = {"Authorization": secret} if secret is not None else {}
    return client.post("/api/webhooks/helius", json=body, headers=headers)


class TestADeliveryLands:
    def test_one_event_becomes_one_sighting(self, client):
        response = _post(client, [_event()])

        assert response.status_code == 200, response.text
        assert response.json()["created"] == 1
        assert response.json()["unparsed"] == 0

        sighting = ledger.get_sighting(MINT)
        assert sighting is not None, "the route returned success but wrote nothing"
        assert sighting.creator == CREATOR
        assert sighting.launchpad == "pump-fun"

    def test_a_launchlab_creation_lands_too(self, client):
        """Registering only one launchpad's type silently covers half the
        market, so both shapes are pinned."""
        event = _event(program=LAUNCHLAB, signature="sig-2")
        event["type"] = "CREATE_POOL"
        event["source"] = "RAYDIUM_LAUNCHLAB"

        _post(client, [event])

        assert ledger.get_sighting(MINT).launchpad == "raydium-launchlab"

    def test_a_bare_object_is_accepted_not_only_a_list(self, client):
        """Helius posts an array, but a single object must not 400 — a shape
        assumption is exactly the kind of thing that produces silence."""
        assert _post(client, _event()).json()["created"] == 1

    def test_the_creator_gets_a_movement_row(self, client):
        """The point of ingest: every launch by every wallet is recorded, so
        "who launches a lot" is answerable later."""
        _post(client, [_event()])

        assert ledger.get_creator(CREATOR)["launches"] == 1
        assert len(ledger.creator_moves(CREATOR)) == 1

    def test_a_delivery_costs_no_firestore_operations(self, client):
        """The reason this path was rewritten. It used to be one read plus
        one write per event, ~32,000 operations a day against a plan
        allowing 20,000 writes in total."""
        from app.memory import db

        _post(client, [_event(signature=f"s{i}") for i in range(50)])

        counters = db.counters_today()
        assert counters.get("firestore_writes", 0) == 0
        assert counters.get("firestore_reads", 0) == 0


class TestTheSecret:
    def test_a_wrong_secret_is_rejected(self, client):
        assert _post(client, [_event()], secret="nope").status_code == 401
        assert ledger.get_sighting(MINT) is None

    def test_a_missing_header_is_rejected(self, client):
        assert _post(client, [_event()], secret=None).status_code == 401

    def test_a_bearer_prefix_is_tolerated(self, client):
        """Helius sends back whatever authHeader was registered; if someone
        registered it with a Bearer prefix the deliveries must still land."""
        assert _post(client, [_event()], secret=f"Bearer {SECRET}").status_code == 200


class TestShapesItCannotRead:
    def test_an_unrecognisable_event_is_counted_not_crashed(self, client):
        body = _post(client, [{"signature": "x", "type": "CREATE"}]).json()

        assert body["unparsed"] == 1
        assert body["created"] == 0

    def test_one_bad_event_does_not_lose_the_batch(self, client):
        """A malformed payload must not cost the rest of the delivery — the
        old handler 500'd the whole batch when one event failed."""
        body = _post(client, [{"nonsense": True}, _event()]).json()

        assert body["created"] == 1
        assert body["unparsed"] == 1
        assert ledger.get_sighting(MINT) is not None

    def test_wrapped_sol_is_never_taken_as_the_launched_token(self, client):
        """A Pump.fun migration moves SOL as payment alongside the real
        token, with SOL listed first. Taking index 0 recorded wrapped SOL as
        a discovered token and then crashed qualification."""
        transfers = [{"mint": SOL}, {"mint": MINT}]

        _post(client, [_event(transfers=transfers)])

        assert ledger.get_sighting(SOL) is None
        assert ledger.get_sighting(MINT) is not None

    def test_the_mint_is_found_in_account_data_when_transfers_are_empty(self, client):
        event = _event(transfers=[])
        event["accountData"] = [
            {"account": PUMP_FUN, "tokenBalanceChanges": [{"mint": MINT}]}
        ]

        assert _post(client, [event]).json()["created"] == 1
        assert ledger.get_sighting(MINT) is not None


class TestRedelivery:
    def test_the_same_event_twice_is_a_repeat_not_a_duplicate(self, client):
        """Helius retries on a non-2xx, so the same signature arriving twice
        is normal and must not double-count a creator's launches."""
        _post(client, [_event()])
        second = _post(client, [_event()]).json()

        assert second["created"] == 0
        assert second["repeats"] == 1
        assert ledger.get_creator(CREATOR)["launches"] == 1
