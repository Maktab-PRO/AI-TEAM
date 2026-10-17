from __future__ import annotations

import ipaddress
import json
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..evidence import REQUIREMENTS
from ..models import EvidenceRejected


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Health checks must not be redirected to an unapproved destination.
        return None


class RenderHealthEvidenceVerifier:
    """Verify a configured health endpoint without accepting caller-supplied URLs."""

    VERIFIER_ID = "render-health-api"

    def __init__(
        self,
        orchestrator,
        health_targets: dict[str, dict[str, str]],
        *,
        opener: Callable[..., Any] | None = None,
        timeout: float = 8.0,
    ):
        self.orchestrator = orchestrator
        self.health_targets = {
            project_id: dict(target)
            for project_id, target in health_targets.items()
        }
        self.opener = opener or build_opener(_NoRedirectHandler()).open
        self.timeout = timeout

    @staticmethod
    def _validate_target(target: dict[str, str]) -> None:
        url = target.get("url", "")
        expected_service = target.get("expected_service", "")
        expected_status = target.get("expected_status", "ok")
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path != "/health"
            or parsed.port not in (None, 443)
        ):
            raise EvidenceRejected("Configured Render health target must be an exact HTTPS /health URL without credentials, query, fragment, or non-standard port.")
        host = parsed.hostname.rstrip(".").casefold()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise EvidenceRejected("Configured health target cannot use an IP-literal host.")
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid")):
            raise EvidenceRejected("Configured health target cannot point to a local or reserved hostname.")
        if not expected_service or expected_service != expected_service.strip() or len(expected_service) > 120:
            raise EvidenceRejected("Configured health target must have an exact expected_service marker.")
        if not expected_status or expected_status != expected_status.strip() or len(expected_status) > 80:
            raise EvidenceRejected("Configured health target must have a valid expected_status marker.")

    def _request_health(self, url: str) -> dict[str, Any]:
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "AI-TEAM-Control-Plane-Health-Verifier",
                "Cache-Control": "no-cache",
            },
            method="GET",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                status = getattr(response, "status", None)
                if status is None and hasattr(response, "getcode"):
                    status = response.getcode()
                body = response.read()
        except HTTPError as exc:
            raise EvidenceRejected(
                f"Configured health endpoint returned HTTP {exc.code}; no trusted health evidence was recorded."
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise EvidenceRejected("Configured health endpoint could not be reached; no trusted evidence was recorded.") from exc

        if status != 200:
            raise EvidenceRejected(f"Configured health endpoint returned HTTP {status!r}; expected 200.")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceRejected("Configured health endpoint did not return valid JSON; no trusted evidence was recorded.") from exc
        if not isinstance(payload, dict):
            raise EvidenceRejected("Configured health endpoint returned an unexpected JSON shape.")
        return payload

    def verify_and_record(self, task_id: str, *, actor: str) -> dict[str, Any]:
        task = self.orchestrator.store.get_task(task_id)
        if task is None:
            raise KeyError(f"Unknown task ID: {task_id}")
        requirements = REQUIREMENTS.get(task["task_type"], ())
        if not any(item.kind == "health_check_passed" and "render_health" in item.accepted_sources for item in requirements):
            raise EvidenceRejected(f"Task type {task['task_type']!r} does not require Render health evidence.")

        target = self.health_targets.get(task["project_id"])
        if not target:
            raise EvidenceRejected(
                f"No Render health target is configured for project {task['project_id']!r}; no trusted evidence was recorded."
            )
        self._validate_target(target)

        payload = self._request_health(target["url"])
        expected_service = target["expected_service"]
        expected_status = target.get("expected_status", "ok")
        if payload.get("service") != expected_service:
            raise EvidenceRejected(
                f"Health service marker {payload.get('service')!r} did not match the configured service {expected_service!r}."
            )
        if payload.get("status") != expected_status:
            raise EvidenceRejected(
                f"Health status {payload.get('status')!r} did not match the configured healthy status {expected_status!r}."
            )

        notes = (
            f"Verified configured HTTPS health endpoint; project_id={task['project_id']}; "
            f"expected_service={expected_service}; expected_status={expected_status}; "
            f"http_status=200"
        )
        evidence = self.orchestrator.record_evidence(
            task_id,
            kind="health_check_passed",
            status="passed",
            uri=target["url"],
            source="render_health",
            actor=actor,
            notes=notes,
            trusted=True,
            verified_by=self.VERIFIER_ID,
        )
        return {
            "task_id": task_id,
            "project_id": task["project_id"],
            "health_url": target["url"],
            "recorded_kind": "health_check_passed",
            "evidence": evidence,
        }
