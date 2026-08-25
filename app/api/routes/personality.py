"""Personality routes — the editable voice knobs on PersonalityConfig
(app/db/models/research.py). Separate from Settings (research thresholds)
and separate from the hard rules in app/annie/persona.py, which this
endpoint cannot touch — see PersonalityConfig's docstring.

``POST /personality/extract`` is the primary way to change this now
(2026-08-25): an operator writes one paragraph describing how they want
Annie to sound, and an LLM extracts the individual tone/communication_style/
skepticism_level/pushback_degree/explanation_style fields from it — "I
cannot fill that one by one" was the direct ask. The old per-field PATCH
still exists for a quick one-off tweak without re-running extraction.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.schemas import PersonalityConfigOut
from app.db.models.research import PersonalityConfig
from app.db.repo import FirestoreRepo, get_repo
from app.providers.interfaces import ProviderError
from app.providers.registry import ProviderRegistry, get_registry

router = APIRouter()

_EDITABLE_FIELDS = {
    "name", "description", "tone", "communication_style",
    "skepticism_level", "pushback_degree", "explanation_style",
}

_EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "description", "tone", "communication_style",
        "skepticism_level", "pushback_degree", "explanation_style",
    ],
    "properties": {
        "description": {"type": "string", "description": "One-paragraph overall summary of the persona."},
        "tone": {"type": "string", "description": "A short phrase, e.g. 'warm but blunt'."},
        "communication_style": {"type": "string"},
        "skepticism_level": {"type": "string"},
        "pushback_degree": {"type": "string"},
        "explanation_style": {"type": "string"},
    },
}

_EXTRACT_SYSTEM_PROMPT = (
    "You turn a free-form paragraph an operator wrote — describing how they want their AI "
    "research assistant, Annie, to sound and behave — into five short, concrete phrases plus a "
    "one-paragraph summary. Each phrase should be a few words to one sentence, written as an "
    "instruction to Annie (e.g. 'blunt, no hedging, gets to the point fast'), grounded in what the "
    "paragraph actually says. If the paragraph doesn't address one of the five dimensions, infer a "
    "reasonable value consistent with the rest of what they described rather than leaving it "
    "generic or empty — every field must reflect a real reading of their paragraph, not a "
    "boilerplate default repeated regardless of input."
)


@router.get("/personality", response_model=PersonalityConfigOut)
async def get_personality(repo: FirestoreRepo = Depends(get_repo)) -> Any:
    config = await repo.get_personality_config()
    return config or PersonalityConfig()


@router.patch("/personality", response_model=PersonalityConfigOut)
async def update_personality(body: dict[str, Any], repo: FirestoreRepo = Depends(get_repo)) -> Any:
    """Direct per-field edit — still here for a quick one-off tweak without
    re-running extraction over the whole paragraph."""
    current = await repo.get_personality_config() or PersonalityConfig()
    for key, value in body.items():
        if key in _EDITABLE_FIELDS:
            setattr(current, key, str(value))
    return await repo.upsert_personality_config(current)


@router.post("/personality/extract", response_model=PersonalityConfigOut)
async def extract_personality(
    body: dict[str, Any],
    repo: FirestoreRepo = Depends(get_repo),
    registry: ProviderRegistry = Depends(get_registry),
) -> Any:
    text = str(body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required.")

    try:
        payload, _usage = await registry.reasoning.structured(
            task="label",
            system=_EXTRACT_SYSTEM_PROMPT,
            user=text,
            schema=_EXTRACT_SCHEMA,
            schema_name="personality_extraction",
            max_tokens=800,
        )
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=f"Extraction failed: {exc}") from exc

    current = await repo.get_personality_config() or PersonalityConfig()
    current.source_text = text
    for key in ("description", "tone", "communication_style", "skepticism_level", "pushback_degree", "explanation_style"):
        value = payload.get(key)
        if value:
            setattr(current, key, str(value))
    return await repo.upsert_personality_config(current)
