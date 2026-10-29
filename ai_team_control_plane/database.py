from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .store import DurableStore

ROOT = Path(__file__).resolve().parents[1]

_PRODUCTION_SSL_MODES = {"require", "verify-ca", "verify-full"}


def _validate_production_database_url(database_url: str) -> None:
    """Fail closed when production PostgreSQL transport TLS is absent or unsafe."""
    try:
        parsed = urlsplit(database_url)
        port = parsed.port  # Validate malformed ports without exposing the URL.
        query = parse_qs(parsed.query, keep_blank_values=True)
    except ValueError:
        raise RuntimeError(
            "Production DATABASE_URL must be a valid PostgreSQL URL with an explicit supported TLS mode."
        ) from None

    ssl_modes = query.get("sslmode", [])
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or not parsed.hostname
        or parsed.path in {"", "/"}
        or port == 0
        or len(ssl_modes) != 1
        or ssl_modes[0].lower() not in _PRODUCTION_SSL_MODES
    ):
        raise RuntimeError(
            "Production DATABASE_URL must be a PostgreSQL URL with a host, database path, "
            "and sslmode=require, verify-ca, or verify-full."
        )


def create_store():
    """Choose the durable backend from environment configuration.

    DATABASE_URL takes precedence and must reference a dedicated AI TEAM
    PostgreSQL database. With no URL, retain the existing local SQLite mode.
    """
    database_url = os.getenv("DATABASE_URL", "").strip()
    environment = os.getenv("APP_ENV", "development").strip().lower()
    if database_url:
        if environment == "production":
            _validate_production_database_url(database_url)
        from .postgres_store import PostgresStore

        return PostgresStore(database_url)

    if environment == "production":
        raise RuntimeError(
            "DATABASE_URL is required in production. Configure a dedicated AI TEAM PostgreSQL database; "
            "SQLite is permitted only for local/development mode."
        )

    db_path = Path(os.getenv("AI_TEAM_DB_PATH", ROOT / "data" / "ai-team.sqlite3"))
    return DurableStore(db_path)
