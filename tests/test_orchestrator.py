import sqlite3

import pytest

from ai_team_control_plane.models import BoundaryViolation, EvidenceRejected, IdempotencyConflict, StateConflict
from ai_team_control_plane.store import DurableStore


def create_task(service, **overrides):
    payload = {
        "project_id": "axsikent-it",
        "title": "Add registry boundary checks",
        "description": "Implement and test strict project scoping.",
        "task_type": "implementation",
        "repository": "Maktab-PRO/Axsikent-IT",
        "branch": "ai-team/registry-boundary-checks",
        "paths": ["docs/control-plane.md"],
        "priority": 2,
        "assigned_role": "engineer",
        "dependencies": [],
        "idempotency_key": "test-task-001",
        "actor": "test-suite",
    }
    payload.update(overrides)
    return service.create_task(**payload)


def test_task_created_with_durable_identity_and_outbox(system):
    _, store, _, service = system
    task = create_task(service)
    assert task["status"] == "queued"
    assert task["project_id"] == "axsikent-it"
    assert len(task["id"]) == 36
    assert len(store.list_outbox()) == 1
    assert store.verify_audit_chain()["valid"] is True


def test_create_task_is_idempotent_but_rejects_reused_key_with_new_body(system):
    _, _, _, service = system
    first = create_task(service)
    again = create_task(service)
    assert again["id"] == first["id"]
    with pytest.raises(IdempotencyConflict):
        create_task(service, title="Different request with same idempotency key")


def test_illegal_transition_is_blocked(system):
    _, _, _, service = system
    task = create_task(service)
    with pytest.raises(StateConflict, match="protected by Evidence Gate"):
        service.transition(task["id"], "verified", actor="tester")


def test_claim_lease_prevents_two_workers_claiming_same_task(system):
    _, _, _, service = system
    task = create_task(service)
    claimed = service.claim_task(task["id"], worker_id="worker-a", actor="worker-a", lease_seconds=60)
    assert claimed["status"] == "claimed"
    assert claimed["lease_owner"] == "worker-a"
    with pytest.raises(StateConflict, match="leased by another worker"):
        service.claim_task(task["id"], worker_id="worker-b", actor="worker-b", lease_seconds=60)


def test_dependency_must_be_verified_before_claim(system):
    _, _, _, service = system
    parent = create_task(service, idempotency_key="parent-task")
    child = create_task(service, idempotency_key="child-task", title="Use prerequisite", dependencies=[parent["id"]])
    with pytest.raises(StateConflict, match="prerequisite tasks are not verified"):
        service.claim_task(child["id"], worker_id="worker-a", actor="worker-a")


def test_cross_project_dependency_is_denied(system):
    _, _, _, service = system
    parent = create_task(service, idempotency_key="axs-task")
    with pytest.raises(StateConflict, match="Cross-project"):
        create_task(
            service,
            idempotency_key="tech-task",
            project_id="technikum-digital-platform",
            title="Technikum task",
            repository="Maktab-PRO/TECHNIKUM-DIGITAL-PLATFORM",
            branch="ai-team/technikum-task",
            dependencies=[parent["id"]],
        )


def test_evidence_gate_rejects_manual_passing_labels(system):
    _, _, _, service = system
    task = create_task(service)
    service.claim_task(task["id"], worker_id="worker-a", actor="worker-a")
    service.transition(task["id"], "in_progress", actor="worker-a")
    service.record_evidence(task["id"], kind="tests_passed", status="passed", uri="https://github.com/org/repo/actions/runs/111", source="github_actions", actor="user-api", trusted=False)
    service.record_evidence(task["id"], kind="ci_passed", status="passed", uri="https://github.com/org/repo/actions/runs/112", source="github_actions", actor="user-api", trusted=False)
    service.record_evidence(task["id"], kind="review_approved", status="passed", uri="https://github.com/org/repo/pull/1", source="github_review", actor="user-api", trusted=False)
    service.request_verification(task["id"], actor="worker-a")
    with pytest.raises(EvidenceRejected, match="untrusted"):
        service.verify_task(task["id"], actor="auditor")


