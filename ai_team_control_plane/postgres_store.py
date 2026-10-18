from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Iterator

from .store import DurableStore


_SCHEMA_LOCK_ID = 6_230_041_701
_AUDIT_LOCK_ID = 6_230_041_702


class _CompatRow(dict):
    """Mapping row compatible with sqlite3.Row's string and positional lookup."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


def _compat_row_factory(cursor):
    # DDL and advisory-lock statements have no result columns/rows.
    if cursor.description is None:
        return lambda values: values
    names = [column.name for column in cursor.description]

    def make_row(values):
        return _CompatRow(zip(names, values))

    return make_row


class _CompatConnection:
    """Keep the existing store SQL API portable while using psycopg 3."""

    def __init__(self, raw_connection):
        self.raw = raw_connection

    def execute(self, query: str, params=()):
        # The control-plane's parameterized SQL uses qmark placeholders for SQLite.
        # Its SQL statements do not use question marks inside string literals.
        return self.raw.execute(query.replace("?", "%s"), params or ())

    def commit(self) -> None:
        self.raw.execute("COMMIT")

    def rollback(self) -> None:
        self.raw.execute("ROLLBACK")

    def __getattr__(self, name):
        return getattr(self.raw, name)


class PostgresStore(DurableStore):
    """PostgreSQL-backed store with row locks and a serialized audit hash chain.

    It intentionally shares the store contract with DurableStore but owns its
    PostgreSQL DDL and transaction semantics. The SQLite file is never opened.
    """

    backend = "postgresql"

    def __init__(self, database_url: str):
        database_url = database_url.strip()
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL must use the postgresql:// or postgres:// scheme.")
        self.database_url = database_url
        self.db_path = database_url  # Kept for the common store interface; this is not a local path.
        self.initialize()

    @contextmanager
    def connection(self) -> Iterator[_CompatConnection]:
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL support requires psycopg. Install the project PostgreSQL dependency."
            ) from exc

        raw = psycopg.connect(
            self.database_url,
            autocommit=True,
            row_factory=_compat_row_factory,
            connect_timeout=10,
            application_name="ai-team-control-plane",
        )
        conn = _CompatConnection(raw)
        try:
            yield conn
        except Exception:
            try:
                if raw.info.transaction_status.name != "IDLE":
                    conn.rollback()
            finally:
                raw.close()
            raise
        else:
            raw.close()

    @contextmanager
    def transaction(self) -> Iterator[_CompatConnection]:
        with self.connection() as conn:
            conn.execute("BEGIN")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def initialize(self) -> None:
        schema = [
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
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at DESC)",
            """
            CREATE TABLE IF NOT EXISTS task_dependencies (
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                depends_on_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
                PRIMARY KEY (task_id, depends_on_task_id),
                CHECK (task_id <> depends_on_task_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS evidence (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('passed','failed','informational')),
                uri TEXT NOT NULL,
                artifact_sha256 TEXT,
                source TEXT NOT NULL,
                trusted SMALLINT NOT NULL CHECK (trusted IN (0,1)),
                verified_by TEXT,
                actor TEXT NOT NULL,
                notes TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_evidence_task_kind ON evidence(task_id, kind)",
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                task_id TEXT,
                actor TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                hash_version SMALLINT NOT NULL DEFAULT 1 CHECK (hash_version IN (1,2))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS outbox (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                aggregate_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                published_at TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_outbox_unpublished ON outbox(published_at, id)",
            """
            CREATE TABLE IF NOT EXISTS request_rate_limits (
                bucket_key TEXT PRIMARY KEY,
                window_start BIGINT NOT NULL,
                request_count INTEGER NOT NULL CHECK (request_count >= 1),
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_request_rate_limits_window ON request_rate_limits(window_start)",
            "ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS hash_version SMALLINT NOT NULL DEFAULT 1 CHECK (hash_version IN (1,2))",
            """
            CREATE OR REPLACE FUNCTION ai_team_prevent_audit_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'audit_events is append-only';
            END;
            $$
            """,
        ]

        with self.transaction() as conn:
            # Serialize startup migrations across service instances.
            conn.execute("SELECT pg_advisory_xact_lock(?)", (_SCHEMA_LOCK_ID,))
            for statement in schema:
                conn.execute(statement)
            for trigger_name, operation in (
                ("audit_events_no_update", "UPDATE"),
                ("audit_events_no_delete", "DELETE"),
            ):
                exists = conn.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname=? AND tgrelid='audit_events'::regclass AND NOT tgisinternal) AS present",
                    (trigger_name,),
                ).fetchone()["present"]
                if not exists:
                    conn.execute(
                        f"CREATE TRIGGER {trigger_name} BEFORE {operation} ON audit_events "
                        "FOR EACH ROW EXECUTE FUNCTION ai_team_prevent_audit_mutation()"
                    )

    def lock_task(self, conn: _CompatConnection, task_id: str):
        """Lock a task row inside the caller's transaction."""
        return conn.execute("SELECT * FROM tasks WHERE id = ? FOR UPDATE", (task_id,)).fetchone()

    def lock_idempotency_key(self, conn: _CompatConnection, idempotency_key: str) -> None:
        """Serialize same-key task creation before checking/inserting."""
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).digest()[:8]
        lock_key = int.from_bytes(digest, byteorder="big", signed=True)
        conn.execute("SELECT pg_advisory_xact_lock(?)", (lock_key,))

    def append_audit(
        self,
        conn: _CompatConnection,
        actor: str,
        event_type: str,
        payload: dict,
        task_id: str | None = None,
    ) -> None:
        # Without this lock, simultaneous transactions can fork the hash chain.
        conn.execute("SELECT pg_advisory_xact_lock(?)", (_AUDIT_LOCK_ID,))
        super().append_audit(conn, actor, event_type, payload, task_id)
