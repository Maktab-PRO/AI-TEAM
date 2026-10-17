from fastapi.testclient import TestClient

from ai_team_control_plane import api


def setup_api(monkeypatch, system):
    registry, store, guard, orchestrator = system
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("AI_TEAM_API_TOKEN", raising=False)
    monkeypatch.setattr(api, "registry", registry)
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api, "guard", guard)
    monkeypatch.setattr(api, "orchestrator", orchestrator)
    return TestClient(api.app)


def valid_task_payload(**changes):
    payload = {
        "project_id": "axsikent-it",
        "title": "API-scoped task",
        "description": "Verify API boundary behavior.",
        "task_type": "implementation",
        "repository": "Maktab-PRO/Axsikent-IT",
        "branch": "ai-team/api-boundary",
        "paths": ["docs/control-plane.md"],
        "priority": 2,
        "assigned_role": "engineer",
        "dependencies": [],
    }
    payload.update(changes)
    return payload


def test_api_requires_actor_and_creates_task_with_idempotency(monkeypatch, system):
    client = setup_api(monkeypatch, system)
    no_actor = client.get("/projects")
    assert no_actor.status_code == 400
    headers = {"X-Actor": "api-test", "Idempotency-Key": "api-key-1"}
    response = client.post("/tasks", json=valid_task_payload(), headers=headers)
    assert response.status_code == 201, response.text
    task = response.json()
    assert task["status"] == "queued"
    second = client.post("/tasks", json=valid_task_payload(), headers=headers)
    assert second.status_code == 201
    assert second.json()["id"] == task["id"]


def test_api_rejects_cross_project_repo(monkeypatch, system):
    client = setup_api(monkeypatch, system)
    response = client.post(
        "/tasks",
        json=valid_task_payload(repository="Maktab-PRO/TECHNIKUM-DIGITAL-PLATFORM"),
        headers={"X-Actor": "api-test", "Idempotency-Key": "api-key-cross"},
    )
    assert response.status_code == 403
    assert "Boundary violation" in response.json()["detail"]


def test_api_evidence_submission_cannot_self_assert_trust(monkeypatch, system):
    client = setup_api(monkeypatch, system)
    create = client.post(
        "/tasks",
        json=valid_task_payload(),
        headers={"X-Actor": "api-test", "Idempotency-Key": "api-key-evidence"},
    )
    task_id = create.json()["id"]
    response = client.post(
        f"/tasks/{task_id}/evidence",
        json={
            "kind": "tests_passed",
            "status": "passed",
            "uri": "https://github.com/org/repo/actions/runs/1234",
            "source": "github_actions",
            "notes": "User submitted reference only.",
        },
        headers={"X-Actor": "api-test"},
    )
    assert response.status_code == 201
    assert response.json()["trusted"] is False


def test_production_api_fails_closed_without_strong_token(monkeypatch, system):
    client = setup_api(monkeypatch, system)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("AI_TEAM_API_TOKEN", raising=False)
    response = client.get("/projects", headers={"X-Actor": "api-test"})
    assert response.status_code == 503


def test_production_api_requires_bearer_token(monkeypatch, system):
    client = setup_api(monkeypatch, system)
    token = "super-secret-test-token-0123456789-abcdef"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AI_TEAM_API_TOKEN", token)
    no_token = client.get("/projects", headers={"X-Actor": "api-test"})
    assert no_token.status_code == 401
    authenticated = client.get("/projects", headers={"Authorization": f"Bearer {token}"})
    assert authenticated.status_code == 200
    assert api.authenticated_actor(
        request=None,
        authorization=f"Bearer {token}",
        x_actor="api-token",
    ) == "api-token"
    spoofed_actor = client.get(
        "/projects",
        headers={"X-Actor": "admin-impersonation", "Authorization": f"Bearer {token}"},
    )
    assert spoofed_actor.status_code == 403


