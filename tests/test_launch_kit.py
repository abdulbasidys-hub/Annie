"""Two stages: choose an idea, then get everything needed to ship it.

An idea is short because the operator is scanning three of them to pick one.
A kit is long because they have picked and now have to launch today.
Producing kits for all three would bury the decision under material for two
launches that will never happen — and producing them inline is what truncated
the daily ideas call and cost a day's launch ideas entirely.
"""

from __future__ import annotations

import json

import pytest

from app.memory import ideas, launch_kit

IDEA = {
    "name": "Cat Lawyer",
    "ticker": "LAWCAT",
    "kind": "meme",
    "derived_from": "DoiDmTARKwqsxdDVngWe2NEevBUAGz1h3k9F9iApump",
    "differs_by": "The original was a generic cat; this one is specifically a lawyer.",
    "angle": "A cat in a courtroom filing motions for bag-holders.",
    "hook": "The indignation is the joke — it reads as a reaction image.",
    "why_now": "Third week of cat-adjacent; the specific variants clear at 3x.",
    "evidence": "11 of 14 modified animal names cleared this week.",
    "grounding": "observed",
    "risk": "Week three is usually where a theme saturates.",
}

KIT = {
    "description": "objection your honour my bags are down bad",
    "visual_identity": "Flat vector, four muted colours off the courtroom palette.",
    "logo_prompt": "A tabby cat in an ill-fitting grey suit behind a courtroom bench...",
    "pfp_prompt": "Close crop of the same tabby mid-objection, readable as a circle...",
    "banner_prompt": "Wide 3:1 courtroom, cat off to the right, empty bench left...",
    "meme_prompts": ["The cat asleep on the bench...", "The cat holding a tiny gavel..."],
    "website": {
        "concept": "One scroll. The cat fills the screen, the only action is buy.",
        "sections": ["hero — cat mid-objection", "live chart", "how to buy"],
        "build_notes": "Palette off the art. No motion. No roadmap — reads as dead.",
    },
    "x_account": {
        "handle": "catlawyercoin",
        "display_name": "Cat Lawyer",
        "bio": "objection. my bags are down bad.",
        "pinned_post": "your honour i object to my bags being down 94%",
    },
    "telegram": {
        "group_name": "Cat Lawyer HQ",
        "description": "chambers",
        "pinned_message": "CA: <contract> — court is in session",
    },
    "launch_posts": ["objection", "overruled", "appealing"],
    "first_hour": "Post one, wait for the first reply, then drop the CA.",
}


class FakeCompletions:
    def __init__(self, payload, finish="stop"):
        self.payload = payload
        self.finish = finish
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = json.dumps(self.payload) if self.payload is not None else "{not json"
        choice = type("C", (), {
            "message": type("M", (), {"content": content})(),
            "finish_reason": self.finish,
        })()
        return type("R", (), {
            "choices": [choice],
            "usage": type("U", (), {"prompt_tokens": 9000, "completion_tokens": 3000})(),
        })()


class FakeRegistry:
    def __init__(self, payload=KIT, finish="stop"):
        self.completions = FakeCompletions(payload, finish)

        async def raw_client(_s=None):
            return type("Cl", (), {
                "chat": type("Ch", (), {"completions": self.completions})()
            })()

        self.reasoning = type("R", (), {"raw_client": raw_client})()


@pytest.fixture
def settings():
    from app.config import get_settings

    return get_settings()


class TestTheIdeaStaysADecision:
    def test_an_idea_carries_only_what_picks_a_winner(self):
        """Art direction, the site and the posts are not in here. They were,
        and three ideas at sixteen fields each did not fit in the response."""
        required = set(ideas.IDEA_SCHEMA["properties"]["ideas"]["items"]["required"])

        assert required == {
            "name", "ticker", "kind", "angle", "hook",
            "derived_from", "differs_by",
            "why_now", "evidence", "grounding", "risk",
        }

    def test_it_asks_why_anyone_would_share_it(self):
        """The hook is the field that separates an idea from a theme."""
        props = ideas.IDEA_SCHEMA["properties"]["ideas"]["items"]["properties"]

        assert "share" in props["hook"]["description"]

    def test_delivery_points_at_the_next_step(self):
        text = ideas.format_for_delivery(
            {"ideas": [IDEA], "read_of_the_market": "cats", "avoid": []}
        )

        assert "LAWCAT" in text
        assert "elaborate" in text.lower()
        assert "logo_prompt" not in text


