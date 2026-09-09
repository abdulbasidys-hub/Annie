"""Diagnosing the webhook registration.

Every way this breaks looks identical from the receiving end — no deliveries.
So the value is entirely in naming *which* way, because the fixes are
different: a wrong URL needs a repair, a missing secret needs an env var, and
an auto-disabled webhook needs re-saving. A check that just said "something is
wrong" would be no better than the zeros it replaced.
"""

from __future__ import annotations

import pytest

from app.providers import helius_webhook
from app.providers.helius import KNOWN_LAUNCHPAD_PROGRAMS

BASE = "https://annie.up.railway.app"
SECRET = "shh"
GOOD_URL = f"{BASE}/api/webhooks/helius"


def _webhook(**overrides):
    base = {
        "webhookID": "wh_1",
        "webhookURL": GOOD_URL,
        "transactionTypes": ["CREATE", "CREATE_POOL"],
        "accountAddresses": sorted(KNOWN_LAUNCHPAD_PROGRAMS),
        "webhookType": "enhanced",
        "authHeader": SECRET,
    }
    return {**base, **overrides}


@pytest.fixture
def registered(monkeypatch):
    """Control what Helius reports back."""
    box = {"webhooks": []}

    async def fake_list(api_key):
        return box["webhooks"]

    monkeypatch.setattr(helius_webhook, "list_webhooks", fake_list)
    return box


async def _inspect(secret=SECRET):
    return await helius_webhook.inspect("key", base_url=BASE, secret=secret)


class TestDiagnosis:
    async def test_a_correct_registration_is_clean(self, registered):
        registered["webhooks"] = [_webhook()]
        report = await _inspect()

        assert report["ok"] is True
        assert report["state"] == "healthy"
        assert report["problems"] == []
        assert report["webhook_id"] == "wh_1"

    async def test_nothing_registered_says_so_plainly(self, registered):
        report = await _inspect()

        assert report["state"] == "not_registered"
        assert "Nothing will ever arrive" in report["problems"][0]

    async def test_a_webhook_for_something_else_is_not_mistaken_for_ours(self, registered):
        registered["webhooks"] = [_webhook(webhookURL="https://other.app/hooks/x")]
        report = await _inspect()

        assert report["state"] == "points_elsewhere"
        assert "none" in report["problems"][0]

    async def test_a_stale_domain_is_named_with_both_urls(self, registered):
        """The redeploy-onto-a-new-domain case. Showing only 'URL mismatch'
        would leave the operator guessing which one is wrong."""
        old = "https://annie-old.up.railway.app/api/webhooks/helius"
        registered["webhooks"] = [_webhook(webhookURL=old)]
        report = await _inspect()

        assert report["state"] == "misconfigured"
        problem = next(p for p in report["problems"] if "Registered URL" in p)
        assert old in problem
        assert GOOD_URL in problem

    async def test_a_missing_transaction_type_is_called_out_by_launchpad(self, registered):
        """Half the market going missing is not obvious from the type name,
        so the message says which launchpad it costs."""
        registered["webhooks"] = [_webhook(transactionTypes=["CREATE"])]
        report = await _inspect()

        problem = next(p for p in report["problems"] if "transactionTypes" in p)
        assert "CREATE_POOL" in problem
        assert "Raydium LaunchLab" in problem

    async def test_a_missing_program_is_named(self, registered):
        registered["webhooks"] = [
            _webhook(accountAddresses=["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"])
        ]
        report = await _inspect()

        problem = next(p for p in report["problems"] if "accountAddresses" in p)
        assert "raydium-launchlab" in problem

    async def test_a_secret_mismatch_explains_why_it_looks_like_silence(self, registered):
        """The nastiest one: the registration looks right in the dashboard and
        every delivery is rejected 401."""
        registered["webhooks"] = [_webhook(authHeader="something-else")]
        report = await _inspect()

        problem = next(p for p in report["problems"] if "authHeader" in p)
        assert "401" in problem
        assert "indistinguishable" in problem

    async def test_no_secret_configured_here_is_its_own_problem(self, registered):
        registered["webhooks"] = [_webhook()]
        report = await _inspect(secret="")

        assert any("HELIUS_WEBHOOK_SECRET is not set" in p for p in report["problems"])

    async def test_the_secret_is_never_echoed_back(self, registered):
        """The report goes into an HTTP response body and a terminal."""
        registered["webhooks"] = [_webhook()]
        report = await _inspect()

        assert SECRET not in str(report["webhooks"])
        assert report["webhooks"][0]["auth_header_matches"] is True

    async def test_an_unreachable_helius_is_reported_not_raised(self, monkeypatch):
        async def boom(api_key):
            raise helius_webhook.WebhookError("Helius returned 401 for GET /")

        monkeypatch.setattr(helius_webhook, "list_webhooks", boom)
        report = await _inspect()

        assert report["state"] == "unreachable"
        assert report["ok"] is False


