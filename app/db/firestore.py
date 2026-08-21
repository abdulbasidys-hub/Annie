"""Firestore client construction (replaces the old ``app/db/session.py``).

Uses the native async Firestore client (``google.cloud.firestore.AsyncClient``)
rather than the synchronous ``firebase-admin`` SDK, so every route stays
``async def`` end to end instead of hiding a blocking call behind a thread pool.

Authentication is a **service account**, never the browser/client SDK config
(apiKey, authDomain, ...) — that config cannot authenticate a server and grants
no Firestore access on its own. Two ways to supply the service account,
checked in order:

1. ``FIREBASE_SERVICE_ACCOUNT_JSON`` — the full key file's contents as one
   env var. This is the portable form: Railway, Render and most hosts let you
   paste a secret value but not upload a file, so this is what production uses.
2. ``FIREBASE_SERVICE_ACCOUNT_FILE`` — a path to the downloaded JSON file, for
   local development where dropping a file in the repo root is easier than
   pasting its contents into ``.env``. That file must never be committed —
   see ``.gitignore``.
"""

from __future__ import annotations

import json
from pathlib import Path

from google.auth.credentials import Credentials
from google.cloud.firestore import AsyncClient
from google.oauth2 import service_account

from app.config import ConfigurationError, get_settings

_client: AsyncClient | None = None


def _load_credentials_info(settings) -> dict:
    raw = (settings.firebase_service_account_json or "").strip()
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(
                "FIREBASE_SERVICE_ACCOUNT_JSON is not valid JSON. Paste the exact "
                "contents of the service-account key file downloaded from "
                "Firebase Console -> Project settings -> Service accounts."
            ) from exc

    file_path = (settings.firebase_service_account_file or "").strip()
    if file_path:
        path = Path(file_path)
        if not path.is_file():
            raise ConfigurationError(
                f"FIREBASE_SERVICE_ACCOUNT_FILE={file_path!r} does not exist."
            )
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigurationError(
                f"FIREBASE_SERVICE_ACCOUNT_FILE={file_path!r} is not valid JSON."
            ) from exc

    raise ConfigurationError(
        "No Firebase service account configured. Set FIREBASE_SERVICE_ACCOUNT_JSON "
        "(the key file's contents) or FIREBASE_SERVICE_ACCOUNT_FILE (a local path). "
        "This is the 'database' capability — see app/config.py."
    )


def _build_credentials(info: dict) -> Credentials:
    try:
        return service_account.Credentials.from_service_account_info(info)
    except (KeyError, ValueError) as exc:
        raise ConfigurationError(
            "FIREBASE_SERVICE_ACCOUNT_JSON/_FILE does not look like a service "
            "account key (expected 'type', 'project_id', 'private_key', "
            "'client_email'). This is the server credential from Firebase "
            "Console -> Project settings -> Service accounts -> Generate new "
            "private key — not the browser/web app config."
        ) from exc


def get_client() -> AsyncClient:
    """Process-wide Firestore client singleton. Built lazily, once."""
    global _client
    if _client is None:
        settings = get_settings()
        info = _load_credentials_info(settings)
        credentials = _build_credentials(info)
        project_id = settings.firebase_project_id.strip() or info.get("project_id")
        if not project_id:
            raise ConfigurationError(
                "Could not determine the Firebase project id. Set "
                "FIREBASE_PROJECT_ID or ensure the service account JSON "
                "includes 'project_id'."
            )
        _client = AsyncClient(project=project_id, credentials=credentials)
    return _client


async def get_db() -> AsyncClient:
    """FastAPI dependency — one shared client, not one per request.

    Firestore's client already pools its gRPC channel internally, so unlike
    the old SQLAlchemy session-per-request pattern, sharing one client across
    requests is the correct usage, not a leak.
    """
    return get_client()


async def dispose_client() -> None:
    """Called once at shutdown. Safe to call even if never initialised."""
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:  # pragma: no cover - best-effort cleanup
            pass
    _client = None
