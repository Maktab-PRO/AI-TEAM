from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath

from .models import BoundaryViolation, Project
from .registry import ProjectRegistry


_RESERVED_EXACT = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}
_RESERVED_PARTS = {"secrets", "credentials", "private_keys"}
_RESERVED_SUFFIXES = (".pem", ".p12", ".pfx", ".key")
_DANGEROUS_ACTIONS = {
    "deploy_production",
    "merge_pull_request",
    "modify_production_database",
    "restore_database",
    "rotate_secrets",
    "delete_repository",
    "change_billing",
}


class BoundaryGuard:
    """Fail-closed enforcement for project/repository/branch/path scope."""

    def __init__(self, registry: ProjectRegistry):
        self.registry = registry

    def _project(self, project_id: str) -> Project:
        project = self.registry.get(project_id)
        if project is None:
            raise BoundaryViolation(f"Unknown project '{project_id}'. Register it before scheduling work.")
        if project.status != "active":
            raise BoundaryViolation(
                f"Project '{project_id}' is '{project.status}', not active; writes and task execution are blocked."
            )
        if not project.repository:
            raise BoundaryViolation(f"Project '{project_id}' has no repository mapped; fail-closed.")
        return project

    def authorize(
        self,
        *,
        project_id: str,
        repository: str,
        branch: str,
        paths: list[str] | tuple[str, ...] = (),
        action: str = "write",
    ) -> Project:
        project = self._project(project_id)
        if repository.strip().lower() != project.repository.lower():
            raise BoundaryViolation(
                f"Boundary violation: project '{project_id}' is mapped to '{project.repository}', "
                f"not '{repository}'. No fallback repository matching is allowed."
            )
        if action in _DANGEROUS_ACTIONS:
            raise BoundaryViolation(
                f"Action '{action}' is external/destructive and is not enabled in the control-plane MVP. "
                "It needs a separate approval and execution adapter."
            )
        if action not in {"read", "plan", "write"}:
            raise BoundaryViolation(f"Unknown action '{action}' is denied by default.")

        branch = branch.strip()
        if (
            not branch
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,248}", branch)
            or ".." in branch.split("/")
            or "//" in branch
            or branch.endswith(("/", ".", ".lock"))
        ):
            raise BoundaryViolation(f"Invalid or unsafe branch name: {branch!r}")
        if action == "write":
            if branch in project.protected_branches:
                raise BoundaryViolation(
                    f"Direct writes to protected branch '{branch}' are forbidden. Use a feature branch and reviewed PR."
                )
            if not any(branch.startswith(prefix) for prefix in project.write_branch_prefixes):
                raise BoundaryViolation(
                    f"Branch '{branch}' is outside allowed write prefixes {project.write_branch_prefixes!r}."
                )

        for raw_path in paths:
            normalized = raw_path.replace("\\", "/").strip()
            path = PurePosixPath(normalized)
            if (
                not normalized
                or "\x00" in normalized
                or path.is_absolute()
                or normalized.startswith("//")
                or re.match(r"^[A-Za-z]:", normalized)
                or normalized.startswith("~/")
                or any(part in {"..", "."} for part in path.parts)
            ):
                raise BoundaryViolation(f"Unsafe repository-relative path: {raw_path!r}")
            lowered_parts = {part.lower() for part in path.parts}
            lowered = normalized.lower()
            if (
                lowered in _RESERVED_EXACT
                or PurePosixPath(lowered).name in _RESERVED_EXACT
                or any(part in _RESERVED_PARTS for part in lowered_parts)
                or lowered.endswith(_RESERVED_SUFFIXES)
                or lowered == ".git"
                or lowered.startswith(".git/")
                or lowered == ".github/workflows"
                or lowered.startswith(".github/workflows/")
                or re.search(r"(^|/)\.env(?:\.|$)", lowered)
            ):
                raise BoundaryViolation(
                    f"Sensitive or high-impact path '{raw_path}' requires a separately approved workflow."
                )
            if not any(fnmatch.fnmatchcase(normalized, pattern) for pattern in project.allowed_paths):
                raise BoundaryViolation(
                    f"Path '{raw_path}' is outside allowed path patterns for project '{project_id}'."
                )
        return project
