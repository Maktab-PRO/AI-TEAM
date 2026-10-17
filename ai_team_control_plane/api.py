from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .boundary import BoundaryGuard
from .models import BoundaryViolation, ControlPlaneError, EvidenceRejected, IdempotencyConflict, StateConflict
from .orchestrator import TaskOrchestrator
from .registry import ProjectRegistry
from .store import DurableStore


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = Path(os.getenv("AI_TEAM_REGISTRY_PATH", ROOT / "config" / "projects.json"))
DB_PATH = Path(os.getenv("AI_TEAM_DB_PATH", ROOT / "data" / "ai-team.sqlite3"))
registry = ProjectRegistry(REGISTRY_PATH)
store = DurableStore(DB_PATH)
guard = BoundaryGuard(registry)
orchestrator = TaskOrchestrator(store, guard)
app = FastAPI(
    title="AI TEAM Control Plane",
    version="0.1.0",
    description="Fail-closed project boundaries, durable task orchestration, evidence gates, and tamper-evident audit events.",
)


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


def authenticated_actor(
    request: Request,
    authorization: str | None = Header(default=None),
    x_actor: str | None = Header(default=None, alias="X-Actor"),
) -> str:
    environment = os.getenv("APP_ENV", "development").strip().lower()
    supplied = authorization.removeprefix("Bearer ") if authorization else ""

    if environment == "production":
        # Prefer distinct opaque tokens mapped to fixed actor IDs. X-Actor is not an authentication claim.
        credentials: dict[str, str] = {}
        token_map_raw = os.getenv("AI_TEAM_API_TOKENS", "").strip()
        if token_map_raw:
            try:
                token_map = json.loads(token_map_raw)
            except json.JSONDecodeError as exc:
                raise HTTPException(status_code=503, detail="AI_TEAM_API_TOKENS must be a JSON object mapping tokens to actor IDs.") from exc
            if not isinstance(token_map, dict) or not token_map:
                raise HTTPException(status_code=503, detail="AI_TEAM_API_TOKENS must be a non-empty JSON object.")
            for candidate, identity in token_map.items():
                if not isinstance(candidate, str) or candidate != candidate.strip() or len(candidate) < 32 or not isinstance(identity, str):
                    raise HTTPException(status_code=503, detail="Each production API token must be an exact 32+ character string and map to an actor ID.")
                identity = identity.strip()
                if not identity or len(identity) > 120 or any(ord(ch) < 32 for ch in identity):
                    raise HTTPException(status_code=503, detail="Each production API token must map to a valid actor ID (1–120 characters).")
                credentials[candidate] = identity

        single_token = os.getenv("AI_TEAM_API_TOKEN", "").strip()
        if single_token:
            if len(single_token) < 32:
                raise HTTPException(status_code=503, detail="AI_TEAM_API_TOKEN must be at least 32 characters in production.")
            single_actor = os.getenv("AI_TEAM_API_ACTOR", "api-token").strip()
            if not single_actor or len(single_actor) > 120 or any(ord(ch) < 32 for ch in single_actor):
                raise HTTPException(status_code=503, detail="AI_TEAM_API_ACTOR must be a valid fixed service identity in production.")
            existing = credentials.get(single_token)
            if existing is not None and existing != single_actor:
                raise HTTPException(status_code=503, detail="The same production token cannot map to multiple actor identities.")
            credentials[single_token] = single_actor

        if not credentials:
            raise HTTPException(status_code=503, detail="Production API needs AI_TEAM_API_TOKENS or a strong AI_TEAM_API_TOKEN.")
        matched_actor = None
        for candidate, identity in credentials.items():
            if hmac.compare_digest(supplied, candidate):
                matched_actor = identity
        if matched_actor is None:
            raise HTTPException(status_code=401, detail="Valid Bearer token required.")
        supplied_actor = (x_actor or "").strip()
        if supplied_actor and supplied_actor != matched_actor:
            raise HTTPException(status_code=403, detail="X-Actor cannot override the identity mapped to the production bearer token.")
        return matched_actor

    token = os.getenv("AI_TEAM_API_TOKEN", "").strip()
    if token and not hmac.compare_digest(supplied, token):
        raise HTTPException(status_code=401, detail="Valid Bearer token required.")
    actor = (x_actor or "").strip()
    if not actor:
        raise HTTPException(status_code=400, detail="X-Actor header is required for traceability in development.")
    return actor


def safe_error(exc: Exception) -> HTTPException:
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


@app.get("/health")
def health() -> dict[str, Any]:
    chain = store.verify_audit_chain()
    return {
        "status": "ok" if chain["valid"] else "degraded",
        "service": "ai-team-control-plane",
        "registry_schema": registry.SCHEMA_VERSION,
        "audit_chain_valid": chain["valid"],
        "audit_events_checked": chain["checked_events"],
    }


@app.get("/projects")
def get_projects(_: str = Depends(authenticated_actor)) -> dict:
    return registry.snapshot()


@app.post("/tasks", status_code=201)
def create_task(
    body: CreateTask,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    actor: str = Depends(authenticated_actor),
) -> dict:
    try:
        return orchestrator.create_task(**body.model_dump(), idempotency_key=idempotency_key, actor=actor)
    except Exception as exc:
        raise safe_error(exc) from exc


@app.get("/tasks")
def list_tasks(
    project_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    _: str = Depends(authenticated_actor),
) -> dict:
    return {"items": store.list_tasks(project_id=project_id, status=status, limit=limit)}


@app.get("/tasks/{task_id}")
def get_task(task_id: str, _: str = Depends(authenticated_actor)) -> dict:
    value = store.get_task(task_id)
    if value is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return value


@app.post("/tasks/{task_id}/claim")
def claim_task(task_id: str, body: ClaimTask, actor: str = Depends(authenticated_actor)) -> dict:
    try:
        # In production, bind lease ownership to the authenticated token identity.
        worker_id = actor if os.getenv("APP_ENV", "development").strip().lower() == "production" else body.worker_id
        return orchestrator.claim_task(task_id, worker_id=worker_id, lease_seconds=body.lease_seconds, actor=actor)
    except Exception as exc:
        raise safe_error(exc) from exc


@app.post("/tasks/{task_id}/transition")
def transition_task(task_id: str, body: TransitionTask, actor: str = Depends(authenticated_actor)) -> dict:
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
def record_evidence(task_id: str, body: EvidenceInput, actor: str = Depends(authenticated_actor)) -> dict:
    try:
        # API callers can record attestations, but cannot mark them trusted. Trusted records must come from verifier adapters.
        return orchestrator.record_evidence(task_id, **body.model_dump(), actor=actor, trusted=False, verified_by=None)
    except Exception as exc:
        raise safe_error(exc) from exc


@app.post("/tasks/{task_id}/request-verification")
def request_verification(task_id: str, actor: str = Depends(authenticated_actor)) -> dict:
    try:
        return orchestrator.request_verification(task_id, actor=actor)
    except Exception as exc:
        raise safe_error(exc) from exc


@app.post("/tasks/{task_id}/verify")
def verify_task(task_id: str, actor: str = Depends(authenticated_actor)) -> dict:
    try:
        return orchestrator.verify_task(task_id, actor=actor)
    except Exception as exc:
        raise safe_error(exc) from exc


@app.get("/audit/verify")
def verify_audit(_: str = Depends(authenticated_actor)) -> dict:
    return store.verify_audit_chain()


@app.get("/outbox")
def get_outbox(limit: int = Query(default=100, ge=1, le=1000), _: str = Depends(authenticated_actor)) -> dict:
    return {"items": store.list_outbox(limit)}
