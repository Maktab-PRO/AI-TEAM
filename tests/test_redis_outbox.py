import json

from ai_team_control_plane.integrations.redis_outbox import RedisOutboxPublisher


class FakeRedis:
    def __init__(self):
        self.events = []

    def xadd(self, stream_name, fields):
        self.events.append((stream_name, fields))
        return str(len(self.events))


def test_outbox_publisher_is_explicit_and_idempotently_acknowledges(system):
    _, store, _, service = system
    service.create_task(
        project_id="axsikent-it",
        title="Outbox publish test",
        description="",
        task_type="documentation",
        repository="Maktab-PRO/Axsikent-IT",
        branch="ai-team/outbox-test",
        idempotency_key="outbox-publish-test",
        actor="test-suite",
    )
    redis = FakeRedis()
    publisher = RedisOutboxPublisher(redis, store)
    first = publisher.publish_pending()
    second = publisher.publish_pending()
    assert first["published_count"] == 1
    assert second["published_count"] == 0
    assert first["delivery"] == "at_least_once"
    assert redis.events[0][0] == "ai-team:events"
    assert "outbox_id" in redis.events[0][1]
    assert json.loads(redis.events[0][1]["payload"])["task_id"]