class TestTheKit:
    async def test_it_produces_every_artefact_needed_to_launch(
        self, isolated_memory, settings
    ):
        kit = await launch_kit.generate(IDEA, FakeRegistry(), settings)

        for field in ("description", "logo_prompt", "pfp_prompt", "banner_prompt",
                      "meme_prompts", "website", "x_account", "telegram",
                      "launch_posts", "first_hour"):
            assert kit.get(field), f"{field} missing — the launch is not shippable"

    async def test_it_carries_the_name_and_ticker_through(
        self, isolated_memory, settings
    ):
        kit = await launch_kit.generate(IDEA, FakeRegistry(), settings)

        assert kit["ticker"] == "LAWCAT"
        assert kit["name"] == "Cat Lawyer"

    async def test_the_design_method_is_loaded_into_the_call(
        self, isolated_memory, settings
    ):
        """The art prompts are the highest-value thing here and go into an
        image model unedited. Without the method they come back as the
        generic crypto art it spends pages telling you to avoid."""
        from app.memory import skills

        skills.seed()
        registry = FakeRegistry()

        await launch_kit.generate(IDEA, registry, settings)

        system = registry.completions.calls[0]["messages"][0]["content"]
        assert "Art direction method" in system
        assert "# Voice" in system, "it should still sound like her"

    async def test_the_idea_is_put_in_front_of_it(self, isolated_memory, settings):
        registry = FakeRegistry()

        await launch_kit.generate(IDEA, registry, settings)

        brief = registry.completions.calls[0]["messages"][1]["content"]
        assert "Cat Lawyer" in brief
        assert "bag-holders" in brief
        assert "saturates" in brief, "the risk it flagged should carry through"

    async def test_operator_context_is_passed_as_a_steer(
        self, isolated_memory, settings
    ):
        registry = FakeRegistry()

        await launch_kit.generate(IDEA, registry, settings, context="make it darker")

        brief = registry.completions.calls[0]["messages"][1]["content"]
        assert "make it darker" in brief

    async def test_the_ceiling_is_generous_enough_for_twenty_artefacts(
        self, isolated_memory, settings
    ):
        """A truncated kit fails exactly the way the daily ideas did."""
        registry = FakeRegistry()

        await launch_kit.generate(IDEA, registry, settings)

        assert registry.completions.calls[0]["max_completion_tokens"] >= 5000

    async def test_truncation_says_it_was_truncated(self, isolated_memory, settings):
        registry = FakeRegistry(payload=None, finish="length")

        kit = await launch_kit.generate(IDEA, registry, settings)

        assert "cut off" in kit["error"]


class TestFindingTheIdeaAgain:
    async def test_an_idea_is_findable_by_ticker(self, isolated_memory):
        await ideas.record(
            {"read_of_the_market": "cats", "ideas": [IDEA], "avoid": []},
            origin="daily",
        )

        found = ideas.find_idea("LAWCAT")

        assert found is not None
        assert found["name"] == "Cat Lawyer"

    async def test_the_dollar_prefix_is_tolerated(self, isolated_memory):
        """The operator writes "elaborate on $LAWCAT"."""
        await ideas.record(
            {"read_of_the_market": "cats", "ideas": [IDEA], "avoid": []},
            origin="daily",
        )

        assert ideas.find_idea("$lawcat") is not None

    async def test_an_older_set_is_still_searched(self, isolated_memory):
        """They may well come back to yesterday's third idea."""
        await ideas.record(
            {"read_of_the_market": "cats", "ideas": [IDEA], "avoid": []},
            origin="daily",
        )
        for n in range(3):
            await ideas.record(
                {"read_of_the_market": "x",
                 "ideas": [{**IDEA, "ticker": f"OTHER{n}", "name": f"Other {n}"}],
                 "avoid": []},
                origin="daily",
            )

        assert ideas.find_idea("LAWCAT") is not None

    async def test_an_unknown_ticker_returns_nothing(self, isolated_memory):
        assert ideas.find_idea("NOPE") is None


