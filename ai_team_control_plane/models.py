from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    EVIDENCE_PENDING = "evidence_pending"
    VERIFIED = "verified"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    name: str
    repository: str | None
    status: str
    default_branch: str = "main"
    protected_branches: tuple[str, ...] = ("main", "master", "production")
    write_branch_prefixes: tuple[str, ...] = ("ai-team/", "codex/", "feature/", "fix/")
    allowed_paths: tuple[str, ...] = ("**",)
    description: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Project":
        return cls(
            id=str(value["id"]),
            name=str(value["name"]),
            repository=value.get("repository"),
            status=str(value.get("status", "active")),
            default_branch=str(value.get("default_branch", "main")),
            protected_branches=tuple(value.get("protected_branches", ["main", "master", "production"])),
            write_branch_prefixes=tuple(value.get("write_branch_prefixes", ["ai-team/", "codex/", "feature/", "fix/"])),
            allowed_paths=tuple(value.get("allowed_paths", ["**"])),
            description=str(value.get("description", "")),
        )


@dataclass(frozen=True, slots=True)
class EvidenceRequirement:
    kind: str
    description: str
    accepted_sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GateResult:
    passed: bool
    task_id: str
    missing: tuple[str, ...]
    failed: tuple[str, ...]
    untrusted: tuple[str, ...]
    summary: str


class ControlPlaneError(Exception):
    """Base exception for an expected, user-correctable control-plane failure."""


class BoundaryViolation(ControlPlaneError):
    pass


class StateConflict(ControlPlaneError):
    pass


class IdempotencyConflict(ControlPlaneError):
    pass


class EvidenceRejected(ControlPlaneError):
    pass
