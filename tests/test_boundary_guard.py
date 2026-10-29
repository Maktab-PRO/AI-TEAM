import pytest

from ai_team_control_plane.models import BoundaryViolation


def test_correct_repository_and_feature_branch_are_allowed(system):
    _, _, guard, _ = system
    project = guard.authorize(
        project_id="axsikent-it",
        repository="Maktab-PRO/Axsikent-IT",
        branch="ai-team/registry-bootstrap",
        paths=["docs/control-plane.md"],
        action="write",
    )
    assert project.id == "axsikent-it"


def test_wrong_repository_is_denied(system):
    _, _, guard, _ = system
    with pytest.raises(BoundaryViolation, match="Boundary violation"):
        guard.authorize(
            project_id="axsikent-it",
            repository="Maktab-PRO/TECHNIKUM-DIGITAL-PLATFORM",
            branch="ai-team/wrong-project",
            action="write",
        )


def test_direct_write_to_main_is_denied(system):
    _, _, guard, _ = system
    with pytest.raises(BoundaryViolation, match="protected branch"):
        guard.authorize(
            project_id="axsikent-it",
            repository="Maktab-PRO/Axsikent-IT",
            branch="main",
            action="write",
        )


def test_path_traversal_and_secret_paths_are_denied(system):
    _, _, guard, _ = system
    base = {"project_id": "axsikent-it", "repository": "Maktab-PRO/Axsikent-IT", "branch": "ai-team/test", "action": "write"}
    with pytest.raises(BoundaryViolation, match="Unsafe"):
        guard.authorize(**base, paths=["frontend/../../other-project/file.py"])
    with pytest.raises(BoundaryViolation, match="Sensitive"):
        guard.authorize(**base, paths=["backend/.env.production"])


def test_quarantined_repository_cannot_be_scheduled(system):
    _, _, guard, _ = system
    with pytest.raises(BoundaryViolation, match="quarantined"):
        guard.authorize(
            project_id="unclassified-repository-4",
            repository="Maktab-PRO/aqlli-mahalla-temirqishloq",
            branch="ai-team/inspect",
            action="write",
        )


def test_ai_team_repository_mapping_allows_only_the_configured_repo(system):
    _, _, guard, _ = system
    project = guard.authorize(
        project_id="ai-team-control-plane",
        repository="Maktab-PRO/AI-TEAM",
        branch="ai-team/bootstrap",
        action="write",
    )
    assert project.repository == "Maktab-PRO/AI-TEAM"

    with pytest.raises(BoundaryViolation, match="mapped to"):
        guard.authorize(
            project_id="ai-team-control-plane",
            repository="Maktab-PRO/ai-team-control-plane",
            branch="ai-team/bootstrap",
            action="write",
        )


def test_production_action_fails_closed(system):
    _, _, guard, _ = system
    with pytest.raises(BoundaryViolation, match="separate approval"):
        guard.authorize(
            project_id="axsikent-it",
            repository="Maktab-PRO/Axsikent-IT",
            branch="ai-team/deploy",
            action="deploy_production",
        )
