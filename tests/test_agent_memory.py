"""Annie writing memory because she was told to, from Telegram or Discord.

The operator dictating memory is a first-class path, not an escape hatch:
they know things Annie cannot infer from the market, and a notebook only she
can write is missing half its content. These tests cover the shape that
conversation actually takes — "make me a file called X", then the content
over the following messages — plus the guardrails that keep a confused model
from doing damage with the same tools.
"""

from __future__ import annotations

import pytest

from app.annie.agent import (
    _tool_create_memory,
    _tool_delete_memory,
    _tool_list_memory,
    _tool_read_memory,
    _tool_write_memory,
)
from app.memory import bootstrap, index, service


class FakeAgent:
    """The tools only reach the memory service, so nothing else is needed."""


@pytest.fixture
def agent(isolated_memory):
    bootstrap._seed_files()
    index.reindex_all()
    return FakeAgent()


class TestCreatingAFileByName:
    async def test_a_plain_name_becomes_a_real_path(self, agent):
        result = await _tool_create_memory(agent, {"name": "Crowded narratives"})

        assert result["created"] is True
        assert result["path"] == "notes/crowded-narratives.md"
        assert result["empty"] is True

    async def test_a_section_can_be_chosen(self, agent):
        result = await _tool_create_memory(
            agent, {"name": "What worked in September", "section": "playbook"}
        )
        assert result["path"] == "playbook/what-worked-in-september.md"

    async def test_an_empty_file_is_allowed_because_content_comes_later(self, agent):
        """The real conversation is "make me a file called X" and *then* the
        content. Refusing to create an empty file would break that."""
        await _tool_create_memory(agent, {"name": "Crowded narratives"})
        memory = service.read("notes/crowded-narratives.md")

        assert memory is not None
        assert "Nothing written yet" in memory.body

    async def test_creating_twice_returns_the_existing_content_instead(self, agent):
        await _tool_create_memory(
            agent, {"name": "Crowded narratives", "text": "AI agents are saturated."}
        )
        again = await _tool_create_memory(agent, {"name": "Crowded narratives"})

        assert again["created"] is False
        assert again["already_exists"] is True
        assert "AI agents are saturated." in again["current_content"]

    async def test_a_traversal_name_is_refused(self, agent, isolated_memory):
        result = await _tool_create_memory(agent, {"path": "../../escaped.md"})
        assert result["created"] is False
        assert not (isolated_memory.parent / "escaped.md").exists()

    async def test_an_unknown_section_falls_back_rather_than_failing(self, agent):
        """A model inventing a section should not lose the operator's note —
        it lands in notes/, which is recoverable, rather than erroring out."""
        result = await _tool_create_memory(
            agent, {"name": "Something", "section": "inventions"}
        )
        assert result["path"] == "notes/something.md"


class TestDictatingContent:
    async def test_dictated_text_is_written_verbatim(self, agent):
        await _tool_create_memory(agent, {"name": "Crowded narratives"})
        dictated = (
            "Stop launching into AI agents. Every desk is doing it, the last four "
            "I watched round-tripped inside an hour, and the ones that held were "
            "all launched before the narrative was obvious."
        )
        result = await _tool_write_memory(
            agent, {"path": "notes/crowded-narratives.md", "text": dictated}
        )

        assert result["saved"] is True
        assert result["mode"] == "appended"
        assert dictated in service.read("notes/crowded-narratives.md").body

    async def test_successive_messages_accumulate(self, agent):
        for line in ["First thing.", "Second thing.", "Third thing."]:
            await _tool_write_memory(agent, {"name": "dictation", "text": line})

        body = service.read("notes/dictation.md").body
        assert "First thing." in body and "Second thing." in body and "Third thing." in body

    async def test_writing_to_a_new_name_creates_it(self, agent):
        result = await _tool_write_memory(
            agent, {"name": "brand new", "section": "playbook", "text": "Content."}
        )
        assert result["mode"] == "created"
        assert result["path"] == "playbook/brand-new.md"

    async def test_replace_overwrites_and_says_so(self, agent):
        await _tool_write_memory(agent, {"name": "draft", "text": "Old thinking."})
        result = await _tool_write_memory(
            agent, {"name": "draft", "text": "New thinking.", "replace": True}
        )

        body = service.read("notes/draft.md").body
        assert result["mode"] == "replaced"
        assert result["replaced_length"] > 0
        assert "New thinking." in body
        assert "Old thinking." not in body

    async def test_append_is_the_default(self, agent):
        """The failure mode of an accidental replace is silently losing prose
        nobody can recover, so the safe operation is the default one."""
        await _tool_write_memory(agent, {"name": "draft", "text": "Keep this."})
        await _tool_write_memory(agent, {"name": "draft", "text": "And this."})

        body = service.read("notes/draft.md").body
        assert "Keep this." in body and "And this." in body

    async def test_the_operator_can_correct_a_core_belief(self, agent):
        """Directed writes reach core/ deliberately — the operator is
        entitled to correct what Annie thinks."""
        result = await _tool_write_memory(
            agent,
            {
                "path": "core/market-model.md",
                "text": "Correction: launches are closer to 20k/day, not 16k.",
            },
        )
        assert result["saved"] is True
        assert "20k/day" in service.read("core/market-model.md").body

    async def test_empty_text_is_refused(self, agent):
        result = await _tool_write_memory(agent, {"name": "x", "text": "   "})
        assert result["saved"] is False


