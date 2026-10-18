# AI TEAM Control Plane

**Milestone 0.1 — executable governance core, not yet production-deployed.** This is a separate management-plane project. It does not modify or deploy Axsikent IT, Technikum Digital Platform, or the personal Telegram bot.

## Why it exists

The control plane is designed to make project boundaries and completion rules enforceable in code, rather than relying on prompts alone. The first milestone provides six connected components:

1. **Project Registry** — maps immutable project IDs to exact GitHub repository names; unknown/quarantined projects are denied.
2. **Boundary Guard** — checks project↔repository match, active project status, safe relative paths, and feature-branch-only writes. Production deployments, merges, secret rotation, and database restoration are deny-by-default.
3. **Task Orchestrator** — persists task states, enforces legal transitions, blocks cross-project dependencies, re-checks current project boundaries on writes, checks prerequisites, and reclaims expired leases without losing in-progress state.
4. **Evidence Gate** — only accepts completion when each task-type policy has passing, trusted evidence from an approved verifier source. Public API submissions are deliberately untrusted; merely writing `status=passed` is not proof.
5. **Durable State** — local SQLite transactions/WAL or a PostgreSQL backend with row locks, serialized audit-chain writes, idempotency-key locks, an outbox, dependency relationships, and worker leases. PostgreSQL becomes active when `DATABASE_URL` is set.
6. **Audit Log** — append-only events chained with versioned SHA-256 hashes. Version 2 binds the task_id column and event content; legacy version-1 events remain verifiable but do not retroactively bind that column.

## Current safety boundaries

- The registry has exact mappings for Axsikent IT, Technikum Digital Platform, the personal Telegram bot, and this dedicated AI TEAM repository. The discovered `Maktab-PRO/aqlli-mahalla-temirqishloq` repository remains **quarantined** until ownership and project boundaries are confirmed.
- Every task is tied to one exact repository. Repository uniqueness is case-insensitive to match GitHub naming behavior; the Guard never guesses that a task can move to another project.
- `main`, `master`, and `production` are protected from direct writes. For Axsikent, Technikum, and the Telegram bot, allowed write branch prefixes are `ai-team/`, `codex/`, `feature/`, and `fix/`; the AI TEAM repository is restricted to `ai-team/` branches.
- `.env`/secret/key paths, `.git`, and `.github/workflows` are blocked by the initial policy. CI policy changes need a separate reviewed bootstrap step.
- No production deploy, PR merge, database restore, secret rotation, repository deletion, or billing action can be triggered by this MVP.
- GitHub Actions evidence can be trusted only after the server verifies the actual Actions REST API response: exact repository, task branch, current branch head SHA, completed-success conclusion, an explicitly allowlisted workflow path, and successful required test/security jobs. A run URL alone never creates trusted evidence. The GitHub PR review adapter also validates PR repository/base/head, open non-draft state, latest normalized reviewer states, author exclusion, and approval of the exact current head SHA. It re-reads the PR and branch immediately before writing trusted evidence and rejects state/head changes. Render health evidence is available only for an explicitly configured project health target; it performs a bounded HTTPS GET to the health endpoint, rejects redirects, and requires the configured service/status markers. It never deploys or restarts a service.
- Redis is an **optional event-delivery layer**. The relational database remains authoritative; Redis is not the source of truth. `RedisOutboxPublisher` uses at-least-once delivery and includes `outbox_id` for consumer deduplication. CI runs the publisher against an isolated Redis 7 service and tests the duplicate window between `XADD` and database acknowledgement.
- Notion is intended for human-readable design decisions, runbooks, and project documentation, not as a second conflicting task ledger. A live Notion API sync is not enabled in this milestone.

## Local setup

Requires Python 3.11+.

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[test,redis]"

