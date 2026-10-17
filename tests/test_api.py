import pytest
from fastapi.testclient import TestClient

from ai_team_control_plane import api


def setup_api(monkeypatch, system):
    registry, store, guard, orchestrator = system
    monkeypatch.setenv("APP_ENV", "development")
    for name in ("AI_TEAM_API_TOKEN", "AI_TEAM_API_TOKENS", "AI_TEAM_API_ACTOR", "AI_TEAM_API_ROLE", "AI_TEAM_API_PROJECTS", "AI_TEAM_GITHUB_TOKEN", "AI_TEAM_GITHUB_WORKFLOW_ALLOWLIST"):
        monkeypatch.delenv(name, raising=False)
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
    monkeypatch.setenv("AI_TEAM_API_ROLE", "platform_admin")
    monkeypatch.setenv("AI_TEAM_API_PROJECTS", "*")
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
    monkeypatch.setenv("AI_TEAM_API_ROLE", "platform_admin")
    monkeypatch.setenv("AI_TEAM_API_PROJECTS", "*")
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
    monkeypatch.setenv("AI_TEAM_API_TOKENS", __import__("json").dumps({
        token_a: {"actor": "worker-a", "role": "worker", "projects": ["axsikent-it"]},
        token_b: {"actor": "reviewer-b", "role": "reviewer", "projects": ["axsikent-it"]},
    }))
    client = TestClient(api.app)
    worker_response = client.get("/projects", headers={"Authorization": f"Bearer {token_a}"})
    assert worker_response.status_code == 200
    assert [project["id"] for project in worker_response.json()["projects"]] == ["axsikent-it"]
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
    monkeypatch.setenv("AI_TEAM_API_ROLE", "worker")
    monkeypatch.setenv("AI_TEAM_API_PROJECTS", "axsikent-it")
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


def test_project_scopes_filter_reads_and_block_cross_project_operations(monkeypatch, system):
    client = setup_api(monkeypatch, system)
    axsikent = client.post(
        "/tasks",
        json=valid_task_payload(title="Scoped Axsikent task"),
        headers={"X-Actor": "dev-admin", "Idempotency-Key": "scope-axsikent"},
    )
    technikum = client.post(
        "/tasks",
        json=valid_task_payload(
            project_id="technikum-digital-platform",
            repository="Maktab-PRO/TECHNIKUM-DIGITAL-PLATFORM",
            title="Scoped Technikum task",
        ),
        headers={"X-Actor": "dev-admin", "Idempotency-Key": "scope-technikum"},
    )
    assert axsikent.status_code == 201, axsikent.text
    assert technikum.status_code == 201, technikum.text
    technikum_id = technikum.json()["id"]

    token = "scoped-worker-token-0123456789-abcdef"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "AI_TEAM_API_TOKENS",
        __import__("json").dumps({
            token: {"actor": "axsikent-worker", "role": "worker", "projects": ["axsikent-it"]},
        }),
    )
    headers = {"Authorization": f"Bearer {token}"}

    project_list = client.get("/projects", headers=headers)
    assert project_list.status_code == 200
    assert [project["id"] for project in project_list.json()["projects"]] == ["axsikent-it"]

    task_list = client.get("/tasks", headers=headers)
    assert task_list.status_code == 200
    assert all(item["project_id"] == "axsikent-it" for item in task_list.json()["items"])

    hidden_task = client.get(f"/tasks/{technikum_id}", headers=headers)
    assert hidden_task.status_code == 404
    hidden_claim = client.post(
        f"/tasks/{technikum_id}/claim",
        json={"worker_id": "spoofed", "lease_seconds": 60},
        headers=headers,
    )
    assert hidden_claim.status_code == 404

    forbidden_create = client.post(
        "/tasks",
        json=valid_task_payload(
            project_id="technikum-digital-platform",
            repository="Maktab-PRO/TECHNIKUM-DIGITAL-PLATFORM",
            title="Cross-project create attempt",
        ),
        headers={**headers, "Idempotency-Key": "cross-scope-create"},
    )
    assert forbidden_create.status_code == 403


