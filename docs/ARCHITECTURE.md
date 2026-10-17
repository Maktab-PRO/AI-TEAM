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
      +---- single SQLite transaction ----+
      | task state / dependency / lease   |
      | audit hash-chain append           |
      | transactional outbox append       |
      +-----------------------------------+
      |
      +---- Evidence Gate <---- trusted verifier adapters (future: GitHub Actions, GitHub Review, Render health, security scanner)
      |
      +---- optional outbox publisher ----> Redis Stream (at-least-once)

Notion: human-readable decisions, runbooks, architecture pages; not authoritative task state.
```

## Source of truth by domain

- Project↔repository identity: `config/projects.json`, changed by reviewed commit.
- Active task state, lease, dependencies, evidence index, audit events, and outbox: durable database.
- CI/deployment/review truth: provider API responses checked by trusted adapters. The MVP does not yet implement those network verifiers, so evidence cannot be promoted to trusted by the public API.
- Human reference docs and decision records: Notion can be used after a deliberate integration; not a substitute for audit state.
- Redis: asynchronous message transport/cache only. Do not use it as the sole record of a task or project.

## State machine

`queued → claimed → in_progress → evidence_pending → verified`

Other supported transitions: `draft → queued`, `claimed → blocked|queued`, `in_progress → blocked|queued|cancelled`, `blocked → queued|in_progress|cancelled`, `evidence_pending → in_progress|rejected`, `rejected → in_progress|cancelled`. `verified` and `cancelled` are terminal. The API cannot set `verified` directly.

A worker lease and the task state update happen under a write transaction. The database stores an expiry. A different worker cannot steal an unexpired lease. The lease owner must match the actor for ordinary transitions, transitions use a required expected-version precondition through the API, and evidence updates bump the aggregate version. Dependencies must be in the same project and `verified` before a dependent task can be claimed.

## Evidence requirements

- `implementation`: passing automated tests, required CI, approved independent review.
- `bugfix`: regression test, all tests, required CI, approved review.
- `deployment`: tests, CI, post-deploy health check, rollback plan.
- `security`: security scan, tests, security review.
- `documentation`: documentation review.

Trusted evidence requires an internal verifier adapter to supply both an approved source and `verified_by`. Public API routes intentionally omit these privileged fields. Tests use a fixture verifier only to validate gate behavior; it is not production evidence.

## Durable storage and scaling

SQLite is suitable for the first local/single-instance milestone. For multiple concurrent service instances, migrate the same transaction boundaries and constraints to PostgreSQL. Use a dedicated AI TEAM database, separate from Axsikent and Technikum databases. Redis Streams uses at-least-once delivery; outbox consumers must deduplicate by `outbox_id`. No background worker is started implicitly by importing the API module.

## Failure policy

Unknown project, missing repository mapping, quarantined project, cross-project repository mismatch, protected branch write, unsafe path, cross-project dependency, invalid transition, expired or conflicting lease, missing/untrusted evidence, or a broken audit chain must fail closed and be visible in the response/logs.