class TestFindingAndForgetting:
    async def test_list_gives_paths_without_dumping_bodies(self, agent):
        await _tool_create_memory(
            agent, {"name": "Crowded narratives", "text": "A" * 5000}
        )
        listed = await _tool_list_memory(agent, {})

        paths = [f["path"] for f in listed["files"]]
        assert "notes/crowded-narratives.md" in paths
        assert all("body" not in f for f in listed["files"])
        # Summaries stay excerpt-sized — the whole point is not filling the
        # turn with the notebook's contents. 300 leaves room for the 280-char
        # cap plus the ellipsis a word-boundary trim adds.
        assert all(len(f["summary"]) <= 300 for f in listed["files"])

    async def test_list_can_be_scoped_to_a_section(self, agent):
        from app.memory.paths import CORE_FILES

        listed = await _tool_list_memory(agent, {"section": "core"})
        assert listed["total"] == len(CORE_FILES)
        assert all(f["section"] == "core" for f in listed["files"])

    async def test_read_returns_the_whole_file(self, agent):
        await _tool_create_memory(agent, {"name": "readable", "text": "The content."})
        result = await _tool_read_memory(agent, {"path": "notes/readable.md"})

        assert result["found"] is True
        assert "The content." in result["content"]

    async def test_being_told_to_forget_actually_deletes(self, agent):
        await _tool_create_memory(agent, {"name": "wrong idea", "text": "Bad take."})
        result = await _tool_delete_memory(agent, {"path": "notes/wrong-idea.md"})

        assert result["deleted"] is True
        assert service.read("notes/wrong-idea.md") is None
        assert index.search("Bad take") == []

    async def test_the_seeded_core_files_cannot_be_deleted(self, agent):
        """Every cycle reads these as established context. Losing one would
        quietly change how she thinks until someone noticed."""
        from app.memory.paths import CORE_FILES

        for path in CORE_FILES:
            result = await _tool_delete_memory(agent, {"path": path})
            assert result["deleted"] is False
            assert "cannot be deleted" in result["error"]
            assert service.read(path) is not None

    async def test_deleting_a_missing_file_is_reported_not_silent(self, agent):
        result = await _tool_delete_memory(agent, {"path": "notes/never-existed.md"})
        assert result["deleted"] is False
        assert result["error"] == "no such memory file"


class TestWhatWasWrittenIsFindable:
    async def test_a_dictated_file_is_searchable_immediately(self, agent):
        await _tool_create_memory(
            agent,
            {
                "name": "Crowded narratives",
                "section": "playbook",
                "text": "AI agent tokens are saturated — stop launching into them.",
                "keys": ["ai-agent"],
            },
        )

        assert [h.path for h in index.by_key("ai-agent")] == [
            "playbook/crowded-narratives.md"
        ]
        assert "playbook/crowded-narratives.md" in [
            h.path for h in index.search("saturated launching")
        ]

    async def test_a_contract_address_dictated_in_chat_resolves_by_key(self, agent):
        mint = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
        await _tool_write_memory(
            agent,
            {
                "name": "watch this one",
                "text": f"Keep an eye on {mint} — the creator has three winners already.",
            },
        )
        assert [h.path for h in index.by_key(mint)] == ["notes/watch-this-one.md"]
