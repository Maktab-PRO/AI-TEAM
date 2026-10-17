# Architecture and trust boundaries

## Components and data flow

```text
Human / approved agent
      |
      v
Authenticated API -----> Project Registry (read-only config)
      |                            |
      v                            v
Task Orchestrator <---------- Boundary Guard
      |
      +---- one database transaction --------+
      | SQLite locally / PostgreSQL remotely  |
      | task state / dependency / lease   |
      | audit hash-chain append           |
      | transactional outbox append       |
      +-----------------------------------+
      |
      +---- Evidence Gate <---- GitHub Actions REST verifier (implemented; exact workflow allowlist required)
      |                  <---- GitHub Pull Request review REST verifier (implemented)
      |                  <---- Render health verifier (implemented; per-project endpoint allowlist required)
      |
      +---- optional outbox publisher ----> Redis Stream (at-least-once)

Notion: human-readable decisions, runbooks, architecture pages; not authoritative task state.
```

## Source of truth by domain

- Project↔repository identity: `config/projects.json`, changed by reviewed commit.
- Active task state, lease, dependencies, evidence index, audit events, and outbox: durable database.
- CI truth: the server-side GitHub Actions verifier checks repository identity, task branch, run ID/canonical URL, commit SHA, completion conclusion, an exact allowlisted workflow path, and successful required test/security jobs before recording trusted evidence.
- PR review truth: the server-side GitHub Pull Requests verifier checks repository identity, configured default base branch, task head branch, open/unmerged state, latest review states, author exclusion, and an APPROVED review on the exact current head SHA.
- Render health truth: the verifier uses only a configured project-to-URL allowlist, makes a bounded HTTPS GET to the exact health endpoint without redirects, and requires both configured service and status JSON markers to match. No endpoint is preconfigured and the verifier cannot deploy or restart a service.
- A URL supplied through the generic public evidence endpoint remains untrusted.
- Human reference docs and decision records: Notion can be used after a deliberate integration; not a substitute for audit state.
- Redis: asynchronous message transport/cache only. Do not use it as the sole record of a task or project.

## State machine

`queued → claimed → in_progress → evidence_pending → verified`

Other supported transitions: `draft → queued`, `claimed → blocked|queued`, `in_progress → blocked|queued|cancelled`, `blocked → queued|cancelled`, `evidence_pending → in_progress|rejected`, `rejected → in_progress|cancelled`. A blocked task must be requeued and claimed to restore a worker lease before work resumes. `verified` and `cancelled` are terminal. The API cannot set `verified` directly.

A worker lease and the task state update happen under a write transaction. The database stores an expiry. A different worker cannot steal an unexpired lease; an expired lease must be reclaimed before a state transition, and expired in_progress tasks can be reassigned without losing their state. The lease owner must match the authenticated actor for ordinary transitions, transitions use a required expected-version precondition through the API, and evidence updates bump the aggregate version. Project/repository/branch/path boundaries are rechecked before task-state, evidence, or verification writes. Dependencies must be in the same project and `verified` before a dependent task can be claimed.

## Evidence requirements

- `implementation`: passing automated tests, required CI, approved independent review.
- `bugfix`: regression test, all tests, required CI, approved review.
- `deployment`: tests, CI, post-deploy health check, rollback plan.
- `security`: security scan, tests, security review.
- `documentation`: documentation review.

Trusted evidence requires an internal verifier adapter to supply both an approved source and `verified_by`. The generic public evidence route never accepts trust claims. The `POST /tasks/{id}/evidence/github-actions` route requires a server-side read-only token and a per-repository workflow path allowlist. The `POST /tasks/{id}/evidence/github-review` route requires the server-side token and verifies real PR/review responses; it rejects self-approval, stale approvals, and outstanding change requests. The `POST /tasks/{id}/evidence/render-health` route checks only an explicitly configured `/health` URL, with redirects disabled and expected service/status markers. Unit-test fixture evidence is not production evidence.

## Durable storage and scaling

Audit hash version 2 binds the task_id database column as well as event metadata/content. Earlier version-1 records remain verifiable for compatibility, but their task ID column was not part of the original hash. The current chain has no external signed checkpoint, so a full database rewrite by a privileged attacker is outside the protection boundary.

SQLite is suitable for local/single-instance development. When `DATABASE_URL` is set, `PostgresStore` uses PostgreSQL transactions, row-level locks for task mutations, per-idempotency-key advisory locks, a schema migration advisory lock, and a global transaction advisory lock to serialize audit-chain appends across workers. Use a dedicated AI TEAM database, separate from Axsikent and Technikum databases. CI uses a separate ephemeral PostgreSQL database to exercise concurrent claims, idempotency, the hash chain, and backup/restore. The presence of a PostgreSQL backend does not by itself establish a production-ready deployment; backup retention, credentials, TLS, migrations, monitoring, and restore drills still need to be configured for the actual host. Redis Streams uses at-least-once delivery; outbox consumers must deduplicate by `outbox_id`. No background worker is started implicitly by importing the API module.

## Failure policy

Unknown project, missing repository mapping, quarantined project, cross-project repository mismatch, protected branch write, unsafe path, cross-project dependency, invalid transition, expired or conflicting lease, missing/untrusted evidence, or a broken audit chain must fail closed and be visible in the response/logs. Production bearer tokens map to a fixed actor, role, and exact project scope. Workers cannot invoke the Evidence Gate; reviewers cannot create or claim tasks; non-platform roles cannot access global audit/outbox endpoints. `X-Actor` is only a development trace label and cannot override production identity. This does not replace an external human identity provider, rate limiting, or infrastructure-level authorization.
