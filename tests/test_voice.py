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

        assert "Knowing what you know" not in section
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

        assert "Knowing what you know" in prompt
        assert "# Voice" in prompt

    def test_the_personality_section_is_not_duplicated(self):
        assert persona.system_prompt().count("# Voice") == 1


class TestSheDoesNotWriteLikeAComplianceForm:
    """A real Telegram answer opened "FACT:" four times, tagged two more
    paragraphs "INFERENCE:", bolded roughly twenty fragments, and closed with
    a confidence rating and a "what would change my mind" paragraph nobody
    asked for.

    The discipline behind it is right and stays. The labelling was the
    mistake: when every claim carries a tag, the tags carry no information,
    and the reader stops being able to tell anything apart.
    """

    def test_it_does_not_ask_for_claim_tags(self):
        prompt = persona.system_prompt()

        assert "FACT:" not in prompt
        assert "INFERENCE:" not in prompt

    def test_it_says_outright_not_to_label(self):
        assert "Do not label your sentences" in persona.CLAIM_DISCIPLINE
        assert "how you think, not how you write" in persona.CLAIM_DISCIPLINE

    def test_the_four_grades_survive_as_thinking(self):
        """Dropping the tags must not drop the discipline — speculation in
        the voice of measurement is still the worst thing she can do."""
        text = persona.CLAIM_DISCIPLINE

        assert "Association is not causation" in text
        assert "have not tested that" in text, "no worked example of hedging a guess"

    def test_bolding_is_restrained(self):
        assert "Bold almost nothing" in persona.FORMATTING

    def test_hedging_paragraphs_are_not_mandatory(self):
        """The old rule demanded confidence and a what-would-change-my-mind
        on every claim that mattered, which is what produced the closing
        paragraph on a routine question."""
        text = persona.EVIDENCE_STANDARD

        assert "to every answer" in text
        assert "load-bearing" in text

    def test_denominators_are_still_required(self):
        """The one formatting rule that was doing real work."""
        assert "denominator" in persona.EVIDENCE_STANDARD
        assert '"14 of 61"' in persona.FORMATTING
