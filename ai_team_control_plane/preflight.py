from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .registry import ProjectRegistry
from .rate_limiting import RateLimitConfigurationError, get_rate_limit_config

ROOT = Path(__file__).resolve().parents[1]
_ALLOWED_ROLES = {"platform_admin", "project_admin", "worker", "reviewer", "read_only"}
_DB_TLS_MODES = {"require", "verify-ca", "verify-full"}


def _check(name: str, passed: bool, detail: str) -> dict[str, str]:
    return {"name": name, "status": "pass" if passed else "fail", "detail": detail}


def _principal_is_valid(policy: object, active_project_ids: set[str]) -> bool:
    if not isinstance(policy, dict):
        return False
    actor = policy.get("actor")
    role = policy.get("role")
    projects = policy.get("projects")
    if (
        not isinstance(actor, str)
        or not actor
        or actor != actor.strip()
        or len(actor) > 120
        or any(ord(ch) < 32 for ch in actor)
    ):
        return False
    if not isinstance(role, str) or role not in _ALLOWED_ROLES:
        return False
    if (
        not isinstance(projects, list)
        or not projects
        or any(not isinstance(project, str) or not project or project != project.strip() for project in projects)
        or len(projects) != len(set(projects))
    ):
        return False
    if role == "platform_admin":
        return projects == ["*"]
    return "*" not in projects and all(project in active_project_ids for project in projects)


def _valid_project_scopes(env: dict[str, str], active_project_ids: set[str]) -> bool:
    policies: list[dict] = []
    token_map_raw = env.get("AI_TEAM_API_TOKENS", "").strip()
    if token_map_raw:
        try:
            token_map = json.loads(token_map_raw)
        except json.JSONDecodeError:
            return False
        if not isinstance(token_map, dict) or not token_map:
            return False
        if any(not isinstance(policy, dict) for policy in token_map.values()):
            return False
        policies.extend(token_map.values())

    single_token = env.get("AI_TEAM_API_TOKEN", "").strip()
    if single_token:
        policies.append({
            "role": env.get("AI_TEAM_API_ROLE", "").strip(),
            "projects": [part.strip() for part in env.get("AI_TEAM_API_PROJECTS", "").split(",") if part.strip()],
        })

    if not policies:
        return False
    for policy in policies:
        role = policy.get("role")
        projects = policy.get("projects")
        if not isinstance(role, str) or role not in _ALLOWED_ROLES:
            return False
        if (
            not isinstance(projects, list)
            or not projects
            or any(not isinstance(project, str) or not project or project != project.strip() for project in projects)
            or len(projects) != len(set(projects))
        ):
            return False
        if role == "platform_admin":
            if projects != ["*"]:
                return False
        elif "*" in projects or not all(project in active_project_ids for project in projects):
            return False
    return True


def _valid_api_credentials(env: dict[str, str], active_project_ids: set[str]) -> bool:
    credentials: dict[str, dict] = {}
    token_map_raw = env.get("AI_TEAM_API_TOKENS", "").strip()
    if token_map_raw:
        try:
            token_map = json.loads(token_map_raw)
        except json.JSONDecodeError:
            return False
        if not isinstance(token_map, dict) or not token_map:
            return False
        for token, policy in token_map.items():
            if (
                not isinstance(token, str)
                or token != token.strip()
                or len(token) < 32
                or not _principal_is_valid(policy, active_project_ids)
            ):
                return False
            credentials[token] = policy

    single_token = env.get("AI_TEAM_API_TOKEN", "").strip()
    if single_token:
        if len(single_token) < 32:
            return False
        projects = [part.strip() for part in env.get("AI_TEAM_API_PROJECTS", "").split(",") if part.strip()]
        policy = {
            "actor": env.get("AI_TEAM_API_ACTOR", "api-token").strip(),
            "role": env.get("AI_TEAM_API_ROLE", "").strip(),
            "projects": projects,
        }
        if not _principal_is_valid(policy, active_project_ids):
            return False
        if single_token in credentials and credentials[single_token] != policy:
            return False
        credentials[single_token] = policy

    return bool(credentials)


