"""Knowing which channels exist, and being able to post to one.

Asked in Discord to send the day's launch ideas "to the appropriate channel",
Annie created a second one and then reported that she had no way to post into
it. Three separate faults in one exchange: she could not see the channels
that already existed, she created without looking, and creating was the only
channel thing she could do.

The duplicate is the worst of the three. An error is recoverable; two
channels for one job means messages land in whichever one a later call
happens to pick, and the operator has to notice that themselves.
"""

from __future__ import annotations

import pytest

from app.db.models.discord import DiscordChannel


class FakeRepo:
    def __init__(self, channels=None):
        self.channels = list(channels or [])
        self.created: list[dict] = []

    async def list_discord_channels(self, **kwargs):
        return self.channels


class FakeAgent:
    def __init__(self, channels=None, *, discord=True, can_create=True):
        from app.annie.platform import PlatformContext

        self.repo = FakeRepo(channels)
        self.sent: list[tuple[str, str]] = []
        self.created: list[dict] = []

        async def create_channel(name, purpose, category=None):
            self.created.append({"name": name, "purpose": purpose})
            return {"created": True, "channel_id": "new-1", "name": name}

        self.platform_context = PlatformContext(
            platform="discord",
            create_channel=create_channel if can_create else None,
        )

        class S:
            discord_bot_token = "t"

            def is_available(self, cap):
                return discord

        self.settings = S()


def _channel(name, purpose, cid="1"):
    return DiscordChannel(channel_id=cid, name=name, purpose=purpose)


@pytest.fixture
def sending(monkeypatch):
    box = {"sent": []}

    async def fake_send(token, channel_id, text):
        box["sent"].append((channel_id, text))
        return True

    import app.bots.discord_bot as bot

    monkeypatch.setattr(bot, "send_channel_message", fake_send)
    return box


class TestSeeingWhatExists:
    async def test_she_can_list_them(self):
        from app.annie.agent import _tool_list_channels

        agent = FakeAgent([_channel("briefing", "morning_brief"),
                           _channel("launch-radar", "launch_ideas", "2")])

        result = await _tool_list_channels(agent, {})

        assert result["count"] == 2
        assert {c["name"] for c in result["channels"]} == {"briefing", "launch-radar"}

    async def test_the_listing_is_available_without_a_discord_context(self):
        """The channels belong to the deployment, not to the conversation.
        Asked from Telegram, she had no view of Discord at all — which is
        how "send it to the right channel" became "I created one"."""
        from app.annie.agent import _tool_list_channels

        agent = FakeAgent([_channel("briefing", "morning_brief")], can_create=False)

        assert (await _tool_list_channels(agent, {}))["count"] == 1


class TestNotCreatingASecondOne:
    async def test_an_existing_channel_is_returned_instead_of_duplicated(self):
        from app.annie.agent import _tool_manage_discord_channel

        agent = FakeAgent([_channel("launch-ideas", "launch_ideas", "42")])

        result = await _tool_manage_discord_channel(
            agent, {"name": "launch-ideas", "purpose": "launch ideas"}
        )

        assert result["created"] is False
        assert result["already_exists"] is True
        assert result["channel_id"] == "42"
        assert agent.created == [], "it created a duplicate anyway"

    async def test_the_hash_prefix_does_not_defeat_the_check(self):
        """The operator writes #launch-ideas; Discord stores launch-ideas."""
        from app.annie.agent import _tool_manage_discord_channel

        agent = FakeAgent([_channel("launch-ideas", "launch_ideas")])

        result = await _tool_manage_discord_channel(
            agent, {"name": "#launch-ideas", "purpose": "x"}
        )

        assert result.get("already_exists") is True

    async def test_case_does_not_defeat_it_either(self):
        from app.annie.agent import _tool_manage_discord_channel

        agent = FakeAgent([_channel("Launch-Ideas", "launch_ideas")])

        result = await _tool_manage_discord_channel(
            agent, {"name": "launch-ideas", "purpose": "x"}
        )

        assert result.get("already_exists") is True

    async def test_a_genuinely_new_channel_is_still_created(self):
        from app.annie.agent import _tool_manage_discord_channel

        agent = FakeAgent([_channel("briefing", "morning_brief")])

        result = await _tool_manage_discord_channel(
            agent, {"name": "launch-ideas", "purpose": "launch ideas"}
        )

        assert result["created"] is True
        assert agent.created[0]["name"] == "launch-ideas"


class TestActuallyPosting:
    async def test_she_can_post_by_channel_name(self, sending):
        from app.annie.agent import _tool_send_to_channel

        agent = FakeAgent([_channel("launch-ideas", "launch_ideas", "42")])

        result = await _tool_send_to_channel(
            agent, {"text": "three ideas", "channel_name": "launch-ideas"}
        )

        assert result["sent"] is True
        assert sending["sent"] == [("42", "three ideas")]

    async def test_she_can_post_by_purpose(self, sending):
        """"The launch ideas channel" is a role. The operator should not have
        to remember what it was named."""
        from app.annie.agent import _tool_send_to_channel

        agent = FakeAgent([_channel("anything-at-all", "launch_ideas", "7")])

        result = await _tool_send_to_channel(
            agent, {"text": "ideas", "purpose": "launch_ideas"}
        )

        assert result["sent"] is True
        assert sending["sent"][0][0] == "7"

    async def test_an_unknown_channel_returns_the_real_ones(self, sending):
        """Rather than failing silently or inventing one."""
        from app.annie.agent import _tool_send_to_channel

        agent = FakeAgent([_channel("briefing", "morning_brief")])

        result = await _tool_send_to_channel(
            agent, {"text": "x", "channel_name": "nope"}
        )

        assert result["sent"] is False
        assert result["known_channels"][0]["name"] == "briefing"
        assert sending["sent"] == []

    async def test_an_empty_message_is_refused(self, sending):
        from app.annie.agent import _tool_send_to_channel

        agent = FakeAgent([_channel("briefing", "morning_brief")])

        assert (await _tool_send_to_channel(agent, {"text": "  "}))["sent"] is False

    async def test_it_says_so_when_discord_is_not_configured(self, sending):
        from app.annie.agent import _tool_send_to_channel

        agent = FakeAgent([_channel("briefing", "morning_brief")], discord=False)

        result = await _tool_send_to_channel(
            agent, {"text": "x", "channel_name": "briefing"}
        )

        assert result["sent"] is False
        assert "not configured" in result["error"]
