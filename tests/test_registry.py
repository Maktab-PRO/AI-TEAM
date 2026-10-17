import json
from pathlib import Path

import pytest

from ai_team_control_plane.registry import ProjectRegistry


def test_registry_rejects_case_insensitive_duplicate_repository_mapping(tmp_path):
    source = Path(__file__).resolve().parents[1] / "config" / "projects.json"
    data = json.loads(source.read_text(encoding="utf-8"))
    data["projects"][1]["repository"] = data["projects"][0]["repository"].swapcase()
    registry_path = tmp_path / "projects.json"
    registry_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="case-insensitive"):
        ProjectRegistry(registry_path)


def test_registry_rejects_repository_mappings_with_whitespace(tmp_path):
    source = Path(__file__).resolve().parents[1] / "config" / "projects.json"
    data = json.loads(source.read_text(encoding="utf-8"))
    data["projects"][0]["repository"] = " Maktab-PRO/Axsikent-IT "
    registry_path = tmp_path / "projects.json"
    registry_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="without whitespace"):
        ProjectRegistry(registry_path)