def test_reviewer_can_read_but_cannot_claim_or_create(monkeypatch, system):
    client = setup_api(monkeypatch, system)
    created = client.post(
        "/tasks",
        json=valid_task_payload(),
        headers={"X-Actor": "dev-admin", "Idempotency-Key": "reviewer-role-task"},
    )
    assert created.status_code == 201
    task_id = created.json()["id"]

    token = "reviewer-role-token-0123456789-abcdef"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "AI_TEAM_API_TOKENS",
        __import__("json").dumps({
            token: {"actor": "reviewer-1", "role": "reviewer", "projects": ["axsikent-it"]},
        }),
    )
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/tasks", headers=headers).status_code == 200

    claim = client.post(
        f"/tasks/{task_id}/claim",
        json={"worker_id": "reviewer-1", "lease_seconds": 60},
        headers=headers,
    )
    assert claim.status_code == 403

    create = client.post(
        "/tasks",
        json=valid_task_payload(title="Reviewer must not create tasks"),
        headers={**headers, "Idempotency-Key": "reviewer-create-denied"},
    )
    assert create.status_code == 403


def test_project_admin_cannot_read_global_audit_or_outbox(monkeypatch, system):
    client = setup_api(monkeypatch, system)
    token = "project-admin-token-0123456789-abcdef"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "AI_TEAM_API_TOKENS",
        __import__("json").dumps({
            token: {"actor": "project-admin", "role": "project_admin", "projects": ["axsikent-it"]},
        }),
    )
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/audit/verify", headers=headers).status_code == 403
    assert client.get("/outbox", headers=headers).status_code == 403


