from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from ai_team_control_plane.boundary import BoundaryGuard
from ai_team_control_plane.models import StateConflict
from ai_team_control_plane.orchestrator import TaskOrchestrator
from ai_team_control_plane.postgres_store import PostgresStore
from ai_team_control_plane.registry import ProjectRegistry
from ai_team_control_plane.store import utc_now


DATABASE_URL = os.getenv("AI_TEAM_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AI_TEAM_TEST_DATABASE_URL is not set")


@pytest.fixture(scope="module")
def postgres_system():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    registry = ProjectRegistry(root / "config" / "projects.json")
    store = PostgresStore(DATABASE_URL)
    guard = BoundaryGuard(registry)
    return registry, store, guard, TaskOrchestrator(store, guard)


def create_task(service, *, key=None, title="PostgreSQL integration task"):
    return service.create_task(
        project_id="axsikent-it",
        title=title,
        description="PostgreSQL backend integration test",
        task_type="documentation",
        repository="Maktab-PRO/Axsikent-IT",
        branch="ai-team/postgres-integration",
        paths=["docs/control-plane.md"],
        idempotency_key=key or f"pg-{uuid.uuid4()}",
        actor="postgres-integration-test",
    )


def test_postgres_task_persistence_audit_and_outbox(postgres_system):
    _, store, _, service = postgres_system
    task = create_task(service)
    reopened = PostgresStore(DATABASE_URL)
    loaded = reopened.get_task(task["id"])
    assert loaded is not None
    assert loaded["repository"] == "Maktab-PRO/Axsikent-IT"
    assert loaded["status"] == "queued"
    assert loaded["paths"] == ["docs/control-plane.md"]

    audit = reopened.verify_audit_chain()
    assert audit["valid"] is True
    assert audit["checked_events"] >= 1
    assert any(event["aggregate_id"] == task["id"] for event in reopened.list_outbox(limit=1000))


def test_postgres_same_idempotency_key_returns_original_task(postgres_system):
    _, _, _, service = postgres_system
    key = f"same-key-{uuid.uuid4()}"
    first = create_task(service, key=key, title="Idempotent PostgreSQL task")
    second = create_task(service, key=key, title="Idempotent PostgreSQL task")
    assert first["id"] == second["id"]


def test_postgres_transaction_rolls_back_all_statements(postgres_system):
    _, store, _, _ = postgres_system
    with store.connection() as conn:
        before = conn.execute("SELECT COUNT(*) AS total FROM outbox").fetchone()["total"]

    with pytest.raises(RuntimeError, match="force rollback"):
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO outbox(aggregate_id,event_type,payload_json,created_at) VALUES (?,?,?,?)",
                ("rollback-test", "test.rollback", "{}", utc_now()),
            )
            raise RuntimeError("force rollback")

    with store.connection() as conn:
        after = conn.execute("SELECT COUNT(*) AS total FROM outbox").fetchone()["total"]
    assert after == before


def test_postgres_only_one_concurrent_worker_can_claim_a_task(postgres_system):
    _, _, _, service = postgres_system
    task = create_task(service)
    barrier = threading.Barrier(2)

    def claim(worker):
        barrier.wait(timeout=10)
        try:
            value = service.claim_task(task["id"], worker_id=worker, actor=worker, lease_seconds=120)
            return ("claimed", value["lease_owner"])
        except StateConflict:
            return ("conflict", worker)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, ["worker-postgres-a", "worker-postgres-b"]))

    winners = [value for status, value in outcomes if status == "claimed"]
    conflicts = [value for status, value in outcomes if status == "conflict"]
    assert len(winners) == 1, outcomes
    assert len(conflicts) == 1, outcomes
    assert store_owner(service, task["id"]) == winners[0]


def store_owner(service, task_id):
    task = service.store.get_task(task_id)
    return task["lease_owner"]


def test_postgres_concurrent_same_key_creation_is_serialized(postgres_system):
    _, _, _, service = postgres_system
    key = f"parallel-same-key-{uuid.uuid4()}"
    barrier = threading.Barrier(2)

    def create(actor):
        barrier.wait(timeout=10)
        return service.create_task(
            project_id="axsikent-it",
            title="Concurrent idempotency test",
            description="Same request from two callers",
            task_type="documentation",
            repository="Maktab-PRO/Axsikent-IT",
            branch="ai-team/postgres-integration",
            paths=["docs/control-plane.md"],
            idempotency_key=key,
            actor=actor,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(create, ["requester-a", "requester-b"]))
    assert values[0]["id"] == values[1]["id"]


def test_postgres_parallel_audit_appends_remain_one_valid_chain(postgres_system):
    _, store, _, service = postgres_system

    def create(index):
        return create_task(
            service,
            key=f"parallel-audit-{uuid.uuid4()}",
            title=f"Parallel audit writer {index}",
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        tasks = list(pool.map(create, range(12)))

    result = store.verify_audit_chain()
    assert result["valid"] is True
    assert result["checked_events"] >= len(tasks)