def test_api_uses_version_preconditions_and_preserves_worker_lease(monkeypatch, system):
    client = setup_api(monkeypatch, system)
    created = client.post(
        "/tasks",
        json=valid_task_payload(),
        headers={"X-Actor": "api-test", "Idempotency-Key": "api-key-version"},
    )
    task_id = created.json()["id"]
    claim = client.post(
        f"/tasks/{task_id}/claim",
        json={"worker_id": "api-test", "lease_seconds": 120},
        headers={"X-Actor": "api-test"},
    )
    assert claim.status_code == 200
    claimed_version = claim.json()["version"]
    transition = client.post(
        f"/tasks/{task_id}/transition",
        json={"status": "in_progress", "expected_version": claimed_version, "reason": "Begin work"},
        headers={"X-Actor": "api-test"},
    )
    assert transition.status_code == 200, transition.text
    assert transition.json()["lease_owner"] == "api-test"
    stale = client.post(
        f"/tasks/{task_id}/transition",
        json={"status": "blocked", "expected_version": claimed_version, "reason": "Stale write test"},
        headers={"X-Actor": "api-test"},
    )
    assert stale.status_code == 409
    assert "Stale task version" in stale.json()["detail"]


def test_public_health_is_minimal_and_reports_audit_status(monkeypatch, system):
    client = setup_api(monkeypatch, system)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["service"] == "ai-team-control-plane"
    assert response.json()["audit_chain_valid"] is True


def test_production_api_actor_cannot_be_spoofed_and_is_pinned(monkeypatch, system):
    setup_api(monkeypatch, system)
    token = "production-secret-test-token-0123456789-abcdef"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AI_TEAM_API_TOKEN", token)
    monkeypatch.setenv("AI_TEAM_API_ACTOR", "ci-service")
    client = TestClient(api.app)
    response = client.get("/projects", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert api.authenticated_actor(None, f"Bearer {token}", "ci-service") == "ci-service"
    impersonation = client.get(
        "/projects",
        headers={"Authorization": f"Bearer {token}", "X-Actor": "project-owner"},
    )
    assert impersonation.status_code == 403


def test_production_token_map_binds_distinct_actor_identities(monkeypatch, system):
    setup_api(monkeypatch, system)
    token_a = "token-for-worker-a-0123456789-abcdef"
    token_b = "token-for-reviewer-b-0123456789-abcdef"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("AI_TEAM_API_TOKEN", raising=False)
    monkeypatch.setenv("AI_TEAM_API_TOKENS", __import__("json").dumps({token_a: "worker-a", token_b: "reviewer-b"}))
    client = TestClient(api.app)
    worker_response = client.get("/projects", headers={"Authorization": f"Bearer {token_a}"})
    assert worker_response.status_code == 200
    assert api.authenticated_actor(None, f"Bearer {token_a}", None) == "worker-a"
    wrong_identity = client.get(
        "/projects",
        headers={"Authorization": f"Bearer {token_a}", "X-Actor": "reviewer-b"},
    )
    assert wrong_identity.status_code == 403


def test_production_claim_uses_authenticated_actor_as_lease_owner(monkeypatch, system):
    setup_api(monkeypatch, system)
    token = "worker-identity-secret-0123456789-abcdef"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AI_TEAM_API_TOKEN", token)
    monkeypatch.setenv("AI_TEAM_API_ACTOR", "worker-a")
    client = TestClient(api.app)
    created = client.post(
        "/tasks",
        json=valid_task_payload(),
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "production-lease-bind"},
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    claimed = client.post(
        f"/tasks/{task_id}/claim",
        json={"worker_id": "spoofed-worker", "lease_seconds": 60},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["lease_owner"] == "worker-a"
    progressed = client.post(
        f"/tasks/{task_id}/transition",
        json={"status": "in_progress", "expected_version": claimed.json()["version"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert progressed.status_code == 200, progressed.text
