from __future__ import annotations

import os
from pathlib import Path

from .store import DurableStore

ROOT = Path(__file__).resolve().parents[1]


def create_store():
    """Choose the durable backend from environment configuration.

    DATABASE_URL takes precedence and must reference a dedicated AI TEAM
    PostgreSQL database. With no URL, retain the existing local SQLite mode.
    """
    database_url = os.getenv("DATABASE_URL", "").strip()
    environment = os.getenv("APP_ENV", "development").strip().lower()
    if database_url:
        from .postgres_store import PostgresStore

        return PostgresStore(database_url)

    if environment == "production":
        raise RuntimeError(
            "DATABASE_URL is required in production. Configure a dedicated AI TEAM PostgreSQL database; "
            "SQLite is permitted only for local/development mode."
        )

    db_path = Path(os.getenv("AI_TEAM_DB_PATH", ROOT / "data" / "ai-team.sqlite3"))
    return DurableStore(db_path)
