from __future__ import annotations

import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .boundary import BoundaryGuard
from .models import BoundaryViolation, ControlPlaneError, EvidenceRejected, IdempotencyConflict, StateConflict
from .orchestrator import TaskOrchestrator
from .registry import ProjectRegistry
from .database import create_store
from .integrations.github_evidence import GitHubActionsEvidenceVerifier
from .integrations.github_review import GitHubPullRequestReviewVerifier
from .integrations.render_health import RenderHealthEvidenceVerifier
from .rate_limiting import RateLimitConfigurationError, get_rate_limit_config, rate_limit_bucket_key


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = Path(os.getenv("AI_TEAM_REGISTRY_PATH", ROOT / "config" / "projects.json"))
registry = ProjectRegistry(REGISTRY_PATH)
store = create_store()
guard = BoundaryGuard(registry)
orchestrator = TaskOrchestrator(store, guard)
app = FastAPI(
    title="AI TEAM Control Plane",
    version="0.1.0",
    description="Fail-closed project boundaries, durable task orchestration, evidence gates, and tamper-evident audit events.",
)

logger = logging.getLogger(__name__)


@app.middleware("http")
async def enforce_database_rate_limit(request: Request, call_next):
    try:
        limit, hash_key = get_rate_limit_config()
    except RateLimitConfigurationError:
        return JSONResponse(
            status_code=503,
            content={"detail": "Production rate limiting is not configured."},
        )

    now = int(time.time())
    peer = request.client.host if request.client and request.client.host else "unknown"
    bucket_keys = [rate_limit_bucket_key(hash_key, "client", peer)]
    authorization = request.headers.get("authorization", "")
    auth_parts = authorization.split(None, 1)
    if (
        len(auth_parts) == 2
        and auth_parts[0].casefold() == "bearer"
        and auth_parts[1]
        and len(auth_parts[1]) <= 4096
    ):
        bucket_keys.append(rate_limit_bucket_key(hash_key, "token", auth_parts[1]))

    try:
        outcomes = [
            store.consume_rate_limit(key, limit=limit, window_seconds=60, now=now)
            for key in bucket_keys
        ]
    except Exception as exc:
        # Never log database URLs, raw tokens, client identifiers, or driver error text.
        logger.warning("Database-backed rate limiting failed (error_type=%s)", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={"detail": "Rate limiting is temporarily unavailable."},
        )

    denied = [outcome for outcome in outcomes if not outcome["allowed"]]
    if denied:
        retry_after = max(1, max(int(outcome["reset_at"]) for outcome in denied) - now)
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded."},
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
            },
        )

    response = await call_next(request)
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(
        max(0, limit - max(int(outcome["count"]) for outcome in outcomes))
    )
    response.headers["X-RateLimit-Reset"] = str(
        max(int(outcome["reset_at"]) for outcome in outcomes)
    )
    return response


class CreateTask(BaseModel):
    project_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=10_000)
    task_type: str = Field(pattern=r"^(implementation|bugfix|deployment|security|documentation)$")
    repository: str = Field(min_length=3, max_length=250)
    branch: str = Field(min_length=1, max_length=250)
    paths: list[str] = Field(default_factory=list, max_length=200)
    priority: int = Field(default=3, ge=1, le=5)
    assigned_role: str = Field(default="engineer", min_length=1, max_length=100)
    dependencies: list[str] = Field(default_factory=list, max_length=100)


class TransitionTask(BaseModel):
    status: str = Field(min_length=1, max_length=50)
    expected_version: int = Field(ge=1)
    reason: str = Field(default="", max_length=1000)


class ClaimTask(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)
    lease_seconds: int = Field(default=300, ge=15, le=3600)


class EvidenceInput(BaseModel):
    kind: str = Field(min_length=1, max_length=100)
    status: str = Field(pattern=r"^(passed|failed|informational)$")
    uri: str = Field(min_length=8, max_length=2000)
    source: str = Field(min_length=1, max_length=100)
    notes: str = Field(default="", max_length=2000)
    artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


class GitHubActionsEvidenceInput(BaseModel):
    run_url: str = Field(min_length=20, max_length=2000)


class GitHubPullRequestEvidenceInput(BaseModel):
    pull_url: str = Field(min_length=20, max_length=2000)


