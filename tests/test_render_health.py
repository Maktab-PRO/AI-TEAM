from __future__ import annotations

import json
from urllib.error import HTTPError

import pytest

from ai_team_control_plane.integrations.render_health import RenderHealthEvidenceVerifier
from ai_team_control_plane.models import EvidenceRejected


TARGET = {
    "url": "https://axsikent-it-backend.onrender.com/health",
    "expected_service": "axsikent-it-backend",
    "expected_status": "ok",
}


class FakeResponse:
    def __init__(self, value, status=200):
        self.payload = json.dumps(value).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def task_for(service, task_type="deployment", key="render-health-task"):
    return service.create_task(
        project_id="axsikent-it",
        title="Verify deployment health",
        description="Render health verifier integration test.",
        task_type=task_type,
        repository="Maktab-PRO/Axsikent-IT",
        branch="ai-team/render-health",
        paths=["docs/control-plane.md"],
        idempotency_key=key,
        actor="health-test",
    )


def verifier(service, opener, targets=None):
    return RenderHealthEvidenceVerifier(
        service,
        targets if targets is not None else {"axsikent-it": TARGET},
        opener=opener,
    )


def healthy_payload(**changes):
    result = {"status": "ok", "service": "axsikent-it-backend"}
    result.update(changes)
    return result


def test_render_health_records_trusted_evidence_only_for_configured_target(system):
    _, store, _, service = system
    task = task_for(service)
    calls = []

    def opener(request, timeout=8):
        calls.append((request.full_url, request.get_method(), request.headers, timeout))
        return FakeResponse(healthy_payload())

    result = verifier(service, opener).verify_and_record(task["id"], actor="health-verifier")

    assert result["recorded_kind"] == "health_check_passed"
    assert result["health_url"] == TARGET["url"]
    assert calls[0][0] == TARGET["url"]
    assert calls[0][1] == "GET"
    evidence = store.list_evidence(task["id"])
    assert len(evidence) == 1
    assert evidence[0]["kind"] == "health_check_passed"
    assert evidence[0]["source"] == "render_health"
    assert evidence[0]["trusted"] is True
    assert evidence[0]["verified_by"] == "render-health-api"


def test_render_health_rejects_wrong_service_marker(system):
    _, store, _, service = system
    task = task_for(service, key="render-health-wrong-service")
    check = verifier(service, lambda *_args, **_kwargs: FakeResponse(healthy_payload(service="different-service")))

    with pytest.raises(EvidenceRejected, match="did not match the configured service"):
        check.verify_and_record(task["id"], actor="health-verifier")

    assert store.list_evidence(task["id"]) == []


def test_render_health_rejects_unhealthy_status(system):
    _, store, _, service = system
    task = task_for(service, key="render-health-unhealthy")
    check = verifier(service, lambda *_args, **_kwargs: FakeResponse(healthy_payload(status="degraded")))

    with pytest.raises(EvidenceRejected, match="did not match the configured healthy status"):
        check.verify_and_record(task["id"], actor="health-verifier")

    assert store.list_evidence(task["id"]) == []


def test_render_health_rejects_non_200_status(system):
    _, store, _, service = system
    task = task_for(service, key="render-health-http-503")
    check = verifier(service, lambda *_args, **_kwargs: FakeResponse(healthy_payload(), status=503))

    with pytest.raises(EvidenceRejected, match="expected 200"):
        check.verify_and_record(task["id"], actor="health-verifier")

    assert store.list_evidence(task["id"]) == []


def test_render_health_rejects_redirect_response(system):
    _, store, _, service = system
    task = task_for(service, key="render-health-redirect")

    def redirect(_request, timeout=8):
        raise HTTPError(TARGET["url"], 302, "Found", {}, None)

    with pytest.raises(EvidenceRejected, match="HTTP 302"):
        verifier(service, redirect).verify_and_record(task["id"], actor="health-verifier")

    assert store.list_evidence(task["id"]) == []


def test_render_health_rejects_unsafe_configured_target(system):
    _, store, _, service = system
    task = task_for(service, key="render-health-unsafe-target")
    unsafe = {
        "axsikent-it": {
            "url": "https://127.0.0.1/health",
            "expected_service": "axsikent-it-backend",
            "expected_status": "ok",
        }
    }
    check = verifier(service, lambda *_args, **_kwargs: pytest.fail("network must not be called"), unsafe)

    with pytest.raises(EvidenceRejected, match="IP-literal host"):
        check.verify_and_record(task["id"], actor="health-verifier")

    assert store.list_evidence(task["id"]) == []


def test_render_health_rejects_arbitrary_path_and_local_host(system):
    _, _, _, service = system
    task = task_for(service, key="render-health-path-check")
    unsafe_targets = [
        {
            "axsikent-it": {
                "url": "https://health.example.com/private",
                "expected_service": "axsikent-it-backend",
                "expected_status": "ok",
            }
        },
        {
            "axsikent-it": {
                "url": "https://localhost/health",
                "expected_service": "axsikent-it-backend",
                "expected_status": "ok",
            }
        },
    ]
    for targets, message in zip(unsafe_targets, ["exact HTTPS /health URL", "local or reserved hostname"]):
        check = verifier(service, lambda *_args, **_kwargs: pytest.fail("network must not be called"), targets)
        with pytest.raises(EvidenceRejected, match=message):
            check.verify_and_record(task["id"], actor="health-verifier")


def test_render_health_only_applies_to_deployment_tasks(system):
    _, store, _, service = system
    task = task_for(service, task_type="implementation", key="render-health-not-deployment")
    check = verifier(service, lambda *_args, **_kwargs: pytest.fail("network must not be called"))

    with pytest.raises(EvidenceRejected, match="does not require Render health evidence"):
        check.verify_and_record(task["id"], actor="health-verifier")

    assert store.list_evidence(task["id"]) == []


def test_render_health_fails_closed_without_project_target(system):
    _, store, _, service = system
    task = task_for(service, key="render-health-no-target")
    check = verifier(service, lambda *_args, **_kwargs: pytest.fail("network must not be called"), {})

    with pytest.raises(EvidenceRejected, match="No Render health target"):
        check.verify_and_record(task["id"], actor="health-verifier")

    assert store.list_evidence(task["id"]) == []
