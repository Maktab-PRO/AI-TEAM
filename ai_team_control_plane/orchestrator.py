from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .boundary import BoundaryGuard
from .evidence import EvidenceGate, validate_evidence_uri
from .models import EvidenceRejected, IdempotencyConflict, StateConflict, TaskStatus
from .store import DurableStore, canonical_json, utc_now


_ALLOWED_TASK_TYPES = {"implementation", "bugfix", "deployment", "security", "documentation"}
_TRANSITIONS = {
    "draft": {"queued", "cancelled"},
    "queued": {"claimed", "cancelled"},
    "claimed": {"in_progress", "queued", "blocked", "cancelled"},
    "in_progress": {"blocked", "queued", "evidence_pending", "cancelled"},
    "blocked": {"queued", "in_progress", "cancelled"},
    "evidence_pending": {"in_progress", "verified", "rejected"},
    "rejected": {"in_progress", "cancelled"},
    "verified": set(),
    "cancelled": set(),
}


def _clean_actor(actor: str) -> str:
    actor = actor.strip()
    if not actor or len(actor) > 120 or any(ord(ch) < 32 for ch in actor):
        raise ValueError("A non-empty actor identity (max 120 characters) is required.")
    return actor


def _fingerprint(body: dict) -> str:
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


class TaskOrchestrator:
    def __init__(self, store: DurableStore, guard: BoundaryGuard):
        self.store = store
        self.guard = guard
        self.gate = EvidenceGate(store)

    def create_task(
        self,
        *,
        project_id: str,
        title: str,
        description: str,
        task_type: str,
        repository: str,
        branch: str,
        paths: list[str] | None = None,
        priority: int = 3,
        assigned_role: str = "engineer",
        dependencies: list[str] | None = None,
        idempotency_key: str,
        actor: str,
    ) -> dict:
        actor = _clean_actor(actor)
        title = title.strip()
        if not title or len(title) > 240:
            raise ValueError("Task title must be 1–240 characters.")
        if task_type not in _ALLOWED_TASK_TYPES:
            raise ValueError(f"Task type must be one of {sorted(_ALLOWED_TASK_TYPES)}")
        if not isinstance(priority, int) or not 1 <= priority <= 5:
            raise ValueError("Priority must be an integer from 1 to 5.")
        if not idempotency_key.strip() or len(idempotency_key) > 200:
            raise ValueError("A stable Idempotency-Key (1–200 characters) is required.")
        paths = sorted(set(paths or []))
        dependencies = sorted(set(dependencies or []))
        self.guard.authorize(project_id=project_id, repository=repository, branch=branch, paths=paths, action="write")
        request_body = {
            "project_id": project_id,
            "title": title,
            "description": description.strip(),
            "task_type": task_type,
            "repository": repository,
            "branch": branch,
            "paths": paths,
            "priority": priority,
            "assigned_role": assigned_role.strip() or "engineer",
            "dependencies": dependencies,
        }
        request_hash = _fingerprint(request_body)
        task_id = str(uuid.uuid4())
        now = utc_now()
        with self.store.transaction() as conn:
            existing = conn.execute("SELECT * FROM tasks WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise IdempotencyConflict("Idempotency-Key was already used with a different request body.")
                return self.store.task_dict(existing)
            for dependency_id in dependencies:
                dep = conn.execute("SELECT project_id, status FROM tasks WHERE id = ?", (dependency_id,)).fetchone()
                if dep is None:
                    raise StateConflict(f"Dependency task '{dependency_id}' does not exist.")
                if dep["project_id"] != project_id:
                    raise StateConflict("Cross-project task dependencies are forbidden by Boundary Guard.")
            conn.execute(
                """INSERT INTO tasks(id,project_id,title,description,task_type,repository,branch,paths_json,status,priority,assigned_role,creator,idempotency_key,request_hash,version,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, project_id, title, description.strip(), task_type, repository, branch, json.dumps(paths), TaskStatus.QUEUED.value, priority, request_body["assigned_role"], actor, idempotency_key, request_hash, 1, now, now),
            )
            for dependency_id in dependencies:
                conn.execute("INSERT INTO task_dependencies(task_id,depends_on_task_id) VALUES (?,?)", (task_id, dependency_id))
            payload = {"task_id": task_id, **request_body, "status": TaskStatus.QUEUED.value, "version": 1}
            self.store.append_audit(conn, actor, "task.created", payload, task_id)
            self.store.append_outbox(conn, task_id, "task.created", payload)
        return self.store.get_task(task_id)

    def claim_task(self, task_id: str, *, worker_id: str, lease_seconds: int = 300, actor: str) -> dict:
        actor, worker_id = _clean_actor(actor), _clean_actor(worker_id)
        if not 15 <= lease_seconds <= 3600:
            raise ValueError("Lease must be between 15 and 3600 seconds.")
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat(timespec="milliseconds")
        lease_until = (now_dt + timedelta(seconds=lease_seconds)).isoformat(timespec="milliseconds")
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task ID: {task_id}")
            status = row["status"]
            lease_expired = not row["lease_until"] or row["lease_until"] <= now
            if status == "claimed" and row["lease_owner"] != worker_id and not lease_expired:
                raise StateConflict(f"Task is leased by another worker until {row['lease_until']}.")
            if status not in {"queued", "claimed"}:
                raise StateConflict(f"Only queued or expired-lease claimed tasks can be claimed; current status is {status}.")
            deps = conn.execute(
                "SELECT t.id, t.status FROM task_dependencies d JOIN tasks t ON t.id=d.depends_on_task_id WHERE d.task_id=?",
                (task_id,),
            ).fetchall()
            unready = [f"{d['id']}({d['status']})" for d in deps if d["status"] != "verified"]
            if unready:
                raise StateConflict("Dependency gate: prerequisite tasks are not verified: " + ", ".join(unready))
            old_status = status
            conn.execute(
                "UPDATE tasks SET status='claimed',lease_owner=?,lease_until=?,version=version+1,updated_at=? WHERE id=?",
                (worker_id, lease_until, now, task_id),
            )
            payload = {"task_id": task_id, "from": old_status, "to": "claimed", "worker_id": worker_id, "lease_until": lease_until}
            self.store.append_audit(conn, actor, "task.claimed", payload, task_id)
            self.store.append_outbox(conn, task_id, "task.claimed", payload)
        return self.store.get_task(task_id)

    def transition(
        self,
        task_id: str,
        new_status: str,
        *,
        actor: str,
        reason: str = "",
        expected_version: int | None = None,
    ) -> dict:
        actor = _clean_actor(actor)
        try:
            target = TaskStatus(new_status)
        except ValueError as exc:
            raise StateConflict(f"Unknown task status '{new_status}'.") from exc
        if target == TaskStatus.VERIFIED:
            raise StateConflict("VERIFIED is protected by Evidence Gate; call verify_task instead.")
        now = utc_now()
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task ID: {task_id}")
            current = row["status"]
            if expected_version is not None and row["version"] != expected_version:
                raise StateConflict(
                    f"Stale task version: expected {expected_version}, current version is {row['version']}. Reload the task before retrying."
                )
            if target.value not in _TRANSITIONS[current]:
                raise StateConflict(f"Illegal status transition: {current} -> {target.value}.")
            if target == TaskStatus.CLAIMED:
                raise StateConflict("Use claim_task so lease ownership is recorded atomically.")
            if row["lease_owner"] and current in {"claimed", "in_progress"} and actor != row["lease_owner"]:
                raise StateConflict(
                    f"Task is leased by '{row['lease_owner']}'; that worker must make the transition or release the lease."
                )
            if target == TaskStatus.IN_PROGRESS:
                conn.execute(
                    "UPDATE tasks SET status=?,version=version+1,updated_at=? WHERE id=?",
                    (target.value, now, task_id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET status=?,lease_owner=NULL,lease_until=NULL,version=version+1,updated_at=? WHERE id=?",
                    (target.value, now, task_id),
                )
            payload = {"task_id": task_id, "from": current, "to": target.value, "reason": reason[:1000]}
            self.store.append_audit(conn, actor, "task.transitioned", payload, task_id)
            self.store.append_outbox(conn, task_id, "task.transitioned", payload)
        return self.store.get_task(task_id)

    def record_evidence(
        self,
        task_id: str,
        *,
        kind: str,
        status: str,
        uri: str,
        source: str,
        actor: str,
        notes: str = "",
        artifact_sha256: str | None = None,
        trusted: bool = False,
        verified_by: str | None = None,
    ) -> dict:
        actor = _clean_actor(actor)
        if status not in {"passed", "failed", "informational"}:
            raise EvidenceRejected("Evidence status must be passed, failed, or informational.")
        validate_evidence_uri(uri)
        if not re.fullmatch(r"[a-fA-F0-9]{64}", artifact_sha256 or "") and artifact_sha256 is not None:
            raise EvidenceRejected("artifact_sha256 must be a 64-character hexadecimal SHA-256 digest.")
        if trusted and (not verified_by or source not in {"test_runner", "github_actions", "ci_provider", "github_review", "human_review", "render_health", "monitoring", "documentation_check", "security_scanner"}):
            raise EvidenceRejected("Trusted evidence must be produced by an approved verifier adapter; clients cannot self-attest trust.")
        evidence_id = str(uuid.uuid4())
        now = utc_now()
        with self.store.transaction() as conn:
            row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task ID: {task_id}")
            if row["status"] in {"verified", "cancelled"}:
                raise StateConflict(f"Evidence cannot be changed on a {row['status']} task.")
            conn.execute(
                """INSERT INTO evidence(id,task_id,kind,status,uri,artifact_sha256,source,trusted,verified_by,actor,notes,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (evidence_id, task_id, kind, status, uri, artifact_sha256, source, 1 if trusted else 0, verified_by, actor, notes[:2000], now),
            )
            conn.execute("UPDATE tasks SET version=version+1,updated_at=? WHERE id=?", (now, task_id))
            payload = {"evidence_id": evidence_id, "task_id": task_id, "kind": kind, "status": status, "uri": uri, "source": source, "trusted": trusted, "verified_by": verified_by}
            self.store.append_audit(conn, actor, "evidence.recorded", payload, task_id)
            self.store.append_outbox(conn, task_id, "evidence.recorded", payload)
        return next(item for item in self.store.list_evidence(task_id) if item["id"] == evidence_id)

    def request_verification(self, task_id: str, *, actor: str) -> dict:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"Unknown task ID: {task_id}")
        if task["status"] != "in_progress":
            raise StateConflict("Only in_progress tasks can enter evidence_pending.")
        return self.transition(
            task_id,
            "evidence_pending",
            actor=actor,
            reason="Evidence submission requested",
            expected_version=task["version"],
        )

    def verify_task(self, task_id: str, *, actor: str) -> dict:
        actor = _clean_actor(actor)
        now = utc_now()
        with self.store.transaction() as conn:
            row = conn.execute("SELECT status,version FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task ID: {task_id}")
            if row["status"] != "evidence_pending":
                raise StateConflict("Task must be evidence_pending before Evidence Gate can verify it.")
            # Check evidence after taking the write lock to avoid a check/update race.
            gate = self.gate.check(task_id)
            if not gate.passed:
                raise EvidenceRejected(
                    f"Evidence Gate denied verification. Missing={list(gate.missing)}; "
                    f"failed={list(gate.failed)}; untrusted={list(gate.untrusted)}"
                )
            conn.execute("UPDATE tasks SET status='verified',lease_owner=NULL,lease_until=NULL,version=version+1,updated_at=? WHERE id=?", (now, task_id))
            payload = {"task_id": task_id, "from": "evidence_pending", "to": "verified", "gate": {"passed": True, "summary": gate.summary}}
            self.store.append_audit(conn, actor, "task.verified", payload, task_id)
            self.store.append_outbox(conn, task_id, "task.verified", payload)
        return self.store.get_task(task_id)
