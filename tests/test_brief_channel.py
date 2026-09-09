"""Pointing the daily brief at a Discord channel.

This existed only as "ask Annie in Discord to create one", which needs the bot
to hold Manage Channels in a guild it shares with the operator. Without that
permission there was no path at all — the brief was written every day and went
nowhere, and the only trace was a reason buried in a job result nobody reads.

The guard that matters here is verification before saving. Registering a
channel the bot cannot post to reproduces exactly the failure being fixed: a
configuration that looks correct in the UI and delivers nothing.
"""

from __future__ import annotations

import pytest

from app.db.models.discord import DiscordChannel


@pytest.fixture
def repo_state():
    return {"channels": [], "sent": []}


@pytest.fixture
def client(isolated_memory, repo_state, monkeypatch):
    from app.auth import require_auth
    from app.db.repo import get_repo
    from app.main import app
    from fastapi.testclient import TestClient

    class StubRepo:
        async def list_discord_channels(self, **kwargs):
            return repo_state["channels"]

        async def create_discord_channel(self, channel):
            repo_state["channels"].append(channel)
            return channel

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "bot-token")
    from app.config import get_settings

    get_settings.cache_clear()

    app.dependency_overrides[require_auth] = lambda: {"sub": "test"}
    app.dependency_overrides[get_repo] = lambda: StubRepo()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _sending(monkeypatch, repo_state, *, succeeds: bool):
    async def fake_send(bot_token, channel_id, text):
        repo_state["sent"].append((channel_id, text))
        return succeeds

    import app.bots.discord_bot as bot

    monkeypatch.setattr(bot, "send_channel_message", fake_send)


class TestReadingTheCurrentState:
    def test_it_says_plainly_when_nothing_is_configured(self, client):
        body = client.get("/api/system/brief-channel").json()

        assert body["configured"] is False
        assert body["channel"] is None
        assert body["discord_configured"] is True

    def test_it_reports_the_configured_channel(self, client, repo_state):
        repo_state["channels"] = [
            DiscordChannel(channel_id="999", name="briefs", purpose="morning_brief")
        ]
        body = client.get("/api/system/brief-channel").json()

        assert body["configured"] is True
        assert body["channel"]["channel_id"] == "999"

    def test_a_channel_with_another_purpose_does_not_count(self, client, repo_state):
        """A channel Annie made for research findings is not where the brief
        goes, and treating it as one would send the brief somewhere wrong."""
        repo_state["channels"] = [
            DiscordChannel(channel_id="1", name="research", purpose="research_findings")
        ]
        body = client.get("/api/system/brief-channel").json()

        assert body["configured"] is False
        assert len(body["known_channels"]) == 1


class TestSetting:
    def test_it_verifies_by_posting_before_saving(self, client, repo_state, monkeypatch):
        _sending(monkeypatch, repo_state, succeeds=True)

        response = client.post(
            "/api/system/brief-channel", json={"channel_id": "1234567890123456789"}
        )

        assert response.status_code == 200
        assert repo_state["sent"], "nothing was posted, so nothing was verified"
        assert repo_state["channels"][0].purpose == "morning_brief"

    def test_the_confirmation_says_what_the_channel_is_for(self, client, repo_state, monkeypatch):
        """It doubles as a marker in the channel itself — someone scrolling
        back should be able to tell why Annie is posting there."""
        _sending(monkeypatch, repo_state, succeeds=True)
        client.post("/api/system/brief-channel", json={"channel_id": "123"})

        _, text = repo_state["sent"][0]
        assert "daily brief" in text

    def test_each_purpose_describes_itself(self, client, repo_state, monkeypatch):
        """The two messages must not be interchangeable. The brief one used
        to promise "the day's three launch ideas" — in a channel that, at the
        time, was never going to receive any."""
        _sending(monkeypatch, repo_state, succeeds=True)
        client.post(
            "/api/system/brief-channel",
            json={"channel_id": "123", "purpose": "launch_ideas"},
        )

        _, text = repo_state["sent"][0]
        assert "launch ideas" in text
        assert "Three of them" in text
        assert repo_state["channels"][0].purpose == "launch_ideas"


