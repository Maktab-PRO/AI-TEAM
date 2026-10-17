from __future__ import annotations

import json
from datetime import datetime, timezone

from ..store import DurableStore


class RedisOutboxPublisher:
    """Publishes committed outbox events to a Redis Stream using at-least-once delivery.

    The relational store remains authoritative. Consumers should deduplicate using `outbox_id`.
    This component is invoked explicitly; it does not start a background worker by itself.
    """

    def __init__(self, redis_client, store: DurableStore, stream_name: str = "ai-team:events"):
        self.redis = redis_client
        self.store = store
        self.stream_name = stream_name

    def publish_pending(self, limit: int = 100) -> dict:
        published = []
        for event in self.store.list_outbox(limit=limit):
            # Publishing and SQLite acknowledgement cannot share one transaction.
            # A crash between XADD and acknowledgement may duplicate a message; outbox_id is the dedupe key.
            self.redis.xadd(
                self.stream_name,
                {
                    "outbox_id": str(event["id"]),
                    "aggregate_id": event["aggregate_id"],
                    "event_type": event["event_type"],
                    "payload": event["payload_json"],
                    "created_at": event["created_at"],
                },
            )
            now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            with self.store.transaction() as conn:
                conn.execute(
                    "UPDATE outbox SET published_at=? WHERE id=? AND published_at IS NULL",
                    (now, event["id"]),
                )
            published.append(event["id"])
        return {"published_count": len(published), "outbox_ids": published, "delivery": "at_least_once"}
