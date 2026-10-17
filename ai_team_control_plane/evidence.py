from __future__ import annotations

from urllib.parse import urlparse

from .models import EvidenceRequirement, GateResult


# A requirement is satisfied only by a passing, trusted attestation from an approved source.
# The public API deliberately cannot set `trusted=True`.
REQUIREMENTS: dict[str, tuple[EvidenceRequirement, ...]] = {
    "implementation": (
        EvidenceRequirement("tests_passed", "Automated tests passed", ("test_runner", "github_actions")),
        EvidenceRequirement("ci_passed", "Required CI checks passed", ("github_actions", "ci_provider")),
        EvidenceRequirement("review_approved", "Independent code review approved", ("github_review", "human_review")),
    ),
    "bugfix": (
        EvidenceRequirement("regression_test_passed", "Regression test passed", ("test_runner", "github_actions")),
        EvidenceRequirement("tests_passed", "Automated tests passed", ("test_runner", "github_actions")),
        EvidenceRequirement("ci_passed", "Required CI checks passed", ("github_actions", "ci_provider")),
        EvidenceRequirement("review_approved", "Independent code review approved", ("github_review", "human_review")),
    ),
    "deployment": (
        EvidenceRequirement("tests_passed", "Automated tests passed", ("test_runner", "github_actions")),
        EvidenceRequirement("ci_passed", "Required CI checks passed", ("github_actions", "ci_provider")),
        EvidenceRequirement("health_check_passed", "Post-deployment health check passed", ("render_health", "monitoring")),
        EvidenceRequirement("rollback_plan", "Rollback plan is documented", ("human_review", "documentation_check")),
    ),
    "security": (
        EvidenceRequirement("security_scan_passed", "Security scan passed", ("security_scanner", "github_actions")),
        EvidenceRequirement("tests_passed", "Automated tests passed", ("test_runner", "github_actions")),
        EvidenceRequirement("review_approved", "Independent security review approved", ("github_review", "human_review")),
    ),
    "documentation": (
        EvidenceRequirement("doc_review_approved", "Documentation reviewed", ("human_review", "documentation_check")),
    ),
}

_ALLOWED_SCHEMES = {"https", "github", "ci", "test"}


def validate_evidence_uri(uri: str) -> None:
    parsed = urlparse(uri)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError("Evidence URI must use one of: https, github, ci, test.")
    if parsed.scheme == "https" and not parsed.netloc:
        raise ValueError("HTTPS evidence must include a host.")
    if parsed.scheme in {"github", "ci", "test"} and not parsed.path and not parsed.netloc:
        raise ValueError("Evidence URI must identify a specific run, review, test, or artifact.")
    lowered = uri.lower()
    if any(marker in lowered for marker in ("example.invalid", "todo", "placeholder", "replace-me")):
        raise ValueError("Placeholder references cannot be used as evidence.")


class EvidenceGate:
    def __init__(self, store):
        self.store = store

    def check(self, task_id: str) -> GateResult:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"Unknown task ID: {task_id}")
        requirements = REQUIREMENTS.get(task["task_type"])
        if requirements is None:
            return GateResult(
                passed=False,
                task_id=task_id,
                missing=(f"Unsupported task type: {task['task_type']}",),
                failed=(),
                untrusted=(),
                summary="Evidence gate denied: task type has no approved evidence policy.",
            )
        evidence = self.store.list_evidence(task_id)
        missing: list[str] = []
        failed: list[str] = []
        untrusted: list[str] = []
        for req in requirements:
            candidates = [item for item in evidence if item["kind"] == req.kind]
            if not candidates:
                missing.append(f"{req.kind}: {req.description}")
                continue
            accepted = [item for item in candidates if item["source"] in req.accepted_sources and item["trusted"]]
            if accepted:
                # Evidence is append-only: the most recent trusted assertion for a requirement wins.
                latest = max(accepted, key=lambda item: (item["created_at"], item["id"]))
                if latest["status"] == "passed":
                    continue
                if latest["status"] == "failed":
                    failed.append(f"{req.kind}: latest trusted evidence failed")
                else:
                    missing.append(f"{req.kind}: latest trusted evidence is informational, not a pass")
                continue
            has_untrusted = any(not item["trusted"] for item in candidates)
            if has_untrusted:
                untrusted.append(f"{req.kind}: records exist but were not verified by a trusted adapter")
            else:
                missing.append(f"{req.kind}: no trusted passing evidence from an accepted source")
        ok = not missing and not failed and not untrusted
        summary = (
            "Evidence gate passed; task may be marked VERIFIED."
            if ok
            else "Evidence gate denied. Missing/failed/untrusted evidence must be resolved; a status label alone is not proof."
        )
        return GateResult(task_id=task_id, passed=ok, missing=tuple(missing), failed=tuple(failed), untrusted=tuple(untrusted), summary=summary)