def test_production_legacy_identity_only_token_map_fails_closed(monkeypatch, system):
    client = setup_api(monkeypatch, system)
    token = "legacy-token-must-not-be-unscoped-012345"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AI_TEAM_API_TOKENS", __import__("json").dumps({token: "legacy-actor"}))
    response = client.get("/projects", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 503


def test_github_actions_endpoint_uses_configured_verifier_and_records_only_server_result(monkeypatch, system):
    client = setup_api(monkeypatch, system)
    created = client.post(
        "/tasks",
        json=valid_task_payload(),
        headers={"X-Actor": "api-test", "Idempotency-Key": "github-actions-endpoint"},
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    monkeypatch.setenv("AI_TEAM_GITHUB_TOKEN", "fake-secret-token")
    monkeypatch.setenv(
        "AI_TEAM_GITHUB_WORKFLOW_ALLOWLIST",
        __import__("json").dumps({"Maktab-PRO/Axsikent-IT": [".github/workflows/ci.yml"]}),
    )

    called = {}

    class FakeVerifier:
        def __init__(self, service, token, *, approved_workflows):
            called["service"] = service
            called["token"] = token
            called["approved_workflows"] = approved_workflows

        def verify_and_record(self, task_id, *, run_url, actor):
            called.update({"task_id": task_id, "run_url": run_url, "actor": actor})
            return {"task_id": task_id, "recorded_kinds": ["tests_passed", "ci_passed"]}

    monkeypatch.setattr(api, "GitHubActionsEvidenceVerifier", FakeVerifier)
    response = client.post(
        f"/tasks/{task_id}/evidence/github-actions",
        json={"run_url": "https://github.com/Maktab-PRO/Axsikent-IT/actions/runs/123456"},
        headers={"X-Actor": "api-test"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["recorded_kinds"] == ["tests_passed", "ci_passed"]
    assert called["token"] == "fake-secret-token"
    assert called["approved_workflows"] == {"Maktab-PRO/Axsikent-IT": [".github/workflows/ci.yml"]}
    assert called["task_id"] == task_id
    assert called["actor"] == "api-test"


def test_github_actions_endpoint_fails_closed_without_token(monkeypatch, system):
    client = setup_api(monkeypatch, system)
    created = client.post(
        "/tasks",
        json=valid_task_payload(),
        headers={"X-Actor": "api-test", "Idempotency-Key": "github-actions-no-token"},
    )
    task_id = created.json()["id"]
    response = client.post(
        f"/tasks/{task_id}/evidence/github-actions",
        json={"run_url": "https://github.com/Maktab-PRO/Axsikent-IT/actions/runs/123456"},
        headers={"X-Actor": "api-test"},
    )
    assert response.status_code == 503
    assert "AI_TEAM_GITHUB_TOKEN" in response.json()["detail"]


def test_github_actions_endpoint_enforces_project_scope_before_verifier(monkeypatch, system):
    client = setup_api(monkeypatch, system)
    created = client.post(
        "/tasks",
        json=valid_task_payload(
            project_id="technikum-digital-platform",
            repository="Maktab-PRO/TECHNIKUM-DIGITAL-PLATFORM",
        ),
        headers={"X-Actor": "api-test", "Idempotency-Key": "github-actions-cross-scope"},
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    token = "scoped-ci-worker-secret-token-0123456789"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "AI_TEAM_API_TOKENS",
        __import__("json").dumps({
            token: {"actor": "axsikent-worker", "role": "worker", "projects": ["axsikent-it"]},
        }),
    )
    monkeypatch.setenv("AI_TEAM_GITHUB_TOKEN", "fake-github-token")
    monkeypatch.setattr(
        api,
        "GitHubActionsEvidenceVerifier",
        lambda *_args, **_kwargs: pytest.fail("verifier must not be constructed for cross-project task access"),
    )
    response = client.post(
        f"/tasks/{task_id}/evidence/github-actions",
        json={"run_url": "https://github.com/Maktab-PRO/TECHNIKUM-DIGITAL-PLATFORM/actions/runs/123456"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404



def test_github_review_endpoint_uses_server_side_verifier(monkeypatch, system):
    client = setup_api(monkeypatch, system)
    created = client.post(
        "/tasks",
        json=valid_task_payload(),
        headers={"X-Actor": "api-test", "Idempotency-Key": "github-review-endpoint"},
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    monkeypatch.setenv("AI_TEAM_GITHUB_TOKEN", "fake-pr-read-token")

    called = {}

    class FakeReviewVerifier:
        def __init__(self, service, project_registry, token):
            called["service"] = service
            called["registry"] = project_registry
            called["token"] = token

        def verify_and_record(self, task_id, *, pull_url, actor):
            called.update({"task_id": task_id, "pull_url": pull_url, "actor": actor})
            return {"task_id": task_id, "recorded_kind": "review_approved"}

    monkeypatch.setattr(api, "GitHubPullRequestReviewVerifier", FakeReviewVerifier)
    response = client.post(
        f"/tasks/{task_id}/evidence/github-review",
        json={"pull_url": "https://github.com/Maktab-PRO/Axsikent-IT/pull/42"},
        headers={"X-Actor": "api-test"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["recorded_kind"] == "review_approved"
    assert called["token"] == "fake-pr-read-token"
    assert called["task_id"] == task_id
    assert called["actor"] == "api-test"


def test_github_review_endpoint_fails_closed_without_token(monkeypatch, system):
    client = setup_api(monkeypatch, system)
    created = client.post(
        "/tasks",
        json=valid_task_payload(),
        headers={"X-Actor": "api-test", "Idempotency-Key": "github-review-no-token"},
    )
    task_id = created.json()["id"]
    response = client.post(
        f"/tasks/{task_id}/evidence/github-review",
        json={"pull_url": "https://github.com/Maktab-PRO/Axsikent-IT/pull/42"},
        headers={"X-Actor": "api-test"},
    )
    assert response.status_code == 503
    assert "AI_TEAM_GITHUB_TOKEN" in response.json()["detail"]