python -m pytest
python -m ai_team_control_plane.cli check-registry
python -m ai_team_control_plane.cli verify-audit
python -m ai_team_control_plane.cli serve
# Optional: manually publish a bounded batch to Redis when REDIS_URL is configured
python -m ai_team_control_plane.cli publish-outbox --limit 50
```

The API binds to `127.0.0.1:8000` by default. Local development uses SQLite unless `DATABASE_URL` points to a dedicated AI TEAM PostgreSQL database. PostgreSQL operations use row locks and per-key advisory locks for claims, idempotency, schema initialization, and audit-chain serialization. OpenAPI UI is available locally at `/docs`. Development requests use `X-Actor`; task creation also requires an `Idempotency-Key`. In production, set `APP_ENV=production` and either a high-entropy `AI_TEAM_API_TOKEN` (32+ characters) with fixed `AI_TEAM_API_ACTOR`, or `AI_TEAM_API_TOKENS` as a JSON mapping of 32+ character tokens to fixed actor IDs. Production actor identity is derived from the matched token; a caller cannot override it with `X-Actor`. A shared-token identity is a service identity, not proof of a human identity. Production requests now require an explicit role and project scope. Roles are `platform_admin` (global; must have scope `*`), `project_admin`, `worker`, `reviewer`, and `read_only`. Workers may create/claim/transition tasks and request verification but cannot verify tasks; reviewers may record untrusted evidence and invoke the Evidence Gate but cannot claim or create tasks. Each non-admin token is limited to exact registered project IDs. `X-Actor` cannot override the identity bound to the bearer token. This is an API-token authorization layer, not an external human identity provider; rate limits and production host/TLS/DB operations still need to be configured.

## Configuration

- `AI_TEAM_REGISTRY_PATH`: registry JSON path; defaults to `config/projects.json`.
- `DATABASE_URL`: optional PostgreSQL connection URL. When set, PostgreSQL is used instead of SQLite; point it only to a dedicated AI TEAM database, never the Axsikent or Technikum databases.
- `AI_TEAM_DB_PATH`: durable SQLite file path used only when `DATABASE_URL` is empty; defaults to `data/ai-team.sqlite3`.
- `AI_TEAM_API_TOKEN`: optional in development; in production it must be a high-entropy bearer token (32+ characters) paired with actor, role, and explicit project scope.
- `AI_TEAM_API_ACTOR`: fixed audit identity for single-token production mode; default `api-token`.
- `AI_TEAM_API_ROLE`: one of `platform_admin`, `project_admin`, `worker`, `reviewer`, or `read_only`.
- `AI_TEAM_API_PROJECTS`: comma-separated exact project IDs; only `platform_admin` may set this to `*`, and that means global access.
- `AI_TEAM_API_TOKENS`: optional JSON object mapping 32+ character bearer tokens to records such as `{"actor":"worker-a","role":"worker","projects":["axsikent-it"]}`. Identity-only/legacy mappings fail closed. Store real values only in a secret manager/environment.
- `APP_ENV`: `development` by default; production requires token-to-role/project binding.
- `AI_TEAM_GITHUB_TOKEN`: fine-grained, read-only GitHub API token used by Actions and PR-review verifiers. Grant Contents: read (to compare the current branch SHA), Metadata: read, Actions: read, and Pull requests: read; no write permissions are required. Keep it only in the host secret manager.
- `AI_TEAM_GITHUB_WORKFLOW_ALLOWLIST`: JSON mapping of exact `owner/repository` names to approved `.github/workflows/...` paths. The verifier fails closed if the repository has no approved path or a run comes from another workflow.
- `AI_TEAM_RENDER_HEALTH_ALLOWLIST`: JSON mapping of registered project IDs to exact health targets, for example `{"axsikent-it":{"url":"https://your-service.onrender.com/health","expected_service":"your-service","expected_status":"ok"}}`. Configure only public HTTPS health endpoints whose returned service and status fields you control. No real endpoint is preconfigured.
- `REDIS_URL`: optional Redis connection URL used only when explicitly running `ai-team publish-outbox`; do not expose it to API callers or commit credentials. CI uses an ephemeral Redis service, not a production Redis instance.

## Endpoint map

| Endpoint | Purpose |
|---|---|
| `GET /health` | Service and audit-chain status (public, minimal output) |
| `GET /projects` | Registry snapshot |
| `POST /tasks` | Create idempotent, scoped task |
| `GET /tasks`, `GET /tasks/{id}` | Read task state |
| `POST /tasks/{id}/claim` | Atomically acquire a lease and check dependencies |
| `POST /tasks/{id}/transition` | Apply a legal status transition |
| `POST /tasks/{id}/evidence` | Record an untrusted/manual evidence reference |
| `POST /tasks/{id}/evidence/github-actions` | Validate an Actions run via GitHub API and record only policy-approved trusted CI/test evidence |
| `POST /tasks/{id}/evidence/github-review` | Validate an open non-draft PR, exact repo/base/head, non-author approval on the current SHA, and final PR/branch freshness via GitHub API |
| `POST /tasks/{id}/evidence/render-health` | Check a per-project allowlisted HTTPS health endpoint and record trusted health evidence only on an exact status/service match |
| `POST /tasks/{id}/request-verification` | Move work to evidence review |
| `POST /tasks/{id}/verify` | Verify using the Evidence Gate only |
| `GET /audit/verify` | Recompute SHA-256 audit chain |
| `GET /outbox` | Inspect un-published events |

## Development rule

**One stage → deep inspection → narrow fix → focused tests → full CI → next stage.** Do not bind AI TEAM to any existing product repository. The dedicated `Maktab-PRO/AI-TEAM` repository is registered under `ai-team-control-plane`. For production, configure a separate PostgreSQL database using `DATABASE_URL`, run the PostgreSQL integration and backup/restore CI checks, and keep the API private until an external identity provider, rate limits, TLS, monitoring, real-host backup/restore drills, and operational recovery are addressed. Provider evidence adapters must have explicit allowlists and real read-only credentials set by the operator. Keep each project boundary exact and testable.


## Production readiness

The release is **blocked until every required gate is evidenced**. Follow [`docs/PRODUCTION-READINESS.md`](docs/PRODUCTION-READINESS.md) for the fail-closed G0–G10 sequence. CI success on ephemeral services is not production deployment or a production recovery drill.


## Production configuration preflight

Once a private staging environment's variables are configured, run:

```bash
python -m ai_team_control_plane.cli preflight
```

The command validates configuration shape only: PostgreSQL URL/TLS mode, token-to-actor/role/project scopes, GitHub token presence and exact workflow allowlist syntax, a configured request limit and HMAC key, optional Redis TLS URL, and optional Render health targets. The API enforces a shared database-backed fixed-window limit keyed by HMAC fingerprints of the direct client peer and bearer token; raw peer addresses and bearer tokens are not stored in the rate-limit table. It does not print secret values, contact external services, prove provider credentials work, or approve/deploy a release. It exits with code `2` if configuration checks fail. Even if it exits successfully, the G0–G10 release gates in `docs/PRODUCTION-READINESS.md` still apply. Validate client-IP/proxy behavior in private staging before relying on this limit in a hosted environment.


## Human identity and ingress security

Human login is intentionally not enabled yet. The provider-neutral OIDC, role-mapping, session, and trusted-proxy requirements are documented in [docs/IDENTITY-AND-EDGE-SECURITY.md](docs/IDENTITY-AND-EDGE-SECURITY.md) and tracked in [Issue #3](https://github.com/Maktab-PRO/AI-TEAM/issues/3). Do not expose the service publicly until a provider/domain is registered and the remaining identity and ingress gates are verified.
