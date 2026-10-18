from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from ai_team_control_plane.store import DurableStore


def test_fixed_window_limit_and_reset_are_deterministic(tmp_path):
    store = DurableStore(tmp_path / "rate-limit.sqlite3")

    first = store.consume_rate_limit("client:fixture", limit=2, window_seconds=60, now=1201)
    second = store.consume_rate_limit("client:fixture", limit=2, window_seconds=60, now=1202)
    third = store.consume_rate_limit("client:fixture", limit=2, window_seconds=60, now=1203)
    next_window = store.consume_rate_limit("client:fixture", limit=2, window_seconds=60, now=1260)

    assert first == {"allowed": True, "count": 1, "reset_at": 1260}
    assert second == {"allowed": True, "count": 2, "reset_at": 1260}
    assert third == {"allowed": False, "count": 3, "reset_at": 1260}
    assert next_window == {"allowed": True, "count": 1, "reset_at": 1320}


def test_sqlite_counter_is_atomic_across_concurrent_connections(tmp_path):
    store = DurableStore(tmp_path / "rate-limit-concurrent.sqlite3")
    request_count = 32
    limit = 7

    def consume(_index: int) -> dict:
        return store.consume_rate_limit(
            "token:concurrency-fixture",
            limit=limit,
            window_seconds=60,
            now=2401,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(consume, range(request_count)))

    assert sum(result["allowed"] for result in outcomes) == limit
    assert sorted(result["count"] for result in outcomes) == list(range(1, request_count + 1))
    assert {result["reset_at"] for result in outcomes} == {2460}


@pytest.mark.parametrize(
    "bucket_key,limit,window_seconds",
    [("", 1, 60), ("x" * 161, 1, 60), ("valid", 0, 60), ("valid", 1, 0)],
)
def test_store_rejects_unsafe_rate_limit_parameters(
    tmp_path, bucket_key: str, limit: int, window_seconds: int
):
    store = DurableStore(tmp_path / "rate-limit-invalid.sqlite3")

    with pytest.raises(ValueError):
        store.consume_rate_limit(
            bucket_key,
            limit=limit,
            window_seconds=window_seconds,
            now=1200,
        )
