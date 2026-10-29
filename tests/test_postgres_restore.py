from __future__ import annotations

import os

import pytest

from ai_team_control_plane.postgres_store import PostgresStore


DATABASE_URL = os.getenv("AI_TEAM_RESTORE_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AI_TEAM_RESTORE_DATABASE_URL is not set")


def test_postgresql_logical_backup_restore_preserves_data_and_audit_chain():
    store = PostgresStore(DATABASE_URL)
    with store.connection() as conn:
        task_count = conn.execute("SELECT COUNT(*) AS total FROM tasks").fetchone()["total"]
        outbox_count = conn.execute("SELECT COUNT(*) AS total FROM outbox").fetchone()["total"]
        audit_count = conn.execute("SELECT COUNT(*) AS total FROM audit_events").fetchone()["total"]

    assert task_count >= 1, "Backup/restore test database should include integration-test tasks."
    assert outbox_count >= 1, "Backup/restore must preserve the transactional outbox."
    assert audit_count >= 1, "Backup/restore must preserve audit events."
    result = store.verify_audit_chain()
    assert result["valid"] is True
    assert result["checked_events"] == audit_count