_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "platform_admin": frozenset({
        "projects.read", "tasks.read", "tasks.create", "tasks.claim",
        "tasks.transition", "tasks.evidence", "tasks.request_verification",
        "tasks.verify", "audit.read", "outbox.read",
    }),
    "project_admin": frozenset({
        "projects.read", "tasks.read", "tasks.create", "tasks.claim",
        "tasks.transition", "tasks.evidence", "tasks.request_verification",
        "tasks.verify",
    }),
    "worker": frozenset({
        "projects.read", "tasks.read", "tasks.create", "tasks.claim",
        "tasks.transition", "tasks.evidence", "tasks.request_verification",
    }),
    "reviewer": frozenset({
        "projects.read", "tasks.read", "tasks.evidence", "tasks.verify",
    }),
    "read_only": frozenset({"projects.read", "tasks.read"}),
}
_GLOBAL_PERMISSIONS = frozenset({"audit.read", "outbox.read"})


def _validated_principal(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HTTPException(
            status_code=503,
            detail="Production token policies must define actor, role, and projects.",
        )
    actor = value.get("actor")
    role = value.get("role")
    projects = value.get("projects")
    if (
        not isinstance(actor, str)
        or actor != actor.strip()
        or not actor
        or len(actor) > 120
        or any(ord(ch) < 32 for ch in actor)
    ):
        raise HTTPException(status_code=503, detail="Token actor must be a valid 1–120 character identity.")
    if not isinstance(role, str) or role not in _ROLE_PERMISSIONS:
        raise HTTPException(status_code=503, detail=f"Token role must be one of {sorted(_ROLE_PERMISSIONS)}.")
    if (
        not isinstance(projects, list)
        or not projects
        or any(not isinstance(project, str) or not project or project != project.strip() for project in projects)
        or len(projects) != len(set(projects))
    ):
        raise HTTPException(status_code=503, detail="Token projects must be a non-empty list of unique project IDs.")
    if role == "platform_admin":
        if projects != ["*"]:
            raise HTTPException(status_code=503, detail="platform_admin must use the explicit global projects scope '*' or use a narrower role.")
    elif "*" in projects:
        raise HTTPException(status_code=503, detail="Only platform_admin may use the global projects scope.")
    for project_id in projects:
        if project_id == "*":
            continue
        project = registry.get(project_id)
        if project is None:
            raise HTTPException(status_code=503, detail=f"Token policy references unknown project ID: {project_id}.")
        if project.status != "active":
            raise HTTPException(status_code=503, detail=f"Token policy references inactive project ID: {project_id}.")
    return {"actor": actor, "role": role, "projects": projects}


def _set_request_principal(request: Request | None, principal: dict[str, Any]) -> None:
    if request is not None:
        request.state.principal = principal


def authenticated_actor(
    request: Request,
    authorization: str | None = Header(default=None),
    x_actor: str | None = Header(default=None, alias="X-Actor"),
) -> str:
    environment = os.getenv("APP_ENV", "development").strip().lower()
    supplied = authorization.removeprefix("Bearer ") if authorization else ""

    if environment == "production":
        credentials: dict[str, dict[str, Any]] = {}
        token_map_raw = os.getenv("AI_TEAM_API_TOKENS", "").strip()
        if token_map_raw:
            try:
                token_map = json.loads(token_map_raw)
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="AI_TEAM_API_TOKENS must be a JSON object mapping tokens to actor/role/project policies.",
                ) from exc
            if not isinstance(token_map, dict) or not token_map:
                raise HTTPException(status_code=503, detail="AI_TEAM_API_TOKENS must be a non-empty JSON object.")
            for candidate, policy in token_map.items():
                if (
                    not isinstance(candidate, str)
                    or candidate != candidate.strip()
                    or len(candidate) < 32
                ):
                    raise HTTPException(status_code=503, detail="Each production bearer token must be an exact 32+ character string.")
                credentials[candidate] = _validated_principal(policy)

        single_token = os.getenv("AI_TEAM_API_TOKEN", "").strip()
        if single_token:
            if len(single_token) < 32:
                raise HTTPException(status_code=503, detail="AI_TEAM_API_TOKEN must be at least 32 characters in production.")
            project_text = os.getenv("AI_TEAM_API_PROJECTS", "").strip()
            project_scopes = [part.strip() for part in project_text.split(",") if part.strip()]
            single_principal = _validated_principal({
                "actor": os.getenv("AI_TEAM_API_ACTOR", "api-token").strip(),
                "role": os.getenv("AI_TEAM_API_ROLE", "").strip(),
                "projects": project_scopes,
            })
            existing = credentials.get(single_token)
            if existing is not None and existing != single_principal:
                raise HTTPException(status_code=503, detail="The same bearer token cannot map to multiple actor/role/project policies.")
            credentials[single_token] = single_principal

        if not credentials:
            raise HTTPException(status_code=503, detail="Production API needs a strong token and explicit role/project scope.")
        matched_principal = None
        for candidate, principal in credentials.items():
            if hmac.compare_digest(supplied, candidate):
                matched_principal = principal
        if matched_principal is None:
            raise HTTPException(status_code=401, detail="Valid Bearer token required.")
        supplied_actor = (x_actor or "").strip()
        if supplied_actor and supplied_actor != matched_principal["actor"]:
            raise HTTPException(status_code=403, detail="X-Actor cannot override the identity mapped to the production bearer token.")
        _set_request_principal(request, matched_principal)
        return matched_principal["actor"]

    token = os.getenv("AI_TEAM_API_TOKEN", "").strip()
    if token and not hmac.compare_digest(supplied, token):
        raise HTTPException(status_code=401, detail="Valid Bearer token required.")
    actor = (x_actor or "").strip()
    if not actor:
        raise HTTPException(status_code=400, detail="X-Actor header is required for traceability in development.")
    # Development mode is intentionally convenient for local tests only. Production
    # ignores this fallback and needs an explicit role/project-bound token policy.
    _set_request_principal(request, {"actor": actor, "role": "platform_admin", "projects": ["*"]})
    return actor