class TestTheTwoPurposes:
    """The brief is a report; the ideas are a proposal.

    They are worth routing separately — pinning an idea is a normal thing to
    want and awkward when it is the tail of a status summary — but requiring
    two channels before anything arrives would be worse than the problem.
    Hence: optional, with a fallback.
    """

    def test_an_unknown_purpose_is_refused(self, client, repo_state, monkeypatch):
        _sending(monkeypatch, repo_state, succeeds=True)

        response = client.post(
            "/api/system/brief-channel", json={"channel_id": "123", "purpose": "whatever"}
        )

        assert response.status_code == 422
        assert "launch_ideas" in response.json()["detail"]
        assert repo_state["sent"] == [], "it posted before validating the purpose"

    def test_the_default_purpose_is_still_the_brief(self, client, repo_state, monkeypatch):
        """Callers written before the split must keep working."""
        _sending(monkeypatch, repo_state, succeeds=True)
        client.post("/api/system/brief-channel", json={"channel_id": "123"})

        assert repo_state["channels"][0].purpose == "morning_brief"

    def test_ideas_report_as_falling_back_when_only_the_brief_is_set(
        self, client, repo_state
    ):
        repo_state["channels"] = [
            DiscordChannel(channel_id="1", name="briefing", purpose="morning_brief")
        ]
        body = client.get("/api/system/brief-channel").json()

        assert body["ideas_configured"] is False
        assert body["ideas_fall_back_to_brief"] is True

    def test_a_separate_ideas_channel_is_reported_as_its_own(self, client, repo_state):
        repo_state["channels"] = [
            DiscordChannel(channel_id="1", name="briefing", purpose="morning_brief"),
            DiscordChannel(channel_id="2", name="launch-radar", purpose="launch_ideas"),
        ]
        body = client.get("/api/system/brief-channel").json()

        assert body["ideas_channel"]["channel_id"] == "2"
        assert body["ideas_fall_back_to_brief"] is False

    def test_nothing_falls_back_when_nothing_is_configured(self, client):
        """The fallback flag is about where ideas go, so it must not read as
        true when there is no brief channel to fall back to."""
        body = client.get("/api/system/brief-channel").json()

        assert body["ideas_fall_back_to_brief"] is False

    def test_an_unreachable_channel_is_refused_not_saved(self, client, repo_state, monkeypatch):
        """The whole point. Saving a channel the bot cannot post to would look
        correct in the UI and deliver nothing — the exact failure being fixed."""
        _sending(monkeypatch, repo_state, succeeds=False)

        response = client.post("/api/system/brief-channel", json={"channel_id": "123"})

        assert response.status_code == 400
        assert "could not post" in response.json()["detail"]
        assert repo_state["channels"] == [], "an unreachable channel was saved anyway"

    def test_a_non_numeric_id_is_rejected_with_how_to_find_it(self, client, repo_state, monkeypatch):
        _sending(monkeypatch, repo_state, succeeds=True)

        response = client.post("/api/system/brief-channel", json={"channel_id": "#general"})

        assert response.status_code == 422
        assert "Developer Mode" in response.json()["detail"]
        assert repo_state["sent"] == [], "it tried to post before validating the ID"

    def test_it_refuses_when_discord_is_not_configured(
        self, client, repo_state, monkeypatch
    ):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "")
        from app.config import get_settings

        get_settings.cache_clear()
        _sending(monkeypatch, repo_state, succeeds=True)

        response = client.post("/api/system/brief-channel", json={"channel_id": "123"})

        assert response.status_code == 400
        assert "DISCORD_BOT_TOKEN" in response.json()["detail"]
