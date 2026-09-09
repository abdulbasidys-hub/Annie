"""One voice, wherever Annie writes.

She had a personality and a set of scheduled messages that did not use it,
which is the wrong way round. The chat agent assembled the full persona;
the cycle headline that opens every brief, the launch ideas and the weekly
and monthly rollups each had their own "You are Annie" prompt that described
a job and never described a voice. The brief is the thing an operator reads
every day without asking. The chat is the thing they only see once they have
come looking.

This module is the shared half. It carries the *voice* and deliberately not
the rest of the persona, because those prompts have their own jobs and their
own rules — a notebook-editing prompt does not need to be told how to cite a
market cap in conversational prose; it needs to be told how to sound while
doing what it already does.

**It reads nothing.** Her voice was a Firestore document edited on a
Personality page until 2026-09-09: a paragraph the operator wrote, plus five
short fields an LLM had extracted from it. Both halves are gone, and the
whole thing is :data:`app.annie.persona.PERSONALITY` — a constant, in one
file, changed by editing it. That is better on every axis that turned out to
matter. The extraction was lossy in exactly the dimension a voice lives in
("dry, a bit sardonic, never chirpy" came back as tone="dry"). A prose
paragraph is reviewable in a diff and a Firestore document is not. And every
scheduled write was paying a read, plus the cache and the failure path that
a read needs, to fetch a worse version of something that could simply be
written down.
"""

from __future__ import annotations

from app.annie import persona


def current() -> str:
    """The voice section to prepend to a non-chat prompt."""
    return persona.voice_section()


def prefix(prompt: str) -> str:
    """``prompt`` with the voice ahead of it.

    Voice first, job second. The task rules are what she is doing; the voice
    is who is doing it, and a model that reads the job first tends to answer
    in the register the job was written in — which, for these, is a spec
    sheet.
    """
    return f"{current()}\n\n---\n\n{prompt}"
