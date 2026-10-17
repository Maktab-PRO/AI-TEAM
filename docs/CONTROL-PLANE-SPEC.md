# Six control mechanisms — implementation contract

## 1. Project Registry

**Invariant:** every actionable project ID maps to exactly one repository; no repository maps to multiple project IDs. Unknown and non-active entries cannot create tasks. Registry edits are file/config changes subject to code review.

## 2. Boundary Guard

**Invariant:** task project ID and repository must match exactly. Mutating tasks must target a non-protected branch with an approved prefix. Relative paths may not traverse outside the repo or reach secret/key/CI-control paths. Destructive/external actions are denied until a separate approval/execution capability is implemented.

## 3. Task Orchestrator

**Invariant:** all states go through the transition table. Each task has an idempotency key; reuse with the same request returns the original task, while reuse with a different request is a conflict. Dependencies are intra-project and must be verified before claim. Leases are stored transactionally and have bounded expiry.

## 4. Evidence Gate

**Invariant:** no direct `verified` status update. Each task type has a versioned set of required evidence kinds. Evidence must be passing, trusted, and from an allowed verification source. API clients cannot self-mark evidence trusted. Placeholder evidence URLs are rejected.

## 5. Durable State

**Invariant:** task state, dependency relation, audit event, and outbox record are committed in the same database transaction when they describe one state change. SQLite WAL/foreign keys/transactions provide single-node durability. Redis is not authoritative. Before multi-instance production use, move to a dedicated PostgreSQL database with equivalent transaction guarantees.

## 6. Audit Log

**Invariant:** every material task/evidence state change writes an audit event in the same transaction. Events are canonicalized and linked with SHA-256 hashes. The verify endpoint reports the first mismatch; a passing chain signals integrity since the last trusted checkpoint, not proof that the original actor was honest.

## Non-goals of milestone 0.1

- No automatic agent spawning or unattended/background orchestration.
- No provider API evidence verification yet.
- No production deployment, PR merge, destructive database operation, or secret management.
- No live Notion sync or Redis daemon management.
- No claim that SQLite has been validated for concurrent multi-instance production.