class TestTheRegister:
    """An idea from three weeks ago was effectively lost — it existed in a
    dated file nobody scans. One file, newest first, is what makes "bring
    back the worm one" answerable."""

    async def test_every_idea_lands_in_one_readable_file(self, isolated_memory):
        from app.memory import service

        await ideas.record(
            {"read_of_the_market": "cats", "ideas": [IDEA], "avoid": []}, origin="daily"
        )

        memory = service.read(ideas.LOG_PATH)
        assert memory is not None
        assert "LAWCAT" in memory.body
        assert "Cat Lawyer" in memory.body

    async def test_the_file_is_grouped_by_day(self, isolated_memory):
        from app.memory import service

        await ideas.record(
            {"read_of_the_market": "cats", "ideas": [IDEA], "avoid": []}, origin="daily"
        )

        body = service.read(ideas.LOG_PATH).body
        assert "## 20" in body, "no date heading to scan by"

    async def test_a_requested_set_says_what_was_asked_for(self, isolated_memory):
        from app.memory import service

        await ideas.record(
            {"read_of_the_market": "x", "ideas": [IDEA], "avoid": []},
            origin="requested",
            brief="something about worms",
        )

        body = service.read(ideas.LOG_PATH).body
        assert "something about worms" in body
        assert "requested" in body

    async def test_the_register_is_queryable_as_data(self, isolated_memory):
        await ideas.record(
            {"read_of_the_market": "cats", "ideas": [IDEA], "avoid": []}, origin="daily"
        )

        entries = ideas.log_entries(limit=10)

        assert entries[0]["ticker"] == "LAWCAT"
        assert entries[0]["day"]

    async def test_it_is_findable_by_contract_of_memory_search(self, isolated_memory):
        """The log carries every ticker as a key, so pasting one into chat
        resolves rather than returning nothing."""
        from app.memory import index

        await ideas.record(
            {"read_of_the_market": "cats", "ideas": [IDEA], "avoid": []}, origin="daily"
        )

        assert index.by_key("lawcat")


class TestPartialRecall:
    """People remember a coin as "the worm one", not by its registered name."""

    async def test_a_partial_name_finds_it(self, isolated_memory):
        await ideas.record(
            {"read_of_the_market": "x",
             "ideas": [{**IDEA, "name": "Worm On A String", "ticker": "WORM"}],
             "avoid": []},
            origin="daily",
        )

        assert ideas.find_idea("", name="worm") is not None

    async def test_a_partial_ticker_finds_it(self, isolated_memory):
        await ideas.record(
            {"read_of_the_market": "x",
             "ideas": [{**IDEA, "name": "World War Rug", "ticker": "WWR2"}],
             "avoid": []},
            origin="daily",
        )

        assert ideas.find_idea("WWR") is not None

    async def test_a_two_character_fragment_does_not_match_everything(
        self, isolated_memory
    ):
        """Partial matching must not turn into "returns the first idea"."""
        await ideas.record(
            {"read_of_the_market": "x", "ideas": [IDEA], "avoid": []}, origin="daily"
        )

        assert ideas.find_idea("zz") is None