def _valid_database_url(value: str) -> bool:
    if not value or value != value.strip():
        return False
    try:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query, keep_blank_values=True)
        # Trigger validation of malformed ports without disclosing the URL.
        port = parsed.port
    except ValueError:
        return False
    ssl_modes = query.get("sslmode", [])
    return (
        parsed.scheme in {"postgres", "postgresql"}
        and bool(parsed.hostname)
        and parsed.path not in {"", "/"}
        and port != 0
        and len(ssl_modes) == 1
        and ssl_modes[0].lower() in _DB_TLS_MODES
    )


def _valid_workflow_allowlist(raw: str, active_repositories: set[str]) -> bool:
    if not raw:
        return False
    try:
        targets = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(targets, dict) or not targets:
        return False
    for repository, paths in targets.items():
        parts = repository.split("/") if isinstance(repository, str) else []
        if len(parts) != 2 or any(not part or part != part.strip() for part in parts):
            return False
        if repository.casefold() not in active_repositories:
            return False
        if not isinstance(paths, list) or not paths:
            return False
        for path in paths:
            if (
                not isinstance(path, str)
                or not path.startswith(".github/workflows/")
                or path.endswith(("/", "."))
                or ".." in path.split("/")
                or "*" in path
                or not path.endswith((".yml", ".yaml"))
            ):
                return False
    return True


def _valid_render_allowlist(raw: str, active_project_ids: set[str]) -> bool:
    if not raw:
        return True
    try:
        targets = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(targets, dict):
        return False
    for project_id, target in targets.items():
        if project_id not in active_project_ids or not isinstance(target, dict):
            return False
        url = target.get("url")
        service = target.get("expected_service")
        status = target.get("expected_status", "ok")
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            port = parsed.port if parsed else None
        except ValueError:
            return False
        if (
            parsed is None
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path != "/health"
            or port not in (None, 443)
        ):
            return False
        host = parsed.hostname.rstrip(".").casefold()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return False
        if host in {"localhost", "localhost.localdomain"} or host.endswith(
            (".localhost", ".local", ".internal", ".test", ".invalid")
        ):
            return False
        if not isinstance(service, str) or not service or service != service.strip() or len(service) > 120:
            return False
        if not isinstance(status, str) or not status or status != status.strip() or len(status) > 80:
            return False
    return True


