"""Boot-time convergence of the Helius registration.

The ingest path has one external dependency and every way it breaks looks
identical from the receiving end: nothing arrives. An empty
``accountAddresses`` matches no transactions. A URL left pointing at a
previous deployment delivers somewhere else. A missing transaction type
covers half the market. None is visible without asking Helius, and all are
mechanically fixable from what the process already knows at boot.

So the tests that matter here are about restraint as much as repair: it must
not write when nothing is wrong, and it must refuse outright rather than
guess when it cannot establish where deliveries should be sent — a webhook
pointing at the wrong host is indistinguishable from no webhook and survives
much longer.
"""

from __future__ import annotations

import pytest

from app.providers import helius_webhook as hw
from app.providers.helius import KNOWN_LAUNCHPAD_PROGRAMS

URL = "https://annie.up.railway.app"
SECRET = "shhh"
PROGRAMS = sorted(KNOWN_LAUNCHPAD_PROGRAMS)


class FakeSettings:
    helius_api_key = "key"
    helius_webhook_secret = SECRET

    def __init__(self, blockchain=True, secret=SECRET):
        self._blockchain = blockchain
        self.helius_webhook_secret = secret

    def is_available(self, capability):
        return self._blockchain if capability == "blockchain" else True


@pytest.fixture
def helius(monkeypatch):
    """A stand-in for the Helius webhook API that records what it was told."""
    state = {"webhooks": [], "puts": [], "posts": []}

    async def fake_list(api_key):
        return state["webhooks"]

    async def fake_call(method, api_key, path="", json=None):
        if method == "PUT":
            state["puts"].append(json)
            return {**(json or {}), "webhookID": "existing-id"}
        if method == "POST":
            state["posts"].append(json)
            return {**(json or {}), "webhookID": "new-id"}
        return state["webhooks"]

    monkeypatch.setattr(hw, "list_webhooks", fake_list)
    monkeypatch.setattr(hw, "_call", fake_call)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    return state


def _registration(**overrides):
    base = {
        "webhookID": "existing-id",
        "webhookURL": f"{URL}/api/webhooks/helius",
        "transactionTypes": ["CREATE", "CREATE_POOL"],
        "accountAddresses": PROGRAMS,
        "webhookType": "enhanced",
        "authHeader": SECRET,
    }
    base.update(overrides)
    return base


class TestItFixesTheRealFailures:
    async def test_an_empty_address_list_is_repaired(self, helius):
        """The one that actually happened. Right URL, right types, matching
        secret, and `accountAddresses: []` — which matches no transactions,
        so the webhook had never fired once."""
        helius["webhooks"] = [_registration(accountAddresses=[])]

        result = await hw.reconcile(FakeSettings(), base_url=URL)

        assert result["action"] == "repaired"
        assert helius["puts"][0]["accountAddresses"] == PROGRAMS
        assert any("accountAddresses" in p for p in result["fixed"])

    async def test_a_url_from_a_previous_deployment_is_repointed(self, helius):
        helius["webhooks"] = [
            _registration(webhookURL="https://annie-old.up.railway.app/api/webhooks/helius")
        ]

        result = await hw.reconcile(FakeSettings(), base_url=URL)

        assert result["action"] == "repaired"
        assert helius["puts"][0]["webhookURL"] == f"{URL}/api/webhooks/helius"

    async def test_a_missing_transaction_type_is_restored(self, helius):
        """Registering only CREATE silently covers Pump.fun and not Raydium."""
        helius["webhooks"] = [_registration(transactionTypes=["CREATE"])]

        await hw.reconcile(FakeSettings(), base_url=URL)

        assert set(helius["puts"][0]["transactionTypes"]) == set(hw.TRANSACTION_TYPES)

    async def test_nothing_registered_at_all_gets_created(self, helius):
        result = await hw.reconcile(FakeSettings(), base_url=URL)

        assert result["action"] == "created"
        assert helius["posts"][0]["accountAddresses"] == PROGRAMS
        assert helius["posts"][0]["authHeader"] == SECRET


class TestItLeavesAWorkingSetupAlone:
    async def test_a_correct_registration_is_not_rewritten(self, helius):
        """It runs on every boot, so the healthy path must cost one read and
        change nothing."""
        helius["webhooks"] = [_registration()]

        result = await hw.reconcile(FakeSettings(), base_url=URL)

        assert result["action"] == "none"
        assert helius["puts"] == [] and helius["posts"] == []

    async def test_someone_elses_webhook_on_the_same_key_is_untouched(self, helius):
        """Matching is on our own path, so an unrelated webhook is neither
        edited nor deleted — it just means ours does not exist yet."""
        other = _registration(webhookURL="https://something-else.example/hook")
        helius["webhooks"] = [other]

        result = await hw.reconcile(FakeSettings(), base_url=URL)

        assert result["action"] == "created", "it edited an unrelated webhook"
        assert helius["puts"] == []


class TestItRefusesRatherThanGuesses:
    async def test_it_does_nothing_without_a_known_public_url(self, helius):
        """A webhook pointing at the wrong host is worse than none: it is
        indistinguishable from silence and survives until someone looks."""
        result = await hw.reconcile(FakeSettings())

        assert result["action"] == "skipped"
        assert helius["posts"] == [] and helius["puts"] == []

    async def test_it_does_nothing_without_a_secret(self, helius):
        """Registering an authHeader this app then rejects produces a
        webhook that looks correct in the dashboard and 401s everything."""
        result = await hw.reconcile(FakeSettings(secret=""), base_url=URL)

        assert result["action"] == "skipped"
        assert helius["posts"] == []

    async def test_it_does_nothing_when_helius_is_not_configured(self, helius):
        result = await hw.reconcile(FakeSettings(blockchain=False), base_url=URL)

        assert result["action"] == "skipped"

    async def test_an_unreachable_api_does_not_raise(self, helius, monkeypatch):
        """Boot must not fail because a third-party API blipped. The next
        boot reconciles anyway."""
        async def boom(api_key):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(hw, "list_webhooks", boom)

        result = await hw.reconcile(FakeSettings(), base_url=URL)

        assert result["action"] == "failed"


class TestFindingItsOwnAddress:
    def test_railways_domain_is_used_when_present(self, monkeypatch):
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "annie-production-2b45.up.railway.app")

        assert hw.public_base_url() == "https://annie-production-2b45.up.railway.app"

    def test_an_explicit_override_wins(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "wrong.up.railway.app")
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://annie.example.com/")

        assert hw.public_base_url() == "https://annie.example.com"

    def test_none_when_it_cannot_tell(self, monkeypatch):
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)

        assert hw.public_base_url() is None
