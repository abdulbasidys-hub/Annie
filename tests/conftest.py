"""Shared fixtures.

The important one is :func:`isolated_memory`, which is autouse. Since the
2026-09-08 rewrite a lot of state — the ledger, the search index, scheduler
run bookkeeping, the Firestore budget counters — lives in a SQLite file under
the memory root. Without isolation every test in the session would share one
database and start seeing each other's rows, which is the kind of coupling
that produces failures depending on test *order*.

Each test gets its own temp directory and its own connection. That is fast
(SQLite file creation plus a dozen ``CREATE TABLE IF NOT EXISTS``) and it
means a test can freely write to memory without cleaning up.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_memory(tmp_path, monkeypatch):
    """Point the memory root at a per-test temp directory."""
    from app.config import get_settings
    from app.memory import db

    root = tmp_path / "memory"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ANNIE_MEMORY_DIR", str(root))
    monkeypatch.setenv("AUTH_SECRET", "test-secret")
    # A placeholder project, not the developer's real one. Settings reads the
    # repo's actual .env file as well as the environment, so without this the
    # suite picks up live Firebase credentials — which it did, and quietly
    # opened gRPC connections to the real project during a memory write.
    # Firestore is a REQUIRED capability, so this cannot simply be blanked:
    # config refuses to build at all without it.
    monkeypatch.setenv(
        "FIREBASE_SERVICE_ACCOUNT_JSON",
        '{"type": "service_account", "project_id": "annie-tests-placeholder"}',
    )
    monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT_FILE", "")
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "annie-tests-placeholder")

    # No scheduler, no bots. Beyond keeping the suite deterministic, this
    # stops a TestClient's lifespan from building a real Firestore client
    # (the scheduler's first act) against a placeholder credential, and from
    # opening a live Discord Gateway connection with the token in .env.
    monkeypatch.setenv("ANNIE_API_ONLY", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "")

    # The mirror is switched off at its own front door rather than by
    # withholding credentials. Every snapshot entry point consults this, so
    # nothing in the suite reaches the network — while the production code
    # path stays exactly as it ships.
    monkeypatch.setattr("app.memory.snapshot.is_configured", lambda: False)

    get_settings.cache_clear()
    db.close()
    try:
        yield root
    finally:
        db.close()
        get_settings.cache_clear()
