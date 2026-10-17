from __future__ import annotations

import json

import pytest

from ai_team_control_plane.integrations.github_evidence import GitHubActionsEvidenceVerifier
from ai_team_control_plane.models import EvidenceRejected


class FakeResponse:
    def __init__(self, value):
        self.payload = json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


RUN_URL = "https://github.com/Maktab-PRO/Axsikent-IT/actions/runs/123456"
HEAD_SHA = "a" * 40


def task_for(service, task_type="implementation", branch="ai-team/github-evidence", key="github-evidence-task"):
    return service.create_task(
        project_id="axsikent-it",
        title="Verify GitHub Actions evidence",
        description="Integration test for CI provenance.",
        task_type=task_type,
        repository="Maktab-PRO/Axsikent-IT",
        branch=branch,
        paths=["docs/control-plane.md"],
        idempotency_key=key,
        actor="test-suite",
    )


def run_payload(**changes):
    value = {
        "id": 123456,
        "name": "AI TEAM Control Plane CI",
        "path": ".github/workflows/ci.yml",
        "repository": {"full_name": "Maktab-PRO/Axsikent-IT"},
        "html_url": RUN_URL,
        "head_branch": "ai-team/github-evidence",
        "head_sha": HEAD_SHA,
        "status": "completed",
        "conclusion": "success",
    }
    value.update(changes)
    return value


def jobs_payload(jobs=None):
    if jobs is None:
        jobs = [
            {"name": "test (3.12)", "status": "completed", "conclusion": "success", "steps": []},
            {
                "name": "security",
                "status": "completed",
                "conclusion": "success",
                "steps": [{"name": "Static security analysis", "status": "completed", "conclusion": "success"}],
            },
        ]
    return {"total_count": len(jobs), "jobs": jobs}


def make_opener(run=None, jobs=None):
    calls = []

    def opener(request, timeout=10):
        url = request.full_url
        calls.append((url, request.headers, timeout))
        if "/actions/runs/123456/jobs?" in url:
            return FakeResponse(jobs if jobs is not None else jobs_payload())
        if url.endswith("/actions/runs/123456"):
            return FakeResponse(run if run is not None else run_payload())
        raise AssertionError(f"Unexpected GitHub API URL: {url}")

    return opener, calls


def test_verifier_records_ci_and_test_evidence_only_after_github_api_success(system):
    _, store, _, service = system
    task = task_for(service)
    opener, calls = make_opener()
    verifier = GitHubActionsEvidenceVerifier(
        service,
        "read-only-test-token",
        approved_workflows={"Maktab-PRO/Axsikent-IT": [".github/workflows/ci.yml"]},
        opener=opener,
    )

    result = verifier.verify_and_record(task["id"], run_url=RUN_URL, actor="ci-verifier")

    assert result["run_id"] == 123456
    assert result["head_sha"] == HEAD_SHA
    assert result["recorded_kinds"] == ["tests_passed", "ci_passed"]
    evidence = store.list_evidence(task["id"])
    assert {item["kind"] for item in evidence} == {"tests_passed", "ci_passed"}
    assert all(item["trusted"] is True for item in evidence)
    assert all(item["verified_by"] == "github-actions-api" for item in evidence)
    assert all(item["source"] == "github_actions" for item in evidence)
    assert len(calls) == 2
    assert all(headers.get("Authorization") == "Bearer read-only-test-token" for _, headers, _ in calls)


def test_verifier_rejects_repository_mismatch_before_api_call(system):
    _, store, _, service = system
    task = task_for(service)
    opener, calls = make_opener()
    verifier = GitHubActionsEvidenceVerifier(
        service,
        "read-only-test-token",
        approved_workflows={"Maktab-PRO/Axsikent-IT": [".github/workflows/ci.yml"]},
        opener=opener,
    )

    with pytest.raises(EvidenceRejected, match="does not match task repository"):
        verifier.verify_and_record(
            task["id"],
            run_url="https://github.com/Maktab-PRO/TECHNIKUM-DIGITAL-PLATFORM/actions/runs/123456",
            actor="ci-verifier",
        )

    assert calls == []
    assert store.list_evidence(task["id"]) == []


