"""Login, logout, and session check (§66). See app/auth.py for the model."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.auth import create_session_token, require_auth, verify_credentials
from app.config import Settings, get_settings

router = APIRouter()


@router.post("/login")
async def login(body: dict[str, str], settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    if not verify_credentials(settings, username, password):
        raise HTTPException(status_code=401, detail="Incorrect username or password.")

    token = create_session_token(settings, username)
    # Returned in the body, not set as a cookie — see app/auth.py's module
    # docstring for why a bearer token is what this deployment actually needs.
    return {"authenticated": True, "username": username, "token": token}


@router.post("/logout")
async def logout() -> dict[str, Any]:
    # Stateless JWT: there is no server-side session to invalidate. Logging
    # out is the frontend discarding the token it's holding.
    return {"authenticated": False}


@router.get("/me")
async def me(username: str = Depends(require_auth)) -> dict[str, Any]:
    return {"authenticated": True, "username": username}
