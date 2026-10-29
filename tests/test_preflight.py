from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONFIG_KEYS = (
    "APP_ENV",
    "DATABASE_URL",
    "REDIS_URL",
    "AI_TEAM_API_TOKEN",
    "AI_TEAM_API_TOKENS",
    "AI_TEAM_API_ACTOR",
    "AI_TEAM_API_ROLE",
    "AI_TEAM_API_PROJECTS",
    "AI_TEAM_GITHUB_TOKEN",
    "AI_TEAM_GITHUB_WORKFLOW_ALLOWLIST",
    "AI_TEAM_RENDER_HEALTH_ALLOWLIST",
    "AI_TEAM_RATE_LIMIT_PER_MINUTE",
    "AI_TEAM_RATE_LIMIT_HASH_KEY",
)


def run_preflight(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> subprocess.CompletedProcess[str]:
    for key in CONFIG_KEYS:
        monkeypatch.delenv(key, raising=False)
    env = os.environ.copy()
    env.update(overrides)
    return subprocess.run(
        [sys.executable, "-m", "ai_team_control_plane.cli", "preflight"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def valid_production_environment() -> dict[str, str]:
    return {
        "APP_ENV": "production",
        "DATABASE_URL": "postgresql://ai_team:synthetic-only@db.example.invalid:5432/ai_team?sslmode=require",
        "AI_TEAM_API_TOKEN": "synthetic-preflight-token-" + "x" * 32,
        "AI_TEAM_API_ACTOR": "preflight-test-actor",
        "AI_TEAM_API_ROLE": "platform_admin",
        "AI_TEAM_API_PROJECTS": "*",
        "AI_TEAM_GITHUB_TOKEN": "synthetic-github-token-" + "x" * 32,
        "AI_TEAM_GITHUB_WORKFLOW_ALLOWLIST": json.dumps(
            {"Maktab-PRO/AI-TEAM": [".github/workflows/ci.yml"]}
        ),
        "AI_TEAM_RATE_LIMIT_PER_MINUTE": "120",
        "AI_TEAM_RATE_LIMIT_HASH_KEY": "synthetic-rate-limit-hash-key-" + "r" * 32,
    }


def test_preflight_reports_missing_production_requirements_without_leaking_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_preflight(
        monkeypatch,
        APP_ENV="production",
        AI_TEAM_API_TOKEN="do-not-print-this-secret-value-" + "x" * 32,
    )

    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["configuration_valid"] is False
    assert report["production_release_approved"] is False
    assert "DATABASE_URL" in json.dumps(report)
    assert "do-not-print-this-secret-value" not in result.stdout
    assert "do-not-print-this-secret-value" not in result.stderr


def test_preflight_accepts_well_formed_config_but_never_approves_production_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_preflight(monkeypatch, **valid_production_environment())

    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads(result.stdout)
    assert report["configuration_valid"] is True
    assert report["production_release_approved"] is False
    assert all(item["status"] == "pass" for item in report["checks"])
    assert "does not verify live services or credentials" in report["note"]


def test_preflight_rejects_unknown_project_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    env = valid_production_environment()
    env["AI_TEAM_API_ROLE"] = "worker"
    env["AI_TEAM_API_PROJECTS"] = "unregistered-project-id"

    result = run_preflight(monkeypatch, **env)

    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["configuration_valid"] is False
    assert any("project" in item["name"].lower() and item["status"] == "fail" for item in report["checks"])


def test_preflight_rejects_malformed_github_workflow_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = valid_production_environment()
    env["AI_TEAM_GITHUB_WORKFLOW_ALLOWLIST"] = '{"Maktab-PRO/AI-TEAM":["README.md"]}'

    result = run_preflight(monkeypatch, **env)

    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["configuration_valid"] is False
    assert any(item["name"] == "github_workflow_allowlist" and item["status"] == "fail" for item in report["checks"])


def test_preflight_rejects_non_tls_database_url_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = valid_production_environment()
    secret_url = "postgresql://sensitive-user:sensitive-password@db.example.invalid:5432/ai_team"
    env["DATABASE_URL"] = secret_url

    result = run_preflight(monkeypatch, **env)

    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["configuration_valid"] is False
    assert "sensitive-password" not in result.stdout
    assert "sensitive-user" not in result.stdout


def test_preflight_rejects_duplicate_database_sslmode_even_when_last_value_is_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = valid_production_environment()
    env["DATABASE_URL"] = (
        "postgresql://ai_team:synthetic-only@db.example.invalid:5432/ai_team"
        "?sslmode=disable&sslmode=require"
    )

    result = run_preflight(monkeypatch, **env)

    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["configuration_valid"] is False
    assert any(
        item["name"] == "database_url" and item["status"] == "fail"
        for item in report["checks"]
    )


def test_preflight_rejects_malformed_role_policy_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = valid_production_environment()
    env["AI_TEAM_API_TOKENS"] = json.dumps({
        "synthetic-map-token-" + "z" * 40: {
            "actor": "malformed-role",
            "role": [],
            "projects": ["ai-team-control-plane"],
        }
    })

    result = run_preflight(monkeypatch, **env)

    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["configuration_valid"] is False
    assert "Traceback" not in result.stderr