def require_permission(permission: str):
    def dependency(
        request: Request,
        actor: str = Depends(authenticated_actor),
    ) -> str:
        principal = getattr(request.state, "principal", None)
        if not isinstance(principal, dict):
            raise HTTPException(status_code=401, detail="Authenticated principal is missing.")
        role = principal.get("role")
        if permission not in _ROLE_PERMISSIONS.get(role, frozenset()):
            raise HTTPException(status_code=403, detail=f"Role '{role}' is not permitted to perform '{permission}'.")
        if permission in _GLOBAL_PERMISSIONS and principal.get("projects") != ["*"]:
            raise HTTPException(status_code=403, detail=f"'{permission}' requires an explicit global platform_admin scope.")
        return actor

    dependency.__name__ = "require_" + permission.replace(".", "_")
    return dependency


def _principal(request: Request) -> dict[str, Any]:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, dict):
        raise HTTPException(status_code=401, detail="Authenticated principal is missing.")
    return principal


def _project_is_in_scope(principal: dict[str, Any], project_id: str) -> bool:
    scopes = principal.get("projects", [])
    return "*" in scopes or project_id in scopes


def _require_task_scope(request: Request, task_id: str) -> dict[str, Any]:
    task = store.get_task(task_id)
    if task is None or not _project_is_in_scope(_principal(request), task["project_id"]):
        # Conceal cross-project task existence from an otherwise authenticated caller.
        raise HTTPException(status_code=404, detail="Task not found.")
    return task


def _github_workflow_allowlist() -> dict[str, list[str]]:
    raw = os.getenv("AI_TEAM_GITHUB_WORKFLOW_ALLOWLIST", "").strip()
    if not raw:
        raise HTTPException(
            status_code=503,
            detail="GitHub Actions verifier needs an explicit AI_TEAM_GITHUB_WORKFLOW_ALLOWLIST.",
        )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=503,
            detail="AI_TEAM_GITHUB_WORKFLOW_ALLOWLIST must be a JSON object from owner/repository to approved workflow paths.",
        ) from exc
    if not isinstance(value, dict) or not value:
        raise HTTPException(status_code=503, detail="AI_TEAM_GITHUB_WORKFLOW_ALLOWLIST must be a non-empty JSON object.")

    result: dict[str, list[str]] = {}
    seen_repositories: set[str] = set()
    for repository, paths in value.items():
        parts = repository.split("/") if isinstance(repository, str) else []
        repository_key = repository.casefold() if isinstance(repository, str) else ""
        if (
            len(parts) != 2
            or any(not part or part != part.strip() for part in parts)
            or not isinstance(paths, list)
            or not paths
            or repository_key in seen_repositories
        ):
            raise HTTPException(
                status_code=503,
                detail="Each workflow allowlist entry must use one exact owner/repository key and a non-empty list of workflow paths.",
            )
        clean_paths: list[str] = []
        for path in paths:
            if (
                not isinstance(path, str)
                or path != path.strip()
                or not path.startswith(".github/workflows/")
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or path in clean_paths
            ):
                raise HTTPException(
                    status_code=503,
                    detail="Approved workflow paths must be unique safe paths under .github/workflows/.",
                )
            clean_paths.append(path)
        seen_repositories.add(repository_key)
        result[repository] = clean_paths
    return result


def safe_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, BoundaryViolation):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, IdempotencyConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (StateConflict, EvidenceRejected)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (ValueError, ControlPlaneError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="Unexpected control-plane error.")


