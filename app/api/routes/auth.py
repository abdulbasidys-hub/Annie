"""Login, logout, and session check (§66). See app/auth.py for the model."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response

from app.auth import COOKIE_NAME, create_session_token, require_auth, verify_credentials
from app.config import Settings, get_settings

router = APIRouter()


@router.post("/login")
async def login(
    body: dict[str, str], response: Response, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    if not verify_credentials(settings, username, password):
        raise HTTPException(status_code=401, detail="Incorrect username or password.")

    token = create_session_token(settings, username)
    production = settings.environment == "production"
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        # Cross-origin in production (frontend and API on different domains)
        # needs SameSite=None, which browsers only honour over HTTPS. Local
        # dev has no HTTPS, so it falls back to Lax — same-site there anyway
        # since "localhost:5180" and "localhost:8000" share a registrable
        # domain, just different ports.
        secure=production,
        samesite="none" if production else "lax",
        max_age=60 * 60 * 24 * 30,
        path="/",
    )
    return {"authenticated": True, "username": username}


@router.post("/logout")
async def logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"authenticated": False}


@router.get("/me")
async def me(username: str = Depends(require_auth)) -> dict[str, Any]:
    return {"authenticated": True, "username": username}