def test_verifier_rejects_noncanonical_or_untrusted_run_url(system):
    _, _, _, service = system
    task = task_for(service)
    opener, calls = make_opener()
    verifier = GitHubActionsEvidenceVerifier(
        service,
        "read-only-test-token",
        approved_workflows={"Maktab-PRO/Axsikent-IT": [".github/workflows/ci.yml"]},
        opener=opener,
    )

    with pytest.raises(EvidenceRejected, match="exact HTTPS github.com"):
        verifier.verify_and_record(
            task["id"],
            run_url="https://github.com.evil.example/Maktab-PRO/Axsikent-IT/actions/runs/123456",
            actor="ci-verifier",
        )

    assert calls == []


def test_verifier_rejects_wrong_branch_and_records_nothing(system):
    _, store, _, service = system
    task = task_for(service)
    wrong_branch = run_payload(head_branch="main")
    opener, _ = make_opener(run=wrong_branch)
    verifier = GitHubActionsEvidenceVerifier(
        service,
        "read-only-test-token",
        approved_workflows={"Maktab-PRO/Axsikent-IT": [".github/workflows/ci.yml"]},
        opener=opener,
    )

    with pytest.raises(EvidenceRejected, match="does not match the task branch"):
        verifier.verify_and_record(task["id"], run_url=RUN_URL, actor="ci-verifier")

    assert store.list_evidence(task["id"]) == []


def test_verifier_rejects_unsuccessful_run_and_records_nothing(system):
    _, store, _, service = system
    task = task_for(service)
    opener, _ = make_opener(run=run_payload(conclusion="failure"))
    verifier = GitHubActionsEvidenceVerifier(
        service,
        "read-only-test-token",
        approved_workflows={"Maktab-PRO/Axsikent-IT": [".github/workflows/ci.yml"]},
        opener=opener,
    )

    with pytest.raises(EvidenceRejected, match="not completed successfully"):
        verifier.verify_and_record(task["id"], run_url=RUN_URL, actor="ci-verifier")

    assert store.list_evidence(task["id"]) == []


def test_security_task_requires_successful_security_job_and_test_job(system):
    _, store, _, service = system
    task = task_for(service, task_type="security", key="github-security-evidence")
    only_tests = jobs_payload([
        {"name": "test (3.12)", "status": "completed", "conclusion": "success", "steps": []},
    ])
    opener, _ = make_opener(jobs=only_tests)
    verifier = GitHubActionsEvidenceVerifier(
        service,
        "read-only-test-token",
        approved_workflows={"Maktab-PRO/Axsikent-IT": [".github/workflows/ci.yml"]},
        opener=opener,
    )

    with pytest.raises(EvidenceRejected, match="security_scan_passed"):
        verifier.verify_and_record(task["id"], run_url=RUN_URL, actor="ci-verifier")

    assert store.list_evidence(task["id"]) == []


def test_bugfix_requires_explicit_regression_signal(system):
    _, store, _, service = system
    task = task_for(service, task_type="bugfix", key="github-bugfix-evidence")
    opener, _ = make_opener()
    verifier = GitHubActionsEvidenceVerifier(
        service,
        "read-only-test-token",
        approved_workflows={"Maktab-PRO/Axsikent-IT": [".github/workflows/ci.yml"]},
        opener=opener,
    )

    with pytest.raises(EvidenceRejected, match="regression_test_passed"):
        verifier.verify_and_record(task["id"], run_url=RUN_URL, actor="ci-verifier")

    assert store.list_evidence(task["id"]) == []


def test_missing_github_token_fails_closed(system):
    _, _, _, service = system
    task = task_for(service)
    verifier = GitHubActionsEvidenceVerifier(
        service,
        "",
        approved_workflows={"Maktab-PRO/Axsikent-IT": [".github/workflows/ci.yml"]},
        opener=lambda *_args, **_kwargs: pytest.fail("network must not be called"),
    )

    with pytest.raises(EvidenceRejected, match="AI_TEAM_GITHUB_TOKEN is missing"):
        verifier.verify_and_record(task["id"], run_url=RUN_URL, actor="ci-verifier")



def test_verifier_rejects_workflow_path_not_in_allowlist(system):
    _, store, _, service = system
    task = task_for(service, key="github-unapproved-workflow-path")
    unapproved = run_payload(path=".github/workflows/untrusted.yml")
    opener, _ = make_opener(run=unapproved)
    verifier = GitHubActionsEvidenceVerifier(
        service,
        "read-only-test-token",
        approved_workflows={"Maktab-PRO/Axsikent-IT": [".github/workflows/ci.yml"]},
        opener=opener,
    )

    with pytest.raises(EvidenceRejected, match="not approved"):
        verifier.verify_and_record(task["id"], run_url=RUN_URL, actor="ci-verifier")

    assert store.list_evidence(task["id"]) == []