def _render_health_allowlist() -> dict[str, dict[str, str]]:
    raw = os.getenv("AI_TEAM_RENDER_HEALTH_ALLOWLIST", "").strip()
    if not raw:
        raise HTTPException(
            status_code=503,
            detail="Render health verifier needs an explicit AI_TEAM_RENDER_HEALTH_ALLOWLIST.",
        )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=503,
            detail="AI_TEAM_RENDER_HEALTH_ALLOWLIST must be a JSON object mapping project IDs to health target settings.",
        ) from exc
    if not isinstance(value, dict) or not value:
        raise HTTPException(status_code=503, detail="AI_TEAM_RENDER_HEALTH_ALLOWLIST must be a non-empty JSON object.")

    result: dict[str, dict[str, str]] = {}
    for project_id, target in value.items():
        project = registry.get(project_id) if isinstance(project_id, str) else None
        if project is None or project.status != "active":
            raise HTTPException(status_code=503, detail=f"Render health target references unknown or inactive project ID: {project_id!r}.")
        if not isinstance(target, dict):
            raise HTTPException(status_code=503, detail=f"Render health target for {project_id!r} must be an object.")
        url = target.get("url")
        expected_service = target.get("expected_service")
        expected_status = target.get("expected_status", "ok")
        if (
            not isinstance(url, str)
            or not isinstance(expected_service, str)
            or not isinstance(expected_status, str)
            or not url
            or not expected_service
            or not expected_status
        ):
            raise HTTPException(
                status_code=503,
                detail=f"Render health target for {project_id!r} needs url, expected_service, and expected_status.",
            )
        result[project_id] = {
            "url": url,
            "expected_service": expected_service,
            "expected_status": expected_status,
        }
    return result

@app.get("/health")
def health() -> dict[str, Any]:
    chain = store.verify_audit_chain()
    return {
        "status": "ok" if chain["valid"] else "degraded",
        "service": "ai-team-control-plane",
        "registry_schema": registry.SCHEMA_VERSION,
        "database_backend": getattr(store, "backend", "unknown"),
        "audit_chain_valid": chain["valid"],
        "audit_events_checked": chain["checked_events"],
    }


@app.get("/projects")
def get_projects(
    request: Request,
    _: str = Depends(require_permission("projects.read")),
) -> dict:
    snapshot = registry.snapshot()
    principal = _principal(request)
    if "*" in principal["projects"]:
        return snapshot
    allowed = set(principal["projects"])
    return {
        "schema_version": snapshot["schema_version"],
        "projects": [item for item in snapshot["projects"] if item["id"] in allowed],
    }


@app.post("/tasks", status_code=201)
def create_task(
    request: Request,
    body: CreateTask,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    actor: str = Depends(require_permission("tasks.create")),
) -> dict:
    principal = _principal(request)
    if not _project_is_in_scope(principal, body.project_id):
        raise HTTPException(status_code=403, detail="Token is not scoped to the requested project.")
    try:
        return orchestrator.create_task(**body.model_dump(), idempotency_key=idempotency_key, actor=actor)
    except Exception as exc:
        raise safe_error(exc) from exc


@app.get("/tasks")
def list_tasks(
    request: Request,
    project_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    _: str = Depends(require_permission("tasks.read")),
) -> dict:
    principal = _principal(request)
    if project_id is not None:
        if not _project_is_in_scope(principal, project_id):
            raise HTTPException(status_code=404, detail="Project not found.")
        return {"items": store.list_tasks(project_id=project_id, status=status, limit=limit)}

    if "*" in principal["projects"]:
        return {"items": store.list_tasks(status=status, limit=limit)}

    values: list[dict[str, Any]] = []
    for allowed_project in principal["projects"]:
        values.extend(store.list_tasks(project_id=allowed_project, status=status, limit=limit))
    values.sort(key=lambda item: (item.get("created_at", ""), item.get("id", "")), reverse=True)
    return {"items": values[:limit]}


@app.get("/tasks/{task_id}")
def get_task(
    request: Request,
    task_id: str,
    _: str = Depends(require_permission("tasks.read")),
) -> dict:
    return _require_task_scope(request, task_id)


@app.post("/tasks/{task_id}/claim")
def claim_task(
    request: Request,
    task_id: str,
    body: ClaimTask,
    actor: str = Depends(require_permission("tasks.claim")),
) -> dict:
    _require_task_scope(request, task_id)
    try:
        # In production, bind lease ownership to the authenticated actor, not a client-supplied worker_id.
        worker_id = actor if os.getenv("APP_ENV", "development").strip().lower() == "production" else body.worker_id
        return orchestrator.claim_task(task_id, worker_id=worker_id, lease_seconds=body.lease_seconds, actor=actor)
    except Exception as exc:
        raise safe_error(exc) from exc


