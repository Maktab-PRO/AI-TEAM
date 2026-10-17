from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .models import Project


class ProjectRegistry:
    """Read-only project registry. Registry changes require a reviewed config change."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._projects: dict[str, Project] = {}
        self.reload()

    def reload(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"Project registry file not found: {self.path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"Project registry is not valid JSON: {exc}") from exc

        if raw.get("schema_version") != self.SCHEMA_VERSION:
            raise ValueError(f"Unsupported project registry schema: {raw.get('schema_version')!r}")
        entries = raw.get("projects")
        if not isinstance(entries, list):
            raise ValueError("Project registry must contain a 'projects' list")

        built: dict[str, Project] = {}
        repositories: dict[str, str] = {}
        for entry in entries:
            project = Project.from_dict(entry)
            if not project.id or project.id in built:
                raise ValueError(f"Project IDs must be non-empty and unique: {project.id!r}")
            if project.status not in {"active", "setup_required", "quarantined", "disabled"}:
                raise ValueError(f"Invalid status for project {project.id}: {project.status}")
            if project.status == "active" and not project.repository:
                raise ValueError(f"Active project {project.id} must have an exact repository")
            if project.repository:
                repository = project.repository
                parts = repository.split("/")
                if (
                    repository != repository.strip()
                    or len(parts) != 2
                    or any(not part or part != part.strip() for part in parts)
                ):
                    raise ValueError(f"Repository mapping must be an exact owner/name pair without whitespace: {repository!r}")
                repository_key = repository.casefold()
                if repository_key in repositories:
                    raise ValueError(
                        f"Repository {repository!r} is mapped to both "
                        f"{repositories[repository_key]!r} and {project.id!r} (repository names are case-insensitive)"
                    )
                repositories[repository_key] = project.id
            built[project.id] = project
        self._projects = built

    def get(self, project_id: str) -> Project | None:
        return self._projects.get(project_id)

    def require(self, project_id: str) -> Project:
        project = self.get(project_id)
        if project is None:
            raise KeyError(f"Unknown project ID: {project_id}")
        return project

    def all(self, include_quarantined: bool = True) -> list[Project]:
        values: Iterable[Project] = self._projects.values()
        if not include_quarantined:
            values = (p for p in values if p.status == "active")
        return sorted(values, key=lambda p: p.id)

    def snapshot(self) -> dict:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "projects": [
                {
                    "id": p.id,
                    "name": p.name,
                    "repository": p.repository,
                    "status": p.status,
                    "default_branch": p.default_branch,
                    "protected_branches": list(p.protected_branches),
                    "write_branch_prefixes": list(p.write_branch_prefixes),
                    "allowed_paths": list(p.allowed_paths),
                    "description": p.description,
                }
                for p in self.all()
            ],
        }
