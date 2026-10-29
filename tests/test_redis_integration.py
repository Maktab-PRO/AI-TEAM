from __future__ import annotations

import json
import os
import uuid

import pytest

redis = pytest.importorskip("redis", reason="Redis integration tests require the optional redis extra")

from ai_team_control_plane.integrations.redis_outbox import RedisOutboxPublisher


REDIS_URL = os.getenv("AI_TEAM_TEST_REDIS_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not REDIS_URL,
    reason="AI_TEAM_TEST_REDIS_URL is not set; live Redis integration tests are disabled.",
)


def create_pending_task(service, *, key: str):
    return service.create_task(
        project_id="axsikent-it",
        title="Live Redis outbox integration test",
        description="Verify real Redis Stream publication semantics.",
        task_type="documentation",
        repository="Maktab-PRO/Axsikent-IT",
        branch="ai-team/redis-integration",
        paths=["docs/control-plane.md"],
        idempotency_key=key,
        actor="redis-integration-test",
    )


@pytest.fixture
def redis_client():
    client = redis.Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=5,
        health_check_interval=15,
    )
    try:
        client.ping()
    except redis.RedisError as exc:
        client.close()
        pytest.fail(f"Redis integration service is not healthy: {exc}")

    yield client
    client.close()


POSTGRES_URL = os.getenv("AI_TEAM_TEST_DATABASE_URL", "").strip()


@pytest.fixture
def postgres_system():
    if not POSTGRES_URL:
        pytest.skip("AI_TEAM_TEST_DATABASE_URL is not set; PostgreSQL+Redis integration is disabled.")
    from pathlib import Path

    from ai_team_control_plane.boundary import BoundaryGuard
    from ai_team_control_plane.orchestrator import TaskOrchestrator
    from ai_team_control_plane.postgres_store import PostgresStore
    from ai_team_control_plane.registry import ProjectRegistry

    root = Path(__file__).resolve().parents[1]
    registry = ProjectRegistry(root / "config" / "projects.json")
    store = PostgresStore(POSTGRES_URL)
    guard = BoundaryGuard(registry)
    return registry, store, guard, TaskOrchestrator(store, guard)


def test_live_redis_stream_publish_and_acknowledgement(system, redis_client):
    _, store, _, service = system
    task = create_pending_task(service, key=f"redis-publish-{uuid.uuid4()}")
    stream = f"ai-team:test:{uuid.uuid4()}"

    try:
        publisher = RedisOutboxPublisher(redis_client, store, stream_name=stream)
        first = publisher.publish_pending(limit=10)
        second = publisher.publish_pending(limit=10)

        assert first["published_count"] == 1
        assert len(first["outbox_ids"]) == 1
        assert second["published_count"] == 0
        assert first["delivery"] == "at_least_once"

        entries = redis_client.xrange(stream, min="-", max="+")
        assert len(entries) == 1
        fields = entries[0][1]
        assert fields["outbox_id"] == str(first["outbox_ids"][0])
        assert fields["aggregate_id"] == task["id"]
        assert fields["event_type"] == "task.created"
        assert json.loads(fields["payload"])["task_id"] == task["id"]
        assert store.list_outbox(limit=10) == []
    finally:
        redis_client.delete(stream)


def test_live_redis_publish_crash_window_retries_same_outbox_id(system, redis_client):
    _, store, _, service = system
    task = create_pending_task(service, key=f"redis-retry-{uuid.uuid4()}")
    stream = f"ai-team:test:{uuid.uuid4()}"

    class PublishThenFailOnce:
        def __init__(self, delegate):
            self.delegate = delegate
            self.failed = False

        def xadd(self, name, fields):
            message_id = self.delegate.xadd(name, fields)
            if not self.failed:
                self.failed = True
                # Simulate process failure after Redis accepted XADD but before
                # the relational outbox acknowledgement can be committed.
                raise RuntimeError("simulated crash after XADD")
            return message_id

    try:
        flaky = RedisOutboxPublisher(PublishThenFailOnce(redis_client), store, stream_name=stream)
        with pytest.raises(RuntimeError, match="simulated crash after XADD"):
            flaky.publish_pending(limit=10)

        pending = store.list_outbox(limit=10)
        assert len(pending) == 1
        outbox_id = str(pending[0]["id"])
        assert pending[0]["aggregate_id"] == task["id"]

        recovered = RedisOutboxPublisher(redis_client, store, stream_name=stream)
        result = recovered.publish_pending(limit=10)
        assert result["published_count"] == 1
        assert result["outbox_ids"] == [pending[0]["id"]]
        assert store.list_outbox(limit=10) == []

        entries = redis_client.xrange(stream, min="-", max="+")
        assert len(entries) == 2
        published_ids = [fields["outbox_id"] for _, fields in entries]
        assert published_ids == [outbox_id, outbox_id]
        # At-least-once delivery intentionally permits this duplicate. Consumers
        # must deduplicate by outbox_id, rather than treating Redis entry ID as identity.
    finally:
        redis_client.delete(stream)

def test_live_redis_stream_publishes_postgres_outbox(postgres_system, redis_client):
    _, store, _, service = postgres_system
    task = create_pending_task(service, key=f"redis-postgres-publish-{uuid.uuid4()}")
    stream = f"ai-team:test:{uuid.uuid4()}"

    try:
        publisher = RedisOutboxPublisher(redis_client, store, stream_name=stream)
        result = publisher.publish_pending(limit=10)

        assert result["published_count"] == 1
        assert len(result["outbox_ids"]) == 1
        entries = redis_client.xrange(stream, min="-", max="+")
        assert len(entries) == 1
        fields = entries[0][1]
        assert fields["outbox_id"] == str(result["outbox_ids"][0])
        assert fields["aggregate_id"] == task["id"]
        assert fields["event_type"] == "task.created"
        assert json.loads(fields["payload"])["task_id"] == task["id"]
        assert store.list_outbox(limit=10) == []
    finally:
        redis_client.delete(stream)


def test_postgres_outbox_redelivers_same_id_after_crash_between_redis_and_ack(postgres_system, redis_client):
    _, store, _, service = postgres_system
    task = create_pending_task(service, key=f"redis-postgres-retry-{uuid.uuid4()}")
    stream = f"ai-team:test:{uuid.uuid4()}"

    class PublishThenFailOnce:
        def __init__(self, delegate):
            self.delegate = delegate
            self.failed = False

        def xadd(self, name, fields):
            message_id = self.delegate.xadd(name, fields)
            if not self.failed:
                self.failed = True
                raise RuntimeError("simulated crash after XADD")
            return message_id

    try:
        flaky = RedisOutboxPublisher(PublishThenFailOnce(redis_client), store, stream_name=stream)
        with pytest.raises(RuntimeError, match="simulated crash after XADD"):
            flaky.publish_pending(limit=10)

        pending = store.list_outbox(limit=10)
        assert len(pending) == 1
        outbox_id = str(pending[0]["id"])
        assert pending[0]["aggregate_id"] == task["id"]

        result = RedisOutboxPublisher(redis_client, store, stream_name=stream).publish_pending(limit=10)
        assert result["published_count"] == 1
        assert result["outbox_ids"] == [pending[0]["id"]]
        assert store.list_outbox(limit=10) == []

        entries = redis_client.xrange(stream, min="-", max="+")
        assert len(entries) == 2
        assert [fields["outbox_id"] for _, fields in entries] == [outbox_id, outbox_id]
    finally:
        redis_client.delete(stream)


