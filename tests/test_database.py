from __future__ import annotations

import pytest

from ai_team_control_plane import database
from ai_team_control_plane.store import DurableStore


def test_development_defaults_to_file_backed_sqlite(monkeypatch, tmp_path):
    db_path = tmp_path / "control-plane.sqlite3"
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AI_TEAM_DB_PATH", str(db_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    store = database.create_store()

    assert isinstance(store, DurableStore)
    assert store.backend == "sqlite"
    assert db_path.exists()


def test_production_fails_closed_without_postgresql_database_url(monkeypatch, tmp_path):
    db_path = tmp_path / "must-not-be-created.sqlite3"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AI_TEAM_DB_PATH", str(db_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="DATABASE_URL is required in production"):
        database.create_store()

    assert not db_path.exists()


def test_production_selects_postgresql_backend(monkeypatch):
    import ai_team_control_plane.postgres_store as postgres_module

    expected_url = "postgresql://control-plane:secret@db.example.invalid/ai_team?sslmode=require"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", expected_url)

    class FakePostgresStore:
        def __init__(self, url):
            self.database_url = url
            self.backend = "postgresql"

    monkeypatch.setattr(postgres_module, "PostgresStore", FakePostgresStore)

    selected = database.create_store()

    assert isinstance(selected, FakePostgresStore)
    assert selected.backend == "postgresql"
    assert selected.database_url == expected_url
