from __future__ import annotations

import json

import pytest

from ai_team_control_plane.integrations.github_review import GitHubPullRequestReviewVerifier
from ai_team_control_plane.models import EvidenceRejected


class FakeResponse:
    def __init__(self, value):
        self.payload = json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self.payload if size < 0 else self.payload[:size]


PULL_URL = "https://github.com/Maktab-PRO/Axsikent-IT/pull/42"
HEAD_SHA = "c" * 40


def task_for(service, *, task_type="implementation", branch="ai-team/github-review", key="github-review-task"):
    return service.create_task(
        project_id="axsikent-it",
        title="Verify GitHub PR review",
        description="Review provenance integration test.",
        task_type=task_type,
        repository="Maktab-PRO/Axsikent-IT",
        branch=branch,
        paths=["docs/control-plane.md"],
        idempotency_key=key,
        actor="review-test",
    )


def pr_payload(**changes):
    value = {
        "number": 42,
        "html_url": PULL_URL,
        "state": "open",
        "merged": False,
        "user": {"login": "pr-author"},
        "head": {
            "ref": "ai-team/github-review",
            "sha": HEAD_SHA,
            "repo": {"full_name": "Maktab-PRO/Axsikent-IT"},
        },
        "base": {
            "ref": "main",
            "repo": {"full_name": "Maktab-PRO/Axsikent-IT"},
        },
    }
    value.update(changes)
    return value


def approval(login="independent-reviewer", *, state="APPROVED", commit_id=HEAD_SHA, review_id=1, submitted_at="2026-10-17T10:00:00Z"):
    return {
        "id": review_id,
        "user": {"login": login},
        "state": state,
        "commit_id": commit_id,
        "submitted_at": submitted_at,
    }


def make_opener(pr=None, reviews=None):
    calls = []

    def opener(request, timeout=10):
        url = request.full_url
        calls.append((url, request.headers, timeout))
        if url.endswith("/pulls/42"):
            return FakeResponse(pr if pr is not None else pr_payload())
        if "/pulls/42/reviews?" in url:
            return FakeResponse(reviews if reviews is not None else [approval()])
        raise AssertionError(f"Unexpected GitHub API URL: {url}")

    return opener, calls


def verifier(service, opener):
    from pathlib import Path
    from ai_team_control_plane.registry import ProjectRegistry

    registry = ProjectRegistry(Path(__file__).resolve().parents[1] / "config" / "projects.json")
    return GitHubPullRequestReviewVerifier(
        service,
        registry,
        "read-only-pr-token",
        opener=opener,
    )


def test_verifier_records_trusted_review_for_exact_current_head(system):
    _, store, _, service = system
    task = task_for(service)
    opener, calls = make_opener()
    result = verifier(service, opener).verify_and_record(task["id"], pull_url=PULL_URL, actor="review-verifier")

    assert result["pull_number"] == 42
    assert result["head_sha"] == HEAD_SHA
    assert result["approved_reviewers"] == ["independent-reviewer"]
    assert result["recorded_kind"] == "review_approved"
    assert len(calls) == 2
    evidence = store.list_evidence(task["id"])
    assert len(evidence) == 1
    assert evidence[0]["kind"] == "review_approved"
    assert evidence[0]["trusted"] is True
    assert evidence[0]["verified_by"] == "github-pr-api"
    assert evidence[0]["source"] == "github_review"


def test_verifier_rejects_approval_for_stale_commit(system):
    _, store, _, service = system
    task = task_for(service, key="github-review-stale-sha")
    stale = approval(commit_id="d" * 40)
    opener, _ = make_opener(reviews=[stale])

    with pytest.raises(EvidenceRejected, match="current pull request head commit"):
        verifier(service, opener).verify_and_record(task["id"], pull_url=PULL_URL, actor="review-verifier")

    assert store.list_evidence(task["id"]) == []


def test_verifier_rejects_author_self_approval(system):
    _, store, _, service = system
    task = task_for(service, key="github-review-self-approval")
    opener, _ = make_opener(reviews=[approval(login="PR-AUTHOR")])

    with pytest.raises(EvidenceRejected, match="current pull request head commit"):
        verifier(service, opener).verify_and_record(task["id"], pull_url=PULL_URL, actor="review-verifier")

    assert store.list_evidence(task["id"]) == []


