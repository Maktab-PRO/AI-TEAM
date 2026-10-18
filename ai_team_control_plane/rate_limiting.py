from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Mapping


DEFAULT_REQUESTS_PER_MINUTE = 120
MAX_REQUESTS_PER_MINUTE = 10_000
MIN_HASH_KEY_LENGTH = 32
_DEVELOPMENT_ONLY_HASH_KEY = "ai-team-control-plane-development-only-hmac-key"


class RateLimitConfigurationError(ValueError):
    """The service cannot safely enable request throttling with its current config."""


def get_rate_limit_config(environment: Mapping[str, str] | None = None) -> tuple[int, bytes]:
    """Return the limit and HMAC key without logging or returning secret text."""
    env = os.environ if environment is None else environment
    app_environment = env.get("APP_ENV", "development").strip().lower()
    raw_limit = env.get("AI_TEAM_RATE_LIMIT_PER_MINUTE", "").strip()
    if app_environment == "production" and not raw_limit:
        raise RateLimitConfigurationError("Production rate-limit settings are missing.")
    if not raw_limit:
        raw_limit = str(DEFAULT_REQUESTS_PER_MINUTE)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as exc:
        raise RateLimitConfigurationError("Rate-limit configuration is invalid.") from exc
    if not 1 <= limit <= MAX_REQUESTS_PER_MINUTE:
        raise RateLimitConfigurationError("Rate-limit configuration is outside the supported range.")

    hash_key = env.get("AI_TEAM_RATE_LIMIT_HASH_KEY", "").strip()
    if app_environment == "production" and len(hash_key) < MIN_HASH_KEY_LENGTH:
        raise RateLimitConfigurationError("Production rate limiting is not configured.")
    if hash_key and len(hash_key) < MIN_HASH_KEY_LENGTH:
        raise RateLimitConfigurationError("Rate-limit hash key is too short.")
    if not hash_key:
        hash_key = _DEVELOPMENT_ONLY_HASH_KEY
    return limit, hash_key.encode("utf-8")


def rate_limit_bucket_key(secret: bytes, category: str, identity: str) -> str:
    """Create a stable pseudonymous key; raw IPs and bearer tokens are never stored."""
    if category not in {"client", "token"}:
        raise ValueError("Unsupported rate-limit identity category.")
    digest = hmac.new(
        secret,
        f"{category}\0{identity}".encode("utf-8", errors="replace"),
        hashlib.sha256,
    ).hexdigest()
    return f"{category}:{digest}"
