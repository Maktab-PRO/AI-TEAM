from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .boundary import BoundaryGuard
from .orchestrator import TaskOrchestrator
from .registry import ProjectRegistry
from .database import create_store
from .preflight import build_preflight_report

ROOT = Path(__file__).resolve().parents[1]


def _setup():
    registry_path = Path(os.getenv("AI_TEAM_REGISTRY_PATH", ROOT / "config" / "projects.json"))
    registry = ProjectRegistry(registry_path)
    store = create_store()
    guard = BoundaryGuard(registry)
    return registry, store, guard, TaskOrchestrator(store, guard)


def main() -> None:
    parser = argparse.ArgumentParser(prog="ai-team", description="AI TEAM Control Plane utilities")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight", help="Validate production configuration shape without connecting to services")
    sub.add_parser("check-registry", help="Validate the project registry and show its isolation status")
    sub.add_parser("verify-audit", help="Recompute and validate the append-only audit hash chain")
    sub.add_parser("snapshot", help="Print a safe JSON snapshot of the project registry")
    publish = sub.add_parser("publish-outbox", help="Publish a bounded batch of pending events to Redis, once")
    publish.add_argument("--limit", type=int, default=100)
    serve = sub.add_parser("serve", help="Run the FastAPI service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.command == "serve":
        import uvicorn
        uvicorn.run("ai_team_control_plane.api:app", host=args.host, port=args.port, reload=False)
        return

    if args.command == "preflight":
        result = build_preflight_report()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if not result["configuration_valid"]:
            raise SystemExit(2)
        return

    registry, store, _, _ = _setup()
    if args.command == "check-registry":
        projects = registry.all()
        result = {
            "valid": True,
            "count": len(projects),
            "active": [p.id for p in projects if p.status == "active"],
            "blocked": [{"id": p.id, "status": p.status} for p in projects if p.status != "active"],
        }
        print(json.dumps(result, indent=2))
    elif args.command == "verify-audit":
        result = store.verify_audit_chain()
        print(json.dumps(result, indent=2))
        if not result["valid"]:
            raise SystemExit(2)
    elif args.command == "snapshot":
        print(json.dumps(registry.snapshot(), indent=2, ensure_ascii=False))
    elif args.command == "publish-outbox":
        redis_url = os.getenv("REDIS_URL", "").strip()
        if not redis_url:
            raise SystemExit("REDIS_URL is not set; no external Redis write was attempted.")
        try:
            import redis
        except ImportError as exc:
            raise SystemExit("Redis adapter missing. Install the optional dependency: pip install -e '.[redis]'") from exc
        from .integrations.redis_outbox import RedisOutboxPublisher
        client = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=5, socket_timeout=10)
        try:
            client.ping()
            result = RedisOutboxPublisher(client, store).publish_pending(limit=args.limit)
            print(json.dumps(result, indent=2))
        finally:
            client.close()


if __name__ == "__main__":
    main()
