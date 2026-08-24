"""Personality routes — the editable voice knobs on PersonalityConfig
(app/db/models/research.py). Separate from Settings (research thresholds)
and separate from the hard rules in app/annie/persona.py, which this
endpoint cannot touch — see PersonalityConfig's docstring.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.schemas import PersonalityConfigOut
from app.db.models.research import PersonalityConfig
from app.db.repo import FirestoreRepo, get_repo

router = APIRouter()

_EDITABLE_FIELDS = {
    "name", "description", "tone", "communication_style",
    "skepticism_level", "pushback_degree", "explanation_style",
}


@router.get("/personality", response_model=PersonalityConfigOut)
async def get_personality(repo: FirestoreRepo = Depends(get_repo)) -> Any:
    config = await repo.get_personality_config()
    return config or PersonalityConfig()


@router.patch("/personality", response_model=PersonalityConfigOut)
async def update_personality(body: dict[str, Any], repo: FirestoreRepo = Depends(get_repo)) -> Any:
    current = await repo.get_personality_config() or PersonalityConfig()
    for key, value in body.items():
        if key in _EDITABLE_FIELDS:
            setattr(current, key, str(value))
    return await repo.upsert_personality_config(current)
