"""One voice, wherever Annie writes.

Annie had two mouths and only one of them sounded like her. The chat agent
assembled the full persona — identity, epistemics, the PERSONALITY section,
disagreement, formatting — plus whatever the operator configured on the
Personality page. Everything else she produced was written by its own
independent prompt: the cycle headline that opens every brief, the launch
ideas, the weekly and monthly summaries. Each said "You are Annie" and then
described a job, with no description of how she sounds.

The result was a system that had a personality and a set of scheduled
messages that did not use it, which is the wrong way round — the brief is the
thing an operator reads every day without asking for it, and the chat is the
thing they only see when they have already come looking.

This module is the shared half. It carries the *voice* — the built-in
PERSONALITY section plus the operator's own configuration — and deliberately
not the rest of the persona, because those other prompts have their own jobs
and their own rules. A notebook-editing prompt does not need to be told how
to cite a market cap in prose; it needs to be told how to sound while doing
what it already does.

The operator's paragraph is included verbatim alongside the five derived
fields. Those fields are an LLM's extraction from what they wrote, and an
extraction is lossy in exactly the dimension that matters here: "dry, a bit
sardonic, never chirpy" survives as tone="dry" and loses the rest.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from app.annie import persona

log = structlog.get_logger(__name__)

#: How long a fetched voice is reused. The Personality page changes rarely
#: and a cycle is not a hot path, but this runs before every scheduled write
#: and there is no reason to spend a Firestore read on each one.
CACHE_SECONDS = 300

_cache: tuple[float, str] | None = None


def clear_cache() -> None:
    """Drop the cached voice so the next write picks up an edit immediately."""
    global _cache
    _cache = None


async def current() -> str:
    """The voice section to prepend to a non-chat prompt.

    Returns the built-in voice when nothing is configured, and never raises:
    a Firestore blip must not stop Annie writing her notebook, and writing in
    the default voice is a much smaller failure than not writing at all.
    """
    global _cache

    now = time.monotonic()
    if _cache and now - _cache[0] < CACHE_SECONDS:
        return _cache[1]

    overrides: dict[str, str] | None = None
    source_text = ""
    try:
        from app.db.repo import get_repo

        config = await (await get_repo()).get_personality_config()
        if config is not None:
            overrides = {
                "tone": config.tone,
                "communication_style": config.communication_style,
                "skepticism_level": config.skepticism_level,
                "pushback_degree": config.pushback_degree,
                "explanation_style": config.explanation_style,
            }
            source_text = config.source_text or ""
    except Exception:
        log.info("voice_config_unavailable", exc_info=True)

    section = persona.voice_section(overrides, source_text=source_text)
    _cache = (now, section)
    return section


async def prefix(prompt: str) -> str:
    """``prompt`` with the current voice ahead of it.

    Voice first, job second. The task rules are what she is doing; the voice
    is who is doing it, and a model that reads the job first tends to answer
    in the register the job was written in.
    """
    return f"{await current()}\n\n---\n\n{prompt}"


def describe(overrides: dict[str, Any] | None, source_text: str = "") -> str:
    """Synchronous form, for callers that already hold the config."""
    return persona.voice_section(overrides, source_text=source_text)
