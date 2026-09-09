"""One voice, wherever Annie writes.

She had a personality and a set of scheduled messages that did not use it,
which is the wrong way round: the brief is what an operator reads every day
without asking, and the chat is what they only see once they have come
looking. Four prompts said "You are Annie" and then described a job — the
cycle headline, the launch ideas, the weekly and monthly rollups — and none
described how she sounds.

So the tests here are mostly about coverage: every place she writes gets the
voice, the operator's configuration reaches all of them, and the epistemic
rules are not what travels — those belong to the chat prompt, which has a
different job from a notebook-editing one.
"""

from __future__ import annotations

import pytest

from app.annie import persona, voice

CONFIGURED = {
    "tone": "dry",
    "communication_style": "terse",
    "skepticism_level": "high",
    "pushback_degree": "push back hard",
    "explanation_style": "worked examples",
}
IN_THEIR_WORDS = "Talk to me like a trader friend. Dry, a bit sardonic, never chirpy."


@pytest.fixture(autouse=True)
def _no_cached_voice():
    voice.clear_cache()
    yield
    voice.clear_cache()


class TestWhatTravels:
    def test_the_voice_section_carries_the_built_in_personality(self):
        assert "# Voice" in persona.voice_section()
        assert "quirky" in persona.voice_section()

    def test_configured_fields_are_added_not_substituted(self):
        """An operator adjusts how she sounds on top of the built-in voice.
        Leaving the page blank must not leave her voiceless."""
        section = persona.voice_section(CONFIGURED)

        assert "# Voice" in section
        assert "Tone: dry" in section

    def test_the_operators_own_words_are_included_verbatim(self):
        """The five fields are an LLM's extraction from this paragraph, and
        the extraction loses exactly what matters: "dry, a bit sardonic,
        never chirpy" survives as tone="dry" and the rest is gone."""
        section = persona.voice_section(CONFIGURED, source_text=IN_THEIR_WORDS)

        assert IN_THEIR_WORDS in section

    def test_their_words_come_after_the_derived_fields(self):
        """So that where the two disagree, what they actually wrote wins."""
        section = persona.voice_section(CONFIGURED, source_text=IN_THEIR_WORDS)

        assert section.index("Tone: dry") < section.index(IN_THEIR_WORDS)

    def test_an_empty_configuration_still_produces_the_built_in_voice(self):
        assert "# Voice" in persona.voice_section(None, source_text="")

    def test_the_epistemic_rules_do_not_travel(self):
        """They stay with the chat prompt. A notebook-editing prompt has its
        own rules and does not need to be told how to cite a market cap in
        conversational prose — carrying them everywhere would bloat every
        scheduled call for nothing."""
        section = persona.voice_section(CONFIGURED, source_text=IN_THEIR_WORDS)

        assert "SPECULATION" not in section
        assert "# Format" not in section


class TestEveryPlaceSheWrites:
    """The prompts that had no voice at all until now."""

    @staticmethod
    async def _prefixed(prompt: str) -> str:
        return await voice.prefix(prompt)

    async def test_the_cycle_headline_prompt_gets_it(self):
        from app.memory.learn import SYSTEM_PROMPT

        out = await self._prefixed(SYSTEM_PROMPT)

        assert "# Voice" in out
        assert "Edit operations:" in out, "the job's own rules were lost"

    async def test_the_launch_ideas_prompt_gets_it(self):
        from app.memory.ideas import SYSTEM_PROMPT

        out = await self._prefixed(SYSTEM_PROMPT)

        assert "# Voice" in out
        assert "launch ideas" in out

    async def test_the_rollup_prompt_gets_it(self):
        from app.memory.rollup import ROLLUP_PROMPT

        out = await self._prefixed(ROLLUP_PROMPT.format(period="weekly"))

        assert "# Voice" in out

    async def test_voice_is_placed_before_the_job(self):
        """A model that reads the job first answers in the register the job
        was written in, which for these is a spec sheet."""
        from app.memory.learn import SYSTEM_PROMPT

        out = await self._prefixed(SYSTEM_PROMPT)

        assert out.index("# Voice") < out.index("You are Annie")


class TestItNeverStopsHerWriting:
    async def test_an_unreachable_config_falls_back_to_the_built_in_voice(
        self, monkeypatch
    ):
        """A Firestore blip must not stop the cycle writing its notebook.
        Writing in the default voice is a far smaller failure than not
        writing."""
        async def boom():
            raise RuntimeError("firestore unreachable")

        monkeypatch.setattr("app.db.repo.get_repo", boom)

        assert "# Voice" in await voice.current()

    async def test_the_lookup_is_cached_between_calls(self, monkeypatch):
        """It runs before every scheduled write, and the Personality page
        changes about never."""
        calls = {"n": 0}

        async def counting():
            calls["n"] += 1
            raise RuntimeError("no repo in tests")

        monkeypatch.setattr("app.db.repo.get_repo", counting)

        await voice.current()
        await voice.current()
        await voice.current()

        assert calls["n"] == 1

    async def test_clearing_the_cache_picks_up_an_edit(self, monkeypatch):
        calls = {"n": 0}

        async def counting():
            calls["n"] += 1
            raise RuntimeError("no repo in tests")

        monkeypatch.setattr("app.db.repo.get_repo", counting)

        await voice.current()
        voice.clear_cache()
        await voice.current()

        assert calls["n"] == 2


class TestChatAgreesWithTheRest:
    def test_the_chat_prompt_uses_the_same_renderer(self):
        """Chat must not drift into sounding like a different person from
        the daily brief."""
        prompt = persona.system_prompt(
            personality_overrides=dict(CONFIGURED), personality_source_text=IN_THEIR_WORDS
        )

        assert "Tone: dry" in prompt
        assert IN_THEIR_WORDS in prompt

    def test_the_chat_prompt_still_carries_the_rules(self):
        prompt = persona.system_prompt()

        assert "SPECULATION" in prompt
        assert "# Voice" in prompt

    def test_the_personality_section_is_not_duplicated(self):
        """`voice_section` and `system_prompt` both start from PERSONALITY,
        so a naive concatenation would send it twice."""
        prompt = persona.system_prompt(
            personality_overrides=dict(CONFIGURED), personality_source_text=IN_THEIR_WORDS
        )

        assert prompt.count("# Voice") == 1