class TestRepair:
    async def test_it_refuses_without_a_secret(self, registered):
        """Registering a webhook whose authHeader the receiver will reject
        produces something that looks fixed in the dashboard and delivers
        nothing — worse than no webhook."""
        with pytest.raises(helius_webhook.WebhookError, match="must be set"):
            await helius_webhook.repair("key", base_url=BASE, secret="  ")

    async def test_it_creates_when_nothing_exists(self, registered, monkeypatch):
        calls = []

        async def fake_call(method, api_key, *, path="", json=None):
            calls.append((method, path, json))
            return {"webhookID": "wh_new"}

        monkeypatch.setattr(helius_webhook, "_call", fake_call)
        result = await helius_webhook.repair("key", base_url=BASE, secret=SECRET)

        assert result["action"] == "created"
        assert calls[0][0] == "POST"
        assert calls[0][2]["webhookURL"] == GOOD_URL
        assert set(calls[0][2]["transactionTypes"]) == {"CREATE", "CREATE_POOL"}
        assert calls[0][2]["authHeader"] == SECRET

    async def test_it_edits_in_place_rather_than_recreating(self, registered, monkeypatch):
        """Helius issues a new webhookID on create. An operator who noted the
        old one should not silently end up with a different one."""
        registered["webhooks"] = [_webhook(webhookURL="https://old.app/api/webhooks/helius")]
        calls = []

        async def fake_call(method, api_key, *, path="", json=None):
            calls.append((method, path, json))
            return {"webhookID": "wh_1"}

        monkeypatch.setattr(helius_webhook, "_call", fake_call)
        result = await helius_webhook.repair("key", base_url=BASE, secret=SECRET)

        assert result["action"] == "repaired"
        assert result["webhook_id"] == "wh_1"
        assert calls[0][0] == "PUT"
        assert calls[0][1] == "/wh_1"
        assert calls[0][2]["webhookURL"] == GOOD_URL


class TestApiSurface:
    @pytest.fixture
    def client(self, isolated_memory):
        from app.auth import require_auth
        from app.main import app
        from fastapi.testclient import TestClient

        app.dependency_overrides[require_auth] = lambda: {"sub": "test"}
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()

    def test_the_endpoint_reports_the_url_it_worked_out(self, client, monkeypatch):
        async def fake_list(api_key):
            return []

        monkeypatch.setattr(helius_webhook, "list_webhooks", fake_list)
        body = client.get("/api/system/webhook").json()

        assert body["state"] == "not_registered"
        assert body["detected_base_url"], "the operator cannot verify a URL that is not shown"
        assert body["expected_url"].endswith("/api/webhooks/helius")

    def test_an_override_url_is_honoured(self, client, monkeypatch):
        async def fake_list(api_key):
            return []

        monkeypatch.setattr(helius_webhook, "list_webhooks", fake_list)
        body = client.get("/api/system/webhook", params={"base_url": BASE}).json()

        assert body["expected_url"] == GOOD_URL

    def test_repair_without_a_secret_is_a_400_not_a_500(self, client, monkeypatch):
        monkeypatch.setenv("HELIUS_WEBHOOK_SECRET", "")
        from app.config import get_settings

        get_settings.cache_clear()
        response = client.post("/api/system/webhook/repair", params={"base_url": BASE})

        assert response.status_code == 400
        assert "HELIUS_WEBHOOK_SECRET" in response.json()["detail"]