class TestIdeasHaveALifecycle:
    """Themes cycle — AI one week, politics the next. An idea that was right
    and untimely is not spent; it comes back when its week does. That only
    means anything if launching one marks it off."""

    async def test_launching_marks_the_idea(self, isolated_memory):
        from app.memory import launches, service

        await ideas.record(
            {"read_of_the_market": "cats", "ideas": [IDEA], "avoid": []}, origin="daily"
        )
        await launches.register("Mint" + "4" * 40, ticker="LAWCAT", name="Cat Lawyer")

        body = service.read(ideas.LOG_PATH).body
        assert "LAWCAT" not in body, "a launched idea is still in the ideas list"

    async def test_an_unlaunched_idea_stays_available(self, isolated_memory):
        await ideas.record(
            {"read_of_the_market": "x",
             "ideas": [IDEA, {**IDEA, "ticker": "WORM", "name": "Worm"}],
             "avoid": []},
            origin="daily",
        )
        from app.memory import launches

        await launches.register("Mint" + "5" * 40, ticker="LAWCAT")

        tickers = {e["ticker"] for e in ideas.log_entries(limit=20)}
        assert "LAWCAT" not in tickers, "launched ideas belong in Our Launches"
        assert "WORM" in tickers, "the unlaunched one must stay available"

    async def test_a_launched_idea_still_carries_its_contract(self, isolated_memory):
        from app.memory import launches

        mint = "Mint" + "6" * 40
        await ideas.record(
            {"read_of_the_market": "x", "ideas": [IDEA], "avoid": []}, origin="daily"
        )
        await launches.register(mint, ticker="LAWCAT")

        assert ideas.launched_map()["LAWCAT"]["mint"] == mint

    async def test_it_can_still_be_elaborated_after_launching(self, isolated_memory):
        """Relaunching a theme later is the point, so nothing is closed off."""
        from app.memory import launches

        await ideas.record(
            {"read_of_the_market": "x", "ideas": [IDEA], "avoid": []}, origin="daily"
        )
        await launches.register("Mint" + "7" * 40, ticker="LAWCAT")

        assert ideas.find_idea("LAWCAT") is not None


class TestOneIdeaIsAlwaysAMeme:
    """The operator launches meme coins, and a set of three narrative plays
    is not what they asked for. A meme is a specific thing — recognition,
    contrast, timing, remixability — and a joke or an advert is not one."""

    def test_the_schema_requires_a_kind(self):
        item = ideas.IDEA_SCHEMA["properties"]["ideas"]["items"]

        assert "kind" in item["required"]
        assert item["properties"]["kind"]["enum"] == ["meme", "narrative", "event"]

    def test_the_prompt_demands_one_and_forbids_faking_it(self):
        """A mislabelled meme is worse than an honest gap: it hides that the
        market had nothing that day."""
        assert "must be a real meme" in ideas.SYSTEM_PROMPT
        assert "mislabelled" in ideas.SYSTEM_PROMPT

    def test_the_meme_method_is_loaded_for_a_launch(self, isolated_memory):
        from app.memory import skills

        skills.seed()

        assert "meme-theory" in skills.ROUTES["launch"]
        assert "What Is a Meme?" in skills.for_task("launch")

    def test_delivery_shows_which_kind_each_idea_is(self):
        text = ideas.format_for_delivery(
            {"ideas": [IDEA], "read_of_the_market": "cats", "avoid": []}
        )

        assert "meme" in text


class TestIdeasPointAtWhatTheyCameFrom:
    """The operator checks the original before acting on a derivative, which
    is the right instinct — so the contract address has to be there, and the
    idea has to be a step away from it rather than the same token again."""

    def test_the_schema_asks_where_it_came_from(self):
        props = ideas.IDEA_SCHEMA["properties"]["ideas"]["items"]["properties"]

        assert "contract address" in props["derived_from"]["description"]
        assert "never from memory" in props["derived_from"]["description"]

    def test_a_derivative_must_say_how_it_differs(self):
        props = ideas.IDEA_SCHEMA["properties"]["ideas"]["items"]["properties"]

        assert "how this is NOT that coin" in props["differs_by"]["description"]

    def test_proposing_an_existing_coin_is_forbidden(self):
        """The original has the holders, the chart and the head start."""
        assert "Never propose a coin that already exists" in ideas.SYSTEM_PROMPT
        assert "head start" in ideas.SYSTEM_PROMPT

    def test_delivery_shows_the_contract_to_check(self):
        text = ideas.format_for_delivery(
            {"ideas": [IDEA], "read_of_the_market": "x", "avoid": []}
        )

        assert "Next to:" in text
        assert "DoiDmTARKwqsxdDVngWe2NEevBUAGz1h3k9F9iApump" in text
        assert "Differs:" in text
