from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


ZERO_HASH = "0" * 64


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class DurableStore:
    """Transactional SQLite store for a single-node MVP; PostgreSQL is the production multi-node target."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        if self.db_path == ":memory:":
            raise ValueError("DurableStore requires a file-backed database path; in-memory SQLite is not durable across connections.")
        Path(self.db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    paths_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('draft','queued','claimed','in_progress','blocked','evidence_pending','verified','rejected','cancelled')),
                    priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 5),
                    assigned_role TEXT NOT NULL,
                    creator TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_hash TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    lease_owner TEXT,
                    lease_until TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project_id, status);
                CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at DESC);

                CREATE TABLE IF NOT EXISTS task_dependencies (
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    depends_on_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
                    PRIMARY KEY (task_id, depends_on_task_id),
                    CHECK (task_id <> depends_on_task_id)
                );

                CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('passed','failed','informational')),
                    uri TEXT NOT NULL,
                    artifact_sha256 TEXT,
                    source TEXT NOT NULL,
                    trusted INTEGER NOT NULL CHECK (trusted IN (0,1)),
                    verified_by TEXT,
                    actor TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_task_kind ON evidence(task_id, kind);

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT,
                    actor TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    hash_version INTEGER NOT NULL DEFAULT 1 CHECK (hash_version IN (1,2))
                );

                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    aggregate_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    published_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_unpublished ON outbox(published_at, id);

                CREATE TRIGGER IF NOT EXISTS audit_events_no_update
                BEFORE UPDATE ON audit_events
                BEGIN
                    SELECT RAISE(ABORT, 'audit_events is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
                BEFORE DELETE ON audit_events
                BEGIN
                    SELECT RAISE(ABORT, 'audit_events is append-only');
                END;
                """
            )
            # Keep existing v1 hashes readable while new records use v2.
            audit_columns = {row[1] for row in conn.execute("PRAGMA table_info(audit_events)").fetchall()}
            if "hash_version" not in audit_columns:
                conn.execute(
                    "ALTER TABLE audit_events ADD COLUMN hash_version INTEGER NOT NULL DEFAULT 1 CHECK (hash_version IN (1,2))"
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def append_audit(self, conn: sqlite3.Connection, actor: str, event_type: str, payload: dict, task_id: str | None = None) -> None:
        prior = conn.execute("SELECT event_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        previous_hash = prior["event_hash"] if prior else ZERO_HASH
        payload_json = canonical_json(payload)
        created_at = utc_now()
        # v2 binds the audit row's task_id column into the hash.
        hash_version = 2
        material = "\n".join((previous_hash, task_id or "", actor, event_type, payload_json, created_at))
        event_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT INTO audit_events(task_id, actor, event_type, payload_json, prev_hash, event_hash, created_at, hash_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, actor, event_type, payload_json, previous_hash, event_hash, created_at, hash_version),
        )

    def append_outbox(self, conn: sqlite3.Connection, aggregate_id: str, event_type: str, payload: dict) -> None:
        conn.execute(
            "INSERT INTO outbox(aggregate_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (aggregate_id, event_type, canonical_json(payload), utc_now()),
        )

    @staticmethod
    def task_dict(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        value = dict(row)
        value["paths"] = json.loads(value.pop("paths_json"))
        value["dependencies"] = []
        return value

    def get_task(self, task_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            value = self.task_dict(row)
            if value is not None:
                value["dependencies"] = [r[0] for r in conn.execute("SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ? ORDER BY depends_on_task_id", (task_id,)).fetchall()]
            return value

    def list_tasks(self, project_id: str | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        query = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
            results = [self.task_dict(row) for row in rows]
            for item in results:
                item["dependencies"] = [r[0] for r in conn.execute("SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ? ORDER BY depends_on_task_id", (item["id"],)).fetchall()]
            return results

    def list_evidence(self, task_id: str) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM evidence WHERE task_id = ? ORDER BY created_at, id", (task_id,)).fetchall()
            return [{**dict(row), "trusted": bool(row["trusted"])} for row in rows]

    def verify_audit_chain(self) -> dict:
        previous_hash = ZERO_HASH
        total = 0
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        for row in rows:
            expected_prev = previous_hash
            hash_version = row["hash_version"] if "hash_version" in row.keys() else 1
            if hash_version == 1:
                # Compatibility for records created before task_id was included in the hash.
                material = "\n".join((expected_prev, row["actor"], row["event_type"], row["payload_json"], row["created_at"]))
            elif hash_version == 2:
                material = "\n".join((expected_prev, row["task_id"] or "", row["actor"], row["event_type"], row["payload_json"], row["created_at"]))
            else:
                return {"valid": False, "checked_events": total, "failed_event_id": row["id"], "reason": "unsupported_hash_version"}
            expected_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
            if row["prev_hash"] != expected_prev or row["event_hash"] != expected_hash:
                return {"valid": False, "checked_events": total, "failed_event_id": row["id"], "reason": "hash_or_previous_hash_mismatch"}
            previous_hash = row["event_hash"]
            total += 1
        return {"valid": True, "checked_events": total, "head_hash": previous_hash}

    def list_outbox(self, limit: int = 100) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM outbox WHERE published_at IS NULL ORDER BY id LIMIT ?", (max(1, min(limit, 1000)),)).fetchall()
        return [dict(row) for row in rows]