def test_verifier_rejects_changes_requested_even_when_another_reviewer_approved(system):
    _, store, _, service = system
    task = task_for(service, key="github-review-changes-requested")
    reviews = [
        approval(login="approved-reviewer"),
        approval(login="blocking-reviewer", state="CHANGES_REQUESTED", review_id=2, submitted_at="2026-10-17T11:00:00Z"),
    ]
    opener, _ = make_opener(reviews=reviews)

    with pytest.raises(EvidenceRejected, match="CHANGES_REQUESTED"):
        verifier(service, opener).verify_and_record(task["id"], pull_url=PULL_URL, actor="review-verifier")

    assert store.list_evidence(task["id"]) == []


def test_verifier_rejects_fork_head_repository(system):
    _, store, _, service = system
    task = task_for(service, key="github-review-fork")
    pr = pr_payload(head={
        "ref": "ai-team/github-review",
        "sha": HEAD_SHA,
        "repo": {"full_name": "external-user/Axsikent-IT"},
    })
    opener, _ = make_opener(pr=pr)

    with pytest.raises(EvidenceRejected, match="same registered repository"):
        verifier(service, opener).verify_and_record(task["id"], pull_url=PULL_URL, actor="review-verifier")

    assert store.list_evidence(task["id"]) == []


def test_verifier_rejects_wrong_head_branch(system):
    _, store, _, service = system
    task = task_for(service, key="github-review-wrong-branch")
    pr = pr_payload(head={
        "ref": "main",
        "sha": HEAD_SHA,
        "repo": {"full_name": "Maktab-PRO/Axsikent-IT"},
    })
    opener, _ = make_opener(pr=pr)

    with pytest.raises(EvidenceRejected, match="does not match task branch"):
        verifier(service, opener).verify_and_record(task["id"], pull_url=PULL_URL, actor="review-verifier")

    assert store.list_evidence(task["id"]) == []


def test_verifier_rejects_closed_or_merged_pull_request(system):
    _, store, _, service = system
    task = task_for(service, key="github-review-closed")
    opener, _ = make_opener(pr=pr_payload(state="closed", merged=True))

    with pytest.raises(EvidenceRejected, match="must still be open and unmerged"):
        verifier(service, opener).verify_and_record(task["id"], pull_url=PULL_URL, actor="review-verifier")

    assert store.list_evidence(task["id"]) == []


def test_verifier_rejects_wrong_default_base_branch(system):
    _, store, _, service = system
    task = task_for(service, key="github-review-wrong-base")
    pr = pr_payload(base={
        "ref": "release",
        "repo": {"full_name": "Maktab-PRO/Axsikent-IT"},
    })
    opener, _ = make_opener(pr=pr)

    with pytest.raises(EvidenceRejected, match="does not match registered default branch"):
        verifier(service, opener).verify_and_record(task["id"], pull_url=PULL_URL, actor="review-verifier")

    assert store.list_evidence(task["id"]) == []


def test_verifier_rejects_task_types_without_review_requirement(system):
    _, store, _, service = system
    task = task_for(service, task_type="deployment", key="github-review-wrong-task-type")
    opener, _ = make_opener()

    with pytest.raises(EvidenceRejected, match="does not require an approved PR review"):
        verifier(service, opener).verify_and_record(task["id"], pull_url=PULL_URL, actor="review-verifier")

    assert store.list_evidence(task["id"]) == []




def test_verifier_rejects_oversized_github_review_response(system):
    _, store, _, service = system
    task = task_for(service, key="github-review-oversized-response")

    def opener(request, timeout=10):
        if request.full_url.endswith("/pulls/42"):
            return FakeResponse({"large": "x" * 2_100_000})
        raise AssertionError("No API call beyond the oversized PR response should occur")

    with pytest.raises(EvidenceRejected, match="exceeded the configured size limit"):
        verifier(service, opener).verify_and_record(task["id"], pull_url=PULL_URL, actor="review-verifier")
    assert store.list_evidence(task["id"]) == []

def test_verifier_rejects_non_github_pull_url(system):
    _, _, _, service = system
    task = task_for(service, key="github-review-host-attack")
    opener, calls = make_opener()

    with pytest.raises(EvidenceRejected, match="exact HTTPS github.com"):
        verifier(service, opener).verify_and_record(
            task["id"],
            pull_url="https://github.com.example.invalid/Maktab-PRO/Axsikent-IT/pull/42",
            actor="review-verifier",
        )

    assert calls == []
