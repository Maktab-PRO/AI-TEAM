from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from ..evidence import REQUIREMENTS
from ..models import EvidenceRejected


class GitHubActionsEvidenceVerifier:
    """Create trusted evidence only after checking GitHub's Actions REST API.

    The supplied run URL is not trusted by itself. Repository, branch, run status,
    conclusion, SHA, and the required test/security job signals are checked against
    GitHub's API response before a trusted attestation is recorded.
    """

    API_ROOT = "https://api.github.com"
    VERIFIER_ID = "github-actions-api"

    def __init__(
        self,
        orchestrator,
        token: str,
        *,
        approved_workflows: dict[str, list[str] | tuple[str, ...]],
        opener: Callable[..., Any] | None = None,
        timeout: float = 10.0,
    ):
        self.orchestrator = orchestrator
        self.token = token.strip()
        self.opener = opener or urlopen
        self.timeout = timeout
        self.approved_workflows = {
            repository.casefold(): frozenset(paths)
            for repository, paths in approved_workflows.items()
        }

    def _get_json(self, url: str) -> dict[str, Any]:
        if not self.token:
            raise EvidenceRejected("GitHub Actions verification is not configured: AI_TEAM_GITHUB_TOKEN is missing.")
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
                body = response.read()
        except HTTPError as exc:
            raise EvidenceRejected(
                f"GitHub Actions API rejected the verification request (HTTP {exc.code}); check read-only token permissions and repository access."
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise EvidenceRejected("GitHub Actions API could not be reached; no trusted evidence was recorded.") from exc
        try:
            result = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceRejected("GitHub Actions API returned invalid JSON; no trusted evidence was recorded.") from exc
        if not isinstance(result, dict):
            raise EvidenceRejected("GitHub Actions API response had an unexpected shape.")
        return result

    @staticmethod
    def _parse_run_url(run_url: str) -> tuple[str, str, int]:
        parsed = urlparse(run_url)
        if (
            parsed.scheme != "https"
            or parsed.netloc.casefold() != "github.com"
            or parsed.query
            or parsed.fragment
        ):
            raise EvidenceRejected("Run URL must be an exact HTTPS github.com Actions run link without query or fragment.")
        parts = parsed.path.strip("/").split("/")
        if (
            len(parts) != 5
            or parts[2] != "actions"
            or parts[3] != "runs"
            or not parts[0]
            or not parts[1]
            or not re.fullmatch(r"[0-9]+", parts[4])
        ):
            raise EvidenceRejected("Run URL must identify one exact /owner/repo/actions/runs/{numeric-id} resource.")
        return parts[0], parts[1], int(parts[4])

    def _get_all_jobs(self, owner: str, repository: str, run_id: int) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        page = 1
        total_count: int | None = None
        while page <= 10:
            url = (
                f"{self.API_ROOT}/repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
                f"/actions/runs/{run_id}/jobs?per_page=100&page={page}"
            )
            payload = self._get_json(url)
            page_jobs = payload.get("jobs")
            reported_total = payload.get("total_count")
            if not isinstance(page_jobs, list) or not isinstance(reported_total, int):
                raise EvidenceRejected("GitHub Actions jobs API response is incomplete or malformed.")
            if total_count is None:
                total_count = reported_total
            elif total_count != reported_total:
                raise EvidenceRejected("GitHub Actions jobs list changed while being verified; retry the check.")
            jobs.extend(job for job in page_jobs if isinstance(job, dict))
            if len(jobs) >= total_count or not page_jobs:
                break
            page += 1
        if total_count is None or len(jobs) != total_count:
            raise EvidenceRejected("Could not retrieve the complete GitHub Actions jobs list; no trusted evidence was recorded.")
        if not jobs:
            raise EvidenceRejected("GitHub Actions run has no job records; no trusted evidence was recorded.")
        return jobs

    @staticmethod
    def _successful_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            job for job in jobs
            if job.get("status") == "completed" and job.get("conclusion") == "success"
        ]

    @staticmethod
    def _has_successful_step(job: dict[str, Any], pattern: re.Pattern[str]) -> bool:
        steps = job.get("steps")
        if not isinstance(steps, list):
            return False
        return any(
            isinstance(step, dict)
            and isinstance(step.get("name"), str)
            and pattern.search(step["name"])
            and step.get("status") == "completed"
            and step.get("conclusion") == "success"
            for step in steps
        )

    def verify_and_record(self, task_id: str, *, run_url: str, actor: str) -> dict[str, Any]:
        task = self.orchestrator.store.get_task(task_id)
        if task is None:
            raise KeyError(f"Unknown task ID: {task_id}")

        owner, repository, run_id = self._parse_run_url(run_url)
        requested_repository = f"{owner}/{repository}"
        if requested_repository.casefold() != task["repository"].casefold():
            raise EvidenceRejected(
                f"Run repository {requested_repository!r} does not match task repository {task['repository']!r}."
            )
        approved_paths = self.approved_workflows.get(task["repository"].casefold())
        if not approved_paths:
            raise EvidenceRejected(
                f"No approved GitHub Actions workflow paths are configured for {task['repository']!r}; no trusted evidence was recorded."
            )

        api_repo = f"{quote(owner, safe='')}/{quote(repository, safe='')}"
        run = self._get_json(f"{self.API_ROOT}/repos/{api_repo}/actions/runs/{run_id}")
        returned_repository = (run.get("repository") or {}).get("full_name") if isinstance(run.get("repository"), dict) else None
        if not isinstance(returned_repository, str) or returned_repository.casefold() != task["repository"].casefold():
            raise EvidenceRejected("GitHub API repository identity does not match the task's registered repository.")
        if run.get("id") != run_id:
            raise EvidenceRejected("GitHub API returned a different run ID than requested.")
        if run.get("html_url") != run_url:
            raise EvidenceRejected("GitHub API canonical run URL does not match the supplied URL.")
        if run.get("head_branch") != task["branch"]:
            raise EvidenceRejected(
                f"Run branch {run.get('head_branch')!r} does not match the task branch {task['branch']!r}."
            )
        workflow_path = run.get("path")
        if not isinstance(workflow_path, str) or workflow_path not in approved_paths:
            raise EvidenceRejected(
                f"Workflow path {workflow_path!r} is not approved for repository {task['repository']!r}; no trusted evidence was recorded."
            )
        head_sha = run.get("head_sha")
        if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", head_sha):
            raise EvidenceRejected("GitHub Actions run has no valid commit SHA.")
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            raise EvidenceRejected("GitHub Actions run is not completed successfully; no trusted evidence was recorded.")

        # A historical successful run is not sufficient if the branch has moved since it ran.
        branch = self._get_json(
            f"{self.API_ROOT}/repos/{api_repo}/branches/{quote(task['branch'], safe='')}"
        )
        branch_commit = branch.get("commit") if isinstance(branch.get("commit"), dict) else {}
        if branch.get("name") != task["branch"] or branch_commit.get("sha") != head_sha:
            raise EvidenceRejected(
                "The task branch no longer points to this run's head SHA; rerun CI for the current branch commit before trusting evidence."
            )

        jobs = self._get_all_jobs(owner, repository, run_id)
        successful_jobs = self._successful_jobs(jobs)
        job_names = [str(job.get("name", "")) for job in successful_jobs]
        successful_test_jobs = [
            job for job in successful_jobs if re.search(r"\btest(s)?\b", str(job.get("name", "")), re.IGNORECASE)
        ]
        successful_regression_jobs = [
            job for job in successful_jobs
            if re.search(r"\bregression\b", str(job.get("name", "")), re.IGNORECASE)
            or self._has_successful_step(job, re.compile(r"\bregression\b", re.IGNORECASE))
        ]
        successful_security_jobs = [
            job for job in successful_jobs
            if re.search(r"\bsecurity\b", str(job.get("name", "")), re.IGNORECASE)
            or self._has_successful_step(job, re.compile(r"\b(security|bandit|pip-audit|codeql)\b", re.IGNORECASE))
        ]

        requirements = REQUIREMENTS.get(task["task_type"])
        if requirements is None:
            raise EvidenceRejected(f"No approved evidence policy exists for task type {task['task_type']!r}.")
        required_kinds = [
            requirement.kind
            for requirement in requirements
            if "github_actions" in requirement.accepted_sources
        ]
        if not required_kinds:
            raise EvidenceRejected(
                "This task type has no evidence requirement that GitHub Actions is approved to attest."
            )

        for kind in required_kinds:
            if kind == "ci_passed":
                continue  # The complete workflow run itself is completed with conclusion=success.
            if kind == "tests_passed" and successful_test_jobs:
                continue
            if kind == "regression_test_passed" and successful_regression_jobs:
                continue
            if kind == "security_scan_passed" and successful_security_jobs:
                continue
            raise EvidenceRejected(
                f"Successful GitHub Actions run does not provide the required '{kind}' signal "
                "from a completed successful job/step; no trusted evidence was recorded."
            )

        notes = (
            f"Verified through GitHub Actions REST API; run_id={run_id}; "
            f"workflow={run.get('name', '')}; head_sha={head_sha}; "
            f"head_branch={task['branch']}; successful_jobs={', '.join(job_names)[:1200]}"
        )
        records = []
        for kind in required_kinds:
            record = self.orchestrator.record_evidence(
                task_id,
                kind=kind,
                status="passed",
                uri=run_url,
                source="github_actions",
                actor=actor,
                notes=notes,
                trusted=True,
                verified_by=self.VERIFIER_ID,
            )
            records.append(record)

        return {
            "task_id": task_id,
            "repository": task["repository"],
            "branch": task["branch"],
            "run_id": run_id,
            "head_sha": head_sha,
            "recorded_kinds": required_kinds,
            "evidence": records,
        }
