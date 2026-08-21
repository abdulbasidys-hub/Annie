"""Single-operator authentication (§66).

This deployment has one operator, not a user table — ``AUTH_USERNAME`` /
``AUTH_PASSWORD`` are literal credentials in configuration, not a hash to
look up. That is why login compares them directly (in constant time, via
``hmac.compare_digest``, so a timing side-channel can't leak the password a
character at a time) rather than involving ``argon2-cffi``: hashing a
password that already lives in plaintext in the environment would be
theatre, not security. If this ever grows into multi-user auth, that is the
point to introduce a real user store and password hashing — not before.

**Session transport: a bearer token in the ``Authorization`` header, not a
cookie.** This deployment runs the frontend and API on two unrelated domains
(a Vercel domain and a Railway domain) — not subdomains of one parent, fully
cross-site. A cross-site cookie there is fragile in a way that has nothing to
do with configuration: Safari blocks third-party cookies unconditionally by
default, and even where cookies are technically allowed (Chrome, Firefox),
getting ``SameSite=None; Secure`` exactly right across two hosts is a class
of bug that fails silently — the login call succeeds, the cookie is set, and
every subsequent request still 401s with nothing in the response to explain
why. A bearer token sidesteps all of it: an ``Authorization`` header is not
a cookie and is not subject to any SameSite/third-party-cookie policy in any
browser. The frontend stores it in ``localStorage`` and attaches it itself
(see ``src/api/client.js``) — this trades away the XSS protection an
httpOnly cookie would have given (a JS injection could read the token) for
something that actually works across these two domains, which running as
JSON is worth more here than the marginal defense-in-depth of httpOnly would
have been. This is also why the JWT session lifetime stays modest — a leaked
token expires rather than living forever.
"""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone

import jwt
import structlog
from fastapi import HTTPException, Request

from app.config import Settings

log = structlog.get_logger(__name__)

SESSION_LIFETIME = timedelta(days=30)
ALGORITHM = "HS256"


def verify_credentials(settings: Settings, username: str, password: str) -> bool:
    if not settings.auth_username or not settings.auth_password:
        return False
    user_ok = hmac.compare_digest(username.strip(), settings.auth_username)
    pass_ok = hmac.compare_digest(password, settings.auth_password)
    return user_ok and pass_ok


def create_session_token(settings: Settings, username: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": username, "iat": now, "exp": now + SESSION_LIFETIME}
    return jwt.encode(payload, settings.auth_secret, algorithm=ALGORITHM)


def _verify_session_token(settings: Settings, token: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.auth_secret, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
    return payload.get("sub")


async def require_auth(request: Request) -> str:
    """FastAPI dependency — raises 401 without a valid ``Authorization: Bearer`` header.

    Applied at the router level (see ``app/main.py``) rather than per-route,
    so a new route added later is protected by default instead of by
    remembering to add this.
    """
    from app.config import get_settings

    settings = get_settings()
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    username = _verify_session_token(settings, token)
    if username is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid.")
    return username
