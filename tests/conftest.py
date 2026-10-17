from pathlib import Path

import pytest

from ai_team_control_plane.boundary import BoundaryGuard
from ai_team_control_plane.orchestrator import TaskOrchestrator
from ai_team_control_plane.registry import ProjectRegistry
from ai_team_control_plane.store import DurableStore

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def system(tmp_path):
    registry = ProjectRegistry(ROOT / "config" / "projects.json")
    store = DurableStore(tmp_path / "test-control-plane.sqlite3")
    guard = BoundaryGuard(registry)
    return registry, store, guard, TaskOrchestrator(store, guard)
