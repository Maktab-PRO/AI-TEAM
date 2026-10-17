# Six control mechanisms — implementation contract

## 1. Project Registry

**Invariant:** every actionable project ID maps to exactly one repository; no repository maps to multiple project IDs. Unknown and non-active entries cannot create tasks. Registry edits are file/config changes subject to code review.

## 2. Boundary Guard

**Invariant:** task project ID and repository must match exactly. Repository uniqueness is case-insensitive. Mutating tasks must target a non-protected branch with an approved prefix, and current project policy is rechecked before claim, transition, evidence write, or verification. Relative paths may not traverse outside the repo or reach secret/key/CI-control paths. Destructive/external actions are denied until a separate approval/execution capability is implemented.

## 3. Task Orchestrator

**Invariant:** all states go through the transition table. Each task has an idempotency key; reuse with the same request returns the original task, while reuse with a different request is a conflict. Dependencies are intra-project and must be verified before claim. Leases are stored transactionally and have bounded expiry; an expired/missing lease cannot transition state until reclaimed. A blocked task must be requeued and claimed before work resumes.

## 4. Evidence Gate

**Invariant:** no direct `verified` status update. Each task type has a versioned set of required evidence kinds. Evidence must be passing, trusted, and from an allowed verification source. Generic API submissions are always untrusted. The GitHub Actions endpoint trusts a record only after checking the run's canonical API identity, repository, task branch, commit SHA, completed-success conclusion, allowlisted workflow path, and successful required test/security job signals. The server token is read-only and never supplied by the caller. The GitHub PR review endpoint separately verifies that the PR is open/unmerged, matches the registered repository/default base and task head branch, and has a non-author APPROVED review for the current head SHA with no latest CHANGES_REQUESTED state. Render health verification is allowed only for a project ID with an explicit configured HTTPS health URL and expected service/status markers. The verifier makes a GET request with redirects disabled and does not trigger a deploy or restart. Placeholder evidence URLs are rejected.

## 5. Durable State

**Invariant:** task state, dependency relation, audit event, and outbox record are committed in the same database transaction when they describe one state change. SQLite WAL/foreign keys/transactions provide local development durability. PostgreSQL uses row-level task locks plus advisory locks for idempotency keys and audit-chain serialization. Redis is not authoritative. `DATABASE_URL` must refer to a dedicated AI TEAM PostgreSQL database, never an existing product database.

## 6. Audit Log

**Invariant:** every material task/evidence state change writes an audit event in the same transaction. Events are canonicalized and linked with versioned SHA-256 hashes. Version 2 covers the task_id column and event content; legacy version-1 events remain readable but did not bind that column. A passing chain signals integrity since the last trusted checkpoint, not proof that the original actor was honest.

## API authorization

**Invariant:** production bearer tokens map to one fixed actor, one role, and an explicit list of registered project IDs. Only `platform_admin` can use the global `*` scope; that role must be configured with it explicitly. Task reads and mutations enforce project scope even after authentication. Workers cannot call verification; reviewers cannot create or claim tasks. Project-scoped roles cannot inspect the global audit chain or outbox. Identity-only legacy token maps fail closed. Development `X-Actor` behavior is local-only.

## Non-goals of milestone 0.1

- No automatic agent spawning or unattended/background orchestration.
- No automated production deployment or service restart. Render health verification performs only an allowlisted GET check; no live project URL is preconfigured. GitHub Actions and GitHub PR review verification use a read-only GitHub token; Actions additionally requires exact workflow allowlist entries.
- No production deployment, PR merge, destructive database operation, or secret management.
- No live Notion sync or Redis daemon management.
- No claim that SQLite has been validated for concurrent multi-instance production. Basic project-scoped roles are enforced in the API, but an external human identity provider, rate limiting, and provider-backed task evidence are not implemented; keep the service private.