def test_evidence_gate_verifies_only_complete_trusted_evidence(system):
    _, store, _, service = system
    task = create_task(service)
    service.claim_task(task["id"], worker_id="worker-a", actor="worker-a")
    service.transition(task["id"], "in_progress", actor="worker-a")
    records = [
        ("tests_passed", "test_runner", "test://runs/tests-001"),
        ("ci_passed", "github_actions", "https://github.com/org/repo/actions/runs/113"),
        ("review_approved", "github_review", "https://github.com/org/repo/pull/2"),
    ]
    for kind, source, uri in records:
        service.record_evidence(
            task["id"], kind=kind, status="passed", uri=uri, source=source,
            actor="verified-adapter", trusted=True, verified_by="unit-test-verifier",
        )
    service.request_verification(task["id"], actor="worker-a")
    verified = service.verify_task(task["id"], actor="evidence-gate")
    assert verified["status"] == "verified"
    assert store.verify_audit_chain()["valid"] is True


def test_evidence_placeholder_uri_is_rejected(system):
    _, _, _, service = system
    task = create_task(service)
    with pytest.raises(ValueError, match="Placeholder"):
        service.record_evidence(task["id"], kind="tests_passed", status="passed", uri="https://example.invalid/todo", source="github_actions", actor="tester")


def test_audit_hash_chain_detects_tampering(system):
    _, store, _, service = system
    task = create_task(service)
    assert store.verify_audit_chain()["valid"] is True
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with store.transaction() as conn:
            conn.execute("UPDATE audit_events SET payload_json='{}' WHERE task_id=?", (task["id"],))
    # Simulate out-of-band database-file tampering: even after bypassing the guard trigger, hashes detect it.
    with store.transaction() as conn:
        conn.execute("DROP TRIGGER audit_events_no_update")
        conn.execute("UPDATE audit_events SET payload_json='{}' WHERE task_id=?", (task["id"],))
    result = store.verify_audit_chain()
    assert result["valid"] is False
    assert result["failed_event_id"] == 1


def test_database_reopen_preserves_tasks_and_audit(system, tmp_path):
    _, store, _, service = system
    task = create_task(service)
    reopened = DurableStore(store.db_path)
    found = reopened.get_task(task["id"])
    assert found is not None
    assert found["title"] == task["title"]
    assert reopened.verify_audit_chain()["valid"] is True


def test_unknown_repository_or_protected_branch_fails_before_insert(system):
    _, store, _, service = system
    with pytest.raises(BoundaryViolation):
        create_task(service, idempotency_key="wrong-repo", repository="Maktab-PRO/TECHNIKUM-DIGITAL-PLATFORM")
    with pytest.raises(BoundaryViolation):
        create_task(service, idempotency_key="main-write", branch="main")
    assert store.list_tasks() == []


def test_latest_trusted_failure_overrides_an_older_pass(system):
    _, _, _, service = system
    task = create_task(service, idempotency_key="latest-evidence-test")
    service.claim_task(task["id"], worker_id="worker-a", actor="worker-a")
    service.transition(task["id"], "in_progress", actor="worker-a")
    records = [
        ("tests_passed", "test_runner", "test://runs/tests-older"),
        ("ci_passed", "github_actions", "https://github.com/org/repo/actions/runs/201"),
        ("review_approved", "github_review", "https://github.com/org/repo/pull/201"),
    ]
    for kind, source, uri in records:
        service.record_evidence(task["id"], kind=kind, status="passed", uri=uri, source=source, actor="trusted-adapter", trusted=True, verified_by="unit-test-verifier")
    service.record_evidence(
        task["id"], kind="tests_passed", status="failed", uri="test://runs/tests-newer-failure",
        source="test_runner", actor="trusted-adapter", trusted=True, verified_by="unit-test-verifier",
    )
    service.request_verification(task["id"], actor="worker-a")
    with pytest.raises(EvidenceRejected, match="latest trusted evidence failed"):
        service.verify_task(task["id"], actor="evidence-gate")


def test_only_current_lease_owner_can_transition_work(system):
    _, _, _, service = system
    task = create_task(service, idempotency_key="owner-lock-test")
    service.claim_task(task["id"], worker_id="worker-a", actor="worker-a")
    with pytest.raises(StateConflict, match="leased by 'worker-a'"):
        service.transition(task["id"], "in_progress", actor="worker-b")
    progressed = service.transition(task["id"], "in_progress", actor="worker-a")
    assert progressed["lease_owner"] == "worker-a"
    assert progressed["lease_until"]


def test_durable_store_refuses_ephemeral_memory_database():
    with pytest.raises(ValueError, match="file-backed database path"):
        DurableStore(":memory:")
