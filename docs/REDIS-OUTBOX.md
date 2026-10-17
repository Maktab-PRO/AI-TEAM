# Redis event delivery

Redis can carry asynchronous state-change notifications, but it is not the durable system of record.

- Durable tasks/evidence/audit/outbox rows are committed to SQLite/PostgreSQL first.
- A publisher then sends unpublished outbox rows to the `ai-team:events` Redis Stream.
- Delivery is at least once: a crash after `XADD` but before marking the outbox row published can re-send it.
- Consumers must store/deduplicate `outbox_id`; never assume exactly-once delivery.
- The included adapter is explicit. It does not start a scheduler or background thread.
- For production, use the `redis` optional dependency and a secret-store `REDIS_URL` with TLS/authentication. Do not expose Redis publicly.
