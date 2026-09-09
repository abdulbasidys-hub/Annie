"""One voice, wherever Annie writes.

She had a personality and a set of scheduled messages that did not use it,
which is the wrong way round: the brief is what an operator reads every day
without asking, and the chat is what they only see once they have come
looking. Four prompts said "You are Annie" and then described a job — the
cycle headline, the launch ideas, the weekly and monthly rollups — and none
described how she sounds.

So the tests here are about coverage: every place she writes gets the voice,
and the epistemic rules are *not* what travels — those belong to the chat
prompt, which has a different job from a notebook-editing one.

Her voice is a constant now. It was a Firestore document behind a Personality
page until 2026-09-09; the tests that covered fetching, caching and falling
back when that read failed are gone with the read.
"""

from __future__ import annotations

from app.annie import persona, voice


class TestWhatTravels:
    def test_the_voice_section_is_the_personality_constant(self):
        assert persona.voice_section() == persona.PERSONALITY

    def test_it_carries_the_built_in_personality(self):
        assert "# Voice" in persona.voice_section()
        assert "quirky" in persona.voice_section()

    def test_it_reads_nothing(self, monkeypatch):
        """The point of retiring the Personality page. A scheduled write must
        not depend on a network round trip to know how to sound."""
        def explode():
            raise AssertionError("voice_section reached for the database")

        monkeypatch.setattr("app.db.repo.get_repo", explode)

        assert "# Voice" in voice.current()

    def test_the_epistemic_rules_do_not_travel(self):
        """They stay with the chat prompt. A notebook-editing prompt has its
        own rules and does not need to be told how to cite a market cap in
        conversational prose — carrying them everywhere would bloat every
        scheduled call for nothing."""
        section = persona.voice_section()

        assert "SPECULATION" not in section
        assert "# Format" not in section


class TestEveryPlaceSheWrites:
    """The prompts that had no voice at all until now."""

    @staticmethod
    def _prefixed(prompt: str) -> str:
        return voice.prefix(prompt)

    def test_the_cycle_headline_prompt_gets_it(self):
        from app.memory.learn import SYSTEM_PROMPT

        out = self._prefixed(SYSTEM_PROMPT)

        assert "# Voice" in out
        assert "Edit operations:" in out, "the job's own rules were lost"

    def test_the_launch_ideas_prompt_gets_it(self):
        from app.memory.ideas import SYSTEM_PROMPT

        out = self._prefixed(SYSTEM_PROMPT)

        assert "# Voice" in out
        assert "launch ideas" in out

    def test_the_rollup_prompt_gets_it(self):
        from app.memory.rollup import ROLLUP_PROMPT

        out = self._prefixed(ROLLUP_PROMPT.format(period="weekly"))

        assert "# Voice" in out

    def test_voice_is_placed_before_the_job(self):
        """A model that reads the job first answers in the register the job
        was written in, which for these is a spec sheet."""
        from app.memory.learn import SYSTEM_PROMPT

        out = self._prefixed(SYSTEM_PROMPT)

        assert out.index("# Voice") < out.index("You are Annie")


class TestChatAgreesWithTheRest:
    def test_chat_and_the_scheduled_prompts_share_one_voice(self):
        """Chat must not drift into sounding like a different person from
        the daily brief."""
        assert persona.voice_section() in persona.system_prompt()

    def test_the_chat_prompt_still_carries_the_rules(self):
        prompt = persona.system_prompt()

        assert "SPECULATION" in prompt
        assert "# Voice" in prompt

    def test_the_personality_section_is_not_duplicated(self):
        assert persona.system_prompt().count("# Voice") == 1
