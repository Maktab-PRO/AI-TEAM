# Six control mechanisms — implementation contract

## 1. Project Registry

**Invariant:** every actionable project ID maps to exactly one repository; no repository maps to multiple project IDs. Unknown and non-active entries cannot create tasks. Registry edits are file/config changes subject to code review.

## 2. Boundary Guard

**Invariant:** task project ID and repository must match exactly. Repository uniqueness is case-insensitive. Mutating tasks must target a non-protected branch with an approved prefix, and current project policy is rechecked before claim, transition, evidence write, or verification. Relative paths may not traverse outside the repo or reach secret/key/CI-control paths. Destructive/external actions are denied until a separate approval/execution capability is implemented.

## 3. Task Orchestrator

**Invariant:** all states go through the transition table. Each task has an idempotency key; reuse with the same request returns the original task, while reuse with a different request is a conflict. Dependencies are intra-project and must be verified before claim. Leases are stored transactionally and have bounded expiry; an expired/missing lease cannot transition state until reclaimed. A blocked task must be requeued and claimed before work resumes.

## 4. Evidence Gate

**Invariant:** no direct `verified` status update. Each task type has a versioned set of required evidence kinds. Evidence must be passing, trusted, and from an allowed verification source. API clients cannot self-mark evidence trusted. Placeholder evidence URLs are rejected.

## 5. Durable State

**Invariant:** task state, dependency relation, audit event, and outbox record are committed in the same database transaction when they describe one state change. SQLite WAL/foreign keys/transactions provide local development durability. PostgreSQL uses row-level task locks plus advisory locks for idempotency keys and audit-chain serialization. Redis is not authoritative. `DATABASE_URL` must refer to a dedicated AI TEAM PostgreSQL database, never an existing product database.

## 6. Audit Log

**Invariant:** every material task/evidence state change writes an audit event in the same transaction. Events are canonicalized and linked with versioned SHA-256 hashes. Version 2 covers the task_id column and event content; legacy version-1 events remain readable but did not bind that column. A passing chain signals integrity since the last trusted checkpoint, not proof that the original actor was honest.

## Non-goals of milestone 0.1

- No automatic agent spawning or unattended/background orchestration.
- No provider API evidence verification yet.
- No production deployment, PR merge, destructive database operation, or secret management.
- No live Notion sync or Redis daemon management.
- No claim that SQLite has been validated for concurrent multi-instance production. Production bearer tokens map to fixed actor IDs, but project-/role-scoped RBAC, human identity, and rate limiting are not implemented; keep the service private.
