from __future__ import annotations

from fastapi.testclient import TestClient

from ai_team_control_plane import api


def configure_test_api(monkeypatch, system):
    registry, store, guard, orchestrator = system
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AI_TEAM_RATE_LIMIT_PER_MINUTE", "2")
    monkeypatch.setenv("AI_TEAM_RATE_LIMIT_HASH_KEY", "test-hash-key-" + "h" * 40)
    monkeypatch.delenv("AI_TEAM_API_TOKEN", raising=False)
    monkeypatch.delenv("AI_TEAM_API_TOKENS", raising=False)
    monkeypatch.setattr(api, "registry", registry)
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api, "guard", guard)
    monkeypatch.setattr(api, "orchestrator", orchestrator)
    return TestClient(api.app), store


def test_rate_limit_blocks_the_third_request_and_does_not_store_raw_client_identity(
    monkeypatch, system
):
    client, store = configure_test_api(monkeypatch, system)

    first = client.get("/projects", headers={"X-Actor": "rate-limit-test"})
    second = client.get("/projects", headers={"X-Actor": "rate-limit-test"})
    third = client.get("/projects", headers={"X-Actor": "rate-limit-test"})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert third.status_code == 429, third.text
    assert third.headers.get("Retry-After", "").isdigit()
    assert third.json() == {"detail": "Rate limit exceeded."}

    with store.connection() as conn:
        rows = conn.execute("SELECT bucket_key FROM request_rate_limits").fetchall()
    keys = [row["bucket_key"] for row in rows]
    assert keys
    assert all("testclient" not in key for key in keys)


def test_production_requests_fail_closed_if_rate_limit_hash_key_is_missing(
    monkeypatch, system
):
    client, _ = configure_test_api(monkeypatch, system)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("AI_TEAM_RATE_LIMIT_HASH_KEY", raising=False)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "Production rate limiting is not configured."}