def build_preflight_report(environment: dict[str, str] | None = None) -> dict:
    """Validate local production configuration shape without connecting to services.

    This is a configuration preflight only; it never approves or performs a
    production release and never includes secret values in the report.
    """
    env = dict(os.environ if environment is None else environment)
    checks: list[dict[str, str]] = []

    app_env = env.get("APP_ENV", "").strip().lower()
    checks.append(_check(
        "app_environment",
        app_env == "production",
        "APP_ENV is set to production." if app_env == "production" else "APP_ENV must equal 'production' for this preflight.",
    ))

    try:
        get_rate_limit_config(env)
        rate_limit_valid = True
    except RateLimitConfigurationError:
        rate_limit_valid = False
    checks.append(_check(
        "rate_limit_config",
        rate_limit_valid,
        "A supported per-minute limit and a 32+ character HMAC key are configured."
        if rate_limit_valid
        else "Configure AI_TEAM_RATE_LIMIT_PER_MINUTE (1–10000) and a 32+ character AI_TEAM_RATE_LIMIT_HASH_KEY in the secret environment.",
    ))

    registry_path = Path(env.get("AI_TEAM_REGISTRY_PATH", str(ROOT / "config" / "projects.json")))
    try:
        registry = ProjectRegistry(registry_path)
        active_projects = [project for project in registry.all() if project.status == "active"]
        active_project_ids = {project.id for project in active_projects}
        active_repositories = {project.repository.casefold() for project in active_projects if project.repository}
        registry_valid = bool(active_projects) and "ai-team-control-plane" in active_project_ids
    except (OSError, ValueError, TypeError):
        active_project_ids = set()
        active_repositories = set()
        registry_valid = False
    checks.append(_check(
        "project_registry",
        registry_valid,
        "Project registry loaded and contains active projects including AI TEAM."
        if registry_valid
        else "Project registry is invalid or missing the active AI TEAM control-plane mapping.",
    ))

    db_valid = _valid_database_url(env.get("DATABASE_URL", ""))
    checks.append(_check(
        "database_url",
        db_valid,
        "DATABASE_URL has a PostgreSQL URL and explicit TLS mode."
        if db_valid
        else "DATABASE_URL must be a PostgreSQL URL with a host, database path, and sslmode=require, verify-ca, or verify-full.",
    ))

    scope_valid = _valid_project_scopes(env, active_project_ids)
    checks.append(_check(
        "project_scope",
        scope_valid,
        "Token roles are paired with explicit registered active project scopes."
        if scope_valid
        else "API tokens need valid explicit project scopes; only platform_admin may use the scope '*'.",
    ))

    api_valid = _valid_api_credentials(env, active_project_ids)
    checks.append(_check(
        "api_token_policy",
        api_valid,
        "At least one strong token has a valid fixed actor, role, and active project scope."
        if api_valid
        else "Configure AI_TEAM_API_TOKEN or AI_TEAM_API_TOKENS with a 32+ character token and valid explicit actor/role/project scope.",
    ))

    github_token = env.get("AI_TEAM_GITHUB_TOKEN", "").strip()
    github_token_valid = len(github_token) >= 20
    checks.append(_check(
        "github_token",
        github_token_valid,
        "A GitHub token value is present with plausible length; live authentication and permissions were not tested."
        if github_token_valid
        else "AI_TEAM_GITHUB_TOKEN must be configured in the secret manager; live token validity is not checked here.",
    ))

    workflow_valid = _valid_workflow_allowlist(
        env.get("AI_TEAM_GITHUB_WORKFLOW_ALLOWLIST", "").strip(),
        active_repositories,
    )
    checks.append(_check(
        "github_workflow_allowlist",
        workflow_valid,
        "Workflow allowlist is valid JSON containing exact registered repositories and workflow YAML paths."
        if workflow_valid
        else "AI_TEAM_GITHUB_WORKFLOW_ALLOWLIST must map registered owner/repository names to exact .github/workflows/*.yml or *.yaml paths.",
    ))

    redis_url = env.get("REDIS_URL", "").strip()
    if not redis_url:
        redis_valid = True
        redis_detail = "Redis publisher is not configured; event delivery remains disabled."
    else:
        try:
            parsed_redis = urlsplit(redis_url)
            redis_valid = parsed_redis.scheme == "rediss" and bool(parsed_redis.hostname) and parsed_redis.port != 0
        except ValueError:
            redis_valid = False
        redis_detail = (
            "Configured Redis URL uses TLS and includes a host; no connection was attempted."
            if redis_valid
            else "REDIS_URL, when configured in production, must use rediss:// with a valid host and port."
        )
    checks.append(_check("redis_url", redis_valid, redis_detail))

    render_raw = env.get("AI_TEAM_RENDER_HEALTH_ALLOWLIST", "").strip()
    render_valid = _valid_render_allowlist(render_raw, active_project_ids)
    render_detail = (
        "Render health allowlist syntax is valid; no endpoint was contacted."
        if render_valid and render_raw
        else "No Render health targets configured; live health evidence stays disabled."
        if render_valid
        else "AI_TEAM_RENDER_HEALTH_ALLOWLIST must contain only registered project IDs with exact HTTPS /health URLs and service/status markers."
    )
    checks.append(_check("render_health_allowlist", render_valid, render_detail))

    configuration_valid = all(item["status"] == "pass" for item in checks)
    return {
        "configuration_valid": configuration_valid,
        "production_release_approved": False,
        "secrets_redacted": True,
        "checks": checks,
        "note": (
            "This configuration preflight does not verify live services or credentials, external identity, "
            "monitoring, backups, proxy/client-IP behavior, or release authorization. It never approves or deploys a production release."
        ),
    }
