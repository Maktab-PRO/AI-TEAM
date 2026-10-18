from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from ..evidence import REQUIREMENTS
from ..models import EvidenceRejected


class GitHubPullRequestReviewVerifier:
    """Promote PR review evidence only after checking GitHub's REST API."""

    API_ROOT = "https://api.github.com"
    VERIFIER_ID = "github-pr-api"
    MAX_API_RESPONSE_BYTES = 2_000_000

    def __init__(
        self,
        orchestrator,
        registry,
        token: str,
        *,
        opener: Callable[..., Any] | None = None,
        timeout: float = 10.0,
    ):
        self.orchestrator = orchestrator
        self.registry = registry
        self.token = token.strip()
        self.opener = opener or urlopen
        self.timeout = timeout

    def _get_json(self, url: str) -> Any:
        if not self.token:
            raise EvidenceRejected("GitHub PR review verification is not configured: AI_TEAM_GITHUB_TOKEN is missing.")
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "AI-TEAM-Control-Plane",
            },
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                body = response.read(self.MAX_API_RESPONSE_BYTES + 1)
                if len(body) > self.MAX_API_RESPONSE_BYTES:
                    raise EvidenceRejected("GitHub PR API response exceeded the configured size limit; no trusted evidence was recorded.")
        except HTTPError as exc:
            raise EvidenceRejected(
                f"GitHub PR review API rejected the verification request (HTTP {exc.code}); check read-only token permissions and repository access."
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise EvidenceRejected("GitHub PR review API could not be reached; no trusted evidence was recorded.") from exc
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceRejected("GitHub PR review API returned invalid JSON; no trusted evidence was recorded.") from exc
        if not isinstance(payload, (dict, list)):
            raise EvidenceRejected("GitHub PR review API response had an unexpected shape.")
        return payload

    @staticmethod
    def _parse_pull_url(pull_url: str) -> tuple[str, str, int]:
        parsed = urlparse(pull_url)
        if parsed.scheme != "https" or parsed.netloc.casefold() != "github.com" or parsed.query or parsed.fragment:
            raise EvidenceRejected("Pull request URL must be an exact HTTPS github.com link without query or fragment.")
        parts = parsed.path.strip("/").split("/")
        if (
            len(parts) != 4
            or not parts[0]
            or not parts[1]
            or parts[2] != "pull"
            or not re.fullmatch(r"[0-9]+", parts[3])
        ):
            raise EvidenceRejected("Pull request URL must identify one exact /owner/repository/pull/{number} resource.")
        return parts[0], parts[1], int(parts[3])

    def _get_all_reviews(self, owner: str, repository: str, pull_number: int) -> list[dict[str, Any]]:
        reviews: list[dict[str, Any]] = []
        for page in range(1, 11):
            url = (
                f"{self.API_ROOT}/repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
                f"/pulls/{pull_number}/reviews?per_page=100&page={page}"
            )
            payload = self._get_json(url)
            if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
                raise EvidenceRejected("GitHub PR reviews API response is incomplete or malformed.")
            reviews.extend(payload)
            if len(payload) < 100:
                break
        else:
            raise EvidenceRejected("More than 1,000 PR reviews found; pagination limit reached and evidence was not trusted.")
        if not reviews:
            raise EvidenceRejected("Pull request has no submitted reviews; no trusted evidence was recorded.")
        return reviews

    def verify_and_record(self, task_id: str, *, pull_url: str, actor: str) -> dict[str, Any]:
        task = self.orchestrator.store.get_task(task_id)
        if task is None:
            raise KeyError(f"Unknown task ID: {task_id}")

        owner, repository, pull_number = self._parse_pull_url(pull_url)
        requested_repository = f"{owner}/{repository}"
        if requested_repository.casefold() != task["repository"].casefold():
            raise EvidenceRejected(
                f"Pull request repository {requested_repository!r} does not match task repository {task['repository']!r}."
            )

        project = self.registry.require(task["project_id"])
        if not project.repository or project.repository.casefold() != task["repository"].casefold():
            raise EvidenceRejected("Task repository no longer matches the authoritative project registry.")
        if "review_approved" not in {
            requirement.kind for requirement in REQUIREMENTS.get(task["task_type"], ())
        }:
            raise EvidenceRejected(f"Task type {task['task_type']!r} does not require an approved PR review.")

        api_repo = f"{quote(owner, safe='')}/{quote(repository, safe='')}"
        pr = self._get_json(f"{self.API_ROOT}/repos/{api_repo}/pulls/{pull_number}")
        if not isinstance(pr, dict):
            raise EvidenceRejected("GitHub returned an invalid pull request resource.")
        if pr.get("number") != pull_number or pr.get("html_url") != pull_url:
            raise EvidenceRejected("GitHub API pull request number or canonical URL did not match the supplied URL.")

        returned_base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
        returned_head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
        base_repo = returned_base.get("repo") if isinstance(returned_base.get("repo"), dict) else {}
        head_repo = returned_head.get("repo") if isinstance(returned_head.get("repo"), dict) else {}
        base_full_name = base_repo.get("full_name")
        head_full_name = head_repo.get("full_name")
        if not isinstance(base_full_name, str) or base_full_name.casefold() != task["repository"].casefold():
            raise EvidenceRejected("Pull request base repository does not match the task repository.")
        if not isinstance(head_full_name, str) or head_full_name.casefold() != task["repository"].casefold():
            raise EvidenceRejected("Pull request head must come from the same registered repository; fork evidence is not accepted.")
        if returned_head.get("ref") != task["branch"]:
            raise EvidenceRejected(
                f"Pull request head branch {returned_head.get('ref')!r} does not match task branch {task['branch']!r}."
            )
        if returned_base.get("ref") != project.default_branch:
            raise EvidenceRejected(
                f"Pull request base branch {returned_base.get('ref')!r} does not match registered default branch {project.default_branch!r}."
            )
        if pr.get("state") != "open" or pr.get("merged") is True:
            raise EvidenceRejected("Pull request must still be open and unmerged when review evidence is verified.")

        head_sha = returned_head.get("sha")
        if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", head_sha):
            raise EvidenceRejected("Pull request has no valid head commit SHA.")
        author = pr.get("user") if isinstance(pr.get("user"), dict) else {}
        author_login = author.get("login")
        if not isinstance(author_login, str) or not author_login:
            raise EvidenceRejected("Pull request author identity is missing; no review evidence was recorded.")

        reviews = self._get_all_reviews(owner, repository, pull_number)

        # Re-check the repository branch after fetching all reviews. The PR object
        # fetched earlier can become stale while paginated reviews are being read.
        current_branch = self._get_json(
            f"{self.API_ROOT}/repos/{api_repo}/branches/{quote(task['branch'], safe='')}"
        )
        current_commit = current_branch.get("commit") if isinstance(current_branch.get("commit"), dict) else {}
        if current_branch.get("name") != task["branch"] or current_commit.get("sha") != head_sha:
            raise EvidenceRejected(
                "Pull request branch head changed during review verification; no trusted evidence was recorded."
            )

        latest_by_reviewer: dict[str, dict[str, Any]] = {}
        for review in reviews:
            user = review.get("user") if isinstance(review.get("user"), dict) else {}
            login = user.get("login")
            state = review.get("state")
            submitted_at = review.get("submitted_at")
            if (
                not isinstance(login, str)
                or not login
                or not isinstance(state, str)
                or not isinstance(submitted_at, str)
            ):
                continue
            if state.upper() not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
                continue
            reviewer_key = login.casefold()
            existing = latest_by_reviewer.get(reviewer_key)
            current_order = (submitted_at, int(review.get("id", 0)) if str(review.get("id", "")).isdigit() else 0)
            existing_order = (
                existing.get("submitted_at", ""),
                int(existing.get("id", 0)) if str(existing.get("id", "")).isdigit() else 0,
            ) if existing else ("", 0)
            if existing is None or current_order > existing_order:
                latest_by_reviewer[reviewer_key] = review

        current_changes_requested = [
            str((review.get("user") or {}).get("login", reviewer_key))
            for reviewer_key, review in latest_by_reviewer.items()
            if reviewer_key != author_login.casefold()
            and str(review.get("state", "")).upper() == "CHANGES_REQUESTED"
        ]
        if current_changes_requested:
            raise EvidenceRejected(
                "A reviewer still has a latest CHANGES_REQUESTED review: "
                + ", ".join(sorted(current_changes_requested))
                + "; no trusted evidence was recorded."
            )

        approved_reviewers = sorted(
            str((review.get("user") or {}).get("login", reviewer_key))
            for reviewer_key, review in latest_by_reviewer.items()
            if reviewer_key != author_login.casefold()
            and str(review.get("state", "")).upper() == "APPROVED"
            and review.get("commit_id") == head_sha
        )
        if not approved_reviewers:
            raise EvidenceRejected(
                "No non-author APPROVED review applies to the current pull request head commit; no trusted evidence was recorded."
            )

        notes = (
            f"Verified through GitHub Pull Requests REST API; pull_number={pull_number}; "
            f"head_sha={head_sha}; head_branch={task['branch']}; base_branch={project.default_branch}; "
            f"approved_reviewers={', '.join(approved_reviewers)}"
        )
        evidence = self.orchestrator.record_evidence(
            task_id,
            kind="review_approved",
            status="passed",
            uri=pull_url,
            source="github_review",
            actor=actor,
            notes=notes,
            trusted=True,
            verified_by=self.VERIFIER_ID,
        )
        return {
            "task_id": task_id,
            "repository": task["repository"],
            "pull_number": pull_number,
            "head_sha": head_sha,
            "approved_reviewers": approved_reviewers,
            "recorded_kind": "review_approved",
            "evidence": evidence,
        }
