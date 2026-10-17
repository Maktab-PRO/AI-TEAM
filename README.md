# AI TEAM Control Plane

**Milestone 0.1 — executable governance core, not yet production-deployed.** This is a separate management-plane project. It does not modify or deploy Axsikent IT, Technikum Digital Platform, or the personal Telegram bot.

## Why it exists

The control plane is designed to make project boundaries and completion rules enforceable in code, rather than relying on prompts alone. The first milestone provides six connected components:

1. **Project Registry** — maps immutable project IDs to exact GitHub repository names; unknown/quarantined projects are denied.
2. **Boundary Guard** — checks project↔repository match, active project status, safe relative paths, and feature-branch-only writes. Production deployments, merges, secret rotation, and database restoration are deny-by-default.
3. **Task Orchestrator** — persists task states, enforces legal state transitions, blocks cross-project dependencies, checks prerequisites, and grants time-limited worker leases.
4. **Evidence Gate** — only accepts completion when each task-type policy has passing, trusted evidence from an approved verifier source. Public API submissions are deliberately untrusted; merely writing `status=passed` is not proof.
5. **Durable State** — SQLite transactions, foreign keys, WAL, full synchronous writes, idempotency keys, an outbox, dependency relationships, and worker leases.
6. **Audit Log** — append-only events chained with SHA-256 hashes; `/audit/verify` detects edits to historical events.

## Current safety boundaries

- The GitHub account's known repositories are only registered here as candidates when their identity is known. A fourth discovered repository is **quarantined** until its owner and project purpose are confirmed.
- `ai-team-control-plane` has `setup_required` and no repository mapped. It cannot schedule write tasks until a **dedicated AI TEAM repository** is created and the registry is updated in a reviewed change.
- The first three registered repositories are exact mappings only. The Guard does not guess that a task can be moved into another repository.
- `main`, `master`, and `production` are protected from direct writes. The initial allowed write branch prefixes are `ai-team/`, `codex/`, `feature/`, and `fix/`.
- `.env`/secret/key paths, `.git`, and `.github/workflows` are blocked by the initial policy. CI policy changes need a separate reviewed bootstrap step.
- No production deploy, PR merge, database restore, secret rotation, repository deletion, or billing action can be triggered by this MVP.
- This API does not claim to independently verify GitHub Actions or Render yet. The trusted-evidence interface is implemented; live provider verifier adapters are a later milestone. Until then Evidence Gate intentionally fails closed.
- Redis is an **optional event-delivery layer**. SQLite remains authoritative in the local MVP; Redis is not the source of truth. `RedisOutboxPublisher` uses at-least-once delivery and includes `outbox_id` for consumer deduplication.
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

The API binds to `127.0.0.1:8000` by default. OpenAPI UI is available locally at `/docs`. Mutating routes require `X-Actor` and an `Idempotency-Key` for task creation. In production, set `APP_ENV=production` and a high-entropy `AI_TEAM_API_TOKEN` at least 32 characters long; the API refuses to run its routes without it. Keep the API private until authentication, rate limits, TLS, and a deployment-ready database are configured.

## Configuration

- `AI_TEAM_REGISTRY_PATH`: registry JSON path; defaults to `config/projects.json`.
- `AI_TEAM_DB_PATH`: durable SQLite file path; defaults to `data/ai-team.sqlite3`.
- `AI_TEAM_API_TOKEN`: required for authenticated API calls when set; mandatory in production.
- `APP_ENV`: `development` by default; set to `production` for the stricter token requirement.
- `REDIS_URL`: reserved for the deployment/worker integration step. Do not add credentials to Git.

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
| `POST /tasks/{id}/request-verification` | Move work to evidence review |
| `POST /tasks/{id}/verify` | Verify using the Evidence Gate only |
| `GET /audit/verify` | Recompute SHA-256 audit chain |
| `GET /outbox` | Inspect un-published events |

## Development rule

**One stage → deep inspection → narrow fix → focused tests → full CI → next stage.** Do not bind AI TEAM to any existing product repository. First create a dedicated `Maktab-PRO/AI-TEAM` (or another explicitly chosen name) repository, then register it under the `ai-team-control-plane` ID. Keep each project boundary exact and testable.
