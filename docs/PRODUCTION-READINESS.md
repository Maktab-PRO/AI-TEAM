# Production Readiness Gate — AI TEAM Control Plane

**Status: BLOCKED / NOT APPROVED FOR PRODUCTION.** This document is an execution gate, not a claim that any external service has been provisioned. Milestone 0.1 has passed repository CI against isolated test services; that does not establish production readiness.

## Non-negotiable project boundary

All work in this runbook applies only to `Maktab-PRO/AI-TEAM`. Never reuse a product database, Redis instance, API token, deploy service, or environment file from Axsikent IT, Technikum Digital Platform, or the personal Telegram bot. Keep the AI TEAM service private until authentication, authorization, network exposure, and monitoring are independently verified.

## Gate policy

A gate is **PASS** only when its evidence is recorded with timestamp, responsible operator, target identity, and artifact/link. A plan, example value, mocked test, or successful ephemeral CI service is not production evidence. Missing, stale, ambiguous, or unverifiable evidence means **BLOCKED**. No deployment, PR merge, or destructive operation is authorized by this document.

## Required gates

| Gate | Required evidence | Current status |
|---|---|---|
| G0 — Code baseline | Reviewed PR; required CI green for the exact candidate SHA; branch protection confirmed | CI green on feature branch; PR #1 remains Draft and unmerged |
| G1 — Dedicated PostgreSQL | Separately provisioned AI TEAM production DB; owner/region/retention documented; least-privilege app user; TLS enabled; migration and connectivity checks recorded | BLOCKED — only ephemeral CI PostgreSQL exists |
| G2 — Dedicated Redis (optional for first release) | If enabled, separate private Redis endpoint, TLS/auth, eviction/retention policy, connectivity check, consumer deduplication on stable `outbox_id`; otherwise explicitly keep publisher disabled | BLOCKED — only ephemeral CI Redis exists |
| G3 — Secrets and identity | Secret-manager entries for strong bearer token(s), fixed actor/role/project scopes, read-only GitHub token, and any provider credentials; no secrets in Git, logs, images, or issue comments; rotation/revocation procedure tested | BLOCKED — no production secrets or real provider token configured |
| G4 — Provider allowlists | Reviewed exact GitHub repository/workflow-path allowlist; current-branch SHA verification; explicit Render health target only when authorized; HTTPS and expected service/status markers verified | BLOCKED — no production allowlists/targets configured |
| G5 — Network and TLS | Private ingress or authenticated HTTPS endpoint; valid certificate; redirect/proxy behavior reviewed; no public unauthenticated OpenAPI/API exposure; allowed outbound destinations documented | BLOCKED |
| G6 — Runtime and migrations | Production-like staging deployment using the exact candidate SHA; startup fails closed without required production config; migrations are repeatable/safe; readiness and liveness checks distinguished | BLOCKED — no service deployed |
| G7 — Backup and recovery | Production DB logical backup encrypted and access-controlled; retention and restore-point objective documented; restore into a separate isolated DB; row counts and audit-chain verification pass; evidence saved without publishing secrets/data | BLOCKED — CI-only dump/restore test is not a production recovery drill |
| G8 — Observability and incident response | Structured logs without credentials; health/error/latency alerts; audit-log integrity monitoring; owner/on-call contact; incident, token-revocation, rollback and recovery runbooks exercised | BLOCKED |
| G9 — Security acceptance | Independent review of auth/scopes, rate limiting, identity, CORS/proxy settings, dependency and image scanning, log redaction, and denial paths; unresolved critical/high findings closed or explicitly accepted by an authorized human | BLOCKED — external human identity provider and rate limiting are not implemented |
| G10 — Release authorization | G0–G9 evidence reviewed; exact SHA frozen; rollback path demonstrated; explicit human approval; deploy result and post-deploy smoke checks recorded | BLOCKED — no release authorization |

## Safe execution order

1. **Close G0 first:** review PR #1 and its full diff. Do not merge while it is Draft or while any required check is pending/failed.
2. **Provision isolated infrastructure:** create a dedicated AI TEAM PostgreSQL database and private runtime. Redis is optional until a concrete event consumer is enabled. Never point `DATABASE_URL` at an existing product database.
3. **Configure secrets out of band:** generate tokens locally or in the host secret manager; configure fixed roles and exact project scopes. Never paste secret values into chat, GitHub issues, source files, or CI logs.
4. **Configure provider verification minimally:** grant the GitHub verifier read-only access only. Add exact workflow paths only after reviewing their checks. Keep Render health targets empty until a target is explicitly authorized and validated.
5. **Deploy to private staging only after G0–G5 are satisfied.** Verify startup, migrations, auth failures, project-scope denials, health endpoints, logs, and rollback.
6. **Run a real recovery drill:** back up staging/production-like data, restore into a different isolated database, and verify record counts plus the audit chain. Never experiment on the source database.
7. **Complete security/observability review (G8–G9).** Public exposure is prohibited until the identity, rate-limiting, TLS, and monitoring gaps are resolved.
8. **Release only with explicit human authorization (G10).** This runbook itself grants no permission to deploy, merge, rotate secrets, or perform destructive operations.

## Evidence record template

For each gate, record:

- Gate ID and PASS/BLOCKED result
- Candidate commit SHA and environment/service identity
- UTC timestamp and operator
- Command/check performed and result
- Sanitized log or artifact URL (never credentials or personal/student data)
- Reviewer and any remaining risk/expiry date

## Current verified facts

- Latest recorded feature-branch commit before this runbook: `d868a4e724d5376fd726b6d473ef27d959cfc9ad`.
- Python 3.11 and 3.12 tests, Bandit, pip-audit, PostgreSQL 16 transaction/backup-restore integration, and Redis 7 + PostgreSQL outbox integration passed in CI for that commit.
- Those services were ephemeral CI services. The GitHub provider verifiers were tested with mocked API responses; there is no production GitHub token or live provider verification configured.
- PR #1 is Draft and unmerged; `main` was not changed by the work recorded above.
- No production runtime, database, Redis service, domain, secret, or deploy target has been provisioned by this phase.

Update this section only with newly checked evidence. Do not convert a planned task into a PASS.