@app.post("/tasks/{task_id}/transition")
def transition_task(
    request: Request,
    task_id: str,
    body: TransitionTask,
    actor: str = Depends(require_permission("tasks.transition")),
) -> dict:
    _require_task_scope(request, task_id)
    try:
        return orchestrator.transition(
            task_id,
            body.status,
            actor=actor,
            reason=body.reason,
            expected_version=body.expected_version,
        )
    except Exception as exc:
        raise safe_error(exc) from exc


@app.post("/tasks/{task_id}/evidence", status_code=201)
def record_evidence(
    request: Request,
    task_id: str,
    body: EvidenceInput,
    actor: str = Depends(require_permission("tasks.evidence")),
) -> dict:
    _require_task_scope(request, task_id)
    try:
        # API callers can record attestations, but cannot mark them trusted.
        return orchestrator.record_evidence(task_id, **body.model_dump(), actor=actor, trusted=False, verified_by=None)
    except Exception as exc:
        raise safe_error(exc) from exc


@app.post("/tasks/{task_id}/evidence/github-actions")
def verify_github_actions_evidence(
    request: Request,
    task_id: str,
    body: GitHubActionsEvidenceInput,
    actor: str = Depends(require_permission("tasks.evidence")),
) -> dict:
    _require_task_scope(request, task_id)
    token = os.getenv("AI_TEAM_GITHUB_TOKEN", "").strip()
    if not token:
        raise HTTPException(
            status_code=503,
            detail="GitHub Actions evidence verifier is not configured; set AI_TEAM_GITHUB_TOKEN in the secret environment.",
        )
    try:
        verifier = GitHubActionsEvidenceVerifier(
            orchestrator,
            token=token,
            approved_workflows=_github_workflow_allowlist(),
        )
        return verifier.verify_and_record(task_id, run_url=body.run_url, actor=actor)
    except Exception as exc:
        raise safe_error(exc) from exc


@app.post("/tasks/{task_id}/evidence/github-review")
def verify_github_pull_request_review(
    request: Request,
    task_id: str,
    body: GitHubPullRequestEvidenceInput,
    actor: str = Depends(require_permission("tasks.evidence")),
) -> dict:
    _require_task_scope(request, task_id)
    token = os.getenv("AI_TEAM_GITHUB_TOKEN", "").strip()
    if not token:
        raise HTTPException(
            status_code=503,
            detail="GitHub PR review verifier is not configured; set AI_TEAM_GITHUB_TOKEN in the secret environment.",
        )
    try:
        verifier = GitHubPullRequestReviewVerifier(orchestrator, registry, token=token)
        return verifier.verify_and_record(task_id, pull_url=body.pull_url, actor=actor)
    except Exception as exc:
        raise safe_error(exc) from exc


@app.post("/tasks/{task_id}/evidence/render-health")
def verify_render_health_evidence(
    request: Request,
    task_id: str,
    actor: str = Depends(require_permission("tasks.evidence")),
) -> dict:
    _require_task_scope(request, task_id)
    try:
        verifier = RenderHealthEvidenceVerifier(
            orchestrator,
            _render_health_allowlist(),
        )
        return verifier.verify_and_record(task_id, actor=actor)
    except Exception as exc:
        raise safe_error(exc) from exc


@app.post("/tasks/{task_id}/request-verification")
def request_verification(
    request: Request,
    task_id: str,
    actor: str = Depends(require_permission("tasks.request_verification")),
) -> dict:
    _require_task_scope(request, task_id)
    try:
        return orchestrator.request_verification(task_id, actor=actor)
    except Exception as exc:
        raise safe_error(exc) from exc


@app.post("/tasks/{task_id}/verify")
def verify_task(
    request: Request,
    task_id: str,
    actor: str = Depends(require_permission("tasks.verify")),
) -> dict:
    _require_task_scope(request, task_id)
    try:
        return orchestrator.verify_task(task_id, actor=actor)
    except Exception as exc:
        raise safe_error(exc) from exc


@app.get("/audit/verify")
def verify_audit(_: str = Depends(require_permission("audit.read"))) -> dict:
    return store.verify_audit_chain()


@app.get("/outbox")
def get_outbox(
    limit: int = Query(default=100, ge=1, le=1000),
    _: str = Depends(require_permission("outbox.read")),
) -> dict:
    return {"items": store.list_outbox(limit)}
