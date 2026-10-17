from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]
def edit(path, replacements):
    file = ROOT / path
    source = file.read_text(encoding="utf-8")
    for old, new in replacements:
        old, new = old.replace("¤", chr(96)), new.replace("¤", chr(96))
        count = source.count(old)
        if count != 1:
            raise SystemExit(f"{path}: expected one occurrence, found {count}: {old[:90]!r}")
        source = source.replace(old, new, 1)
    file.write_text(source, encoding="utf-8")

edit("ai_team_control_plane/store.py", [
('''                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );''', '''                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    hash_version INTEGER NOT NULL DEFAULT 1 CHECK (hash_version IN (1,2))
                );'''),
('''            )

    @contextmanager
    def transaction''', '''            )
            # Keep existing v1 hashes readable while new records use v2.
            audit_columns = {row[1] for row in conn.execute("PRAGMA table_info(audit_events)").fetchall()}
            if "hash_version" not in audit_columns:
                conn.execute(
                    "ALTER TABLE audit_events ADD COLUMN hash_version INTEGER NOT NULL DEFAULT 1 CHECK (hash_version IN (1,2))"
                )

    @contextmanager
    def transaction'''),
('''        material = "\\n".join((previous_hash, actor, event_type, payload_json, created_at))
        event_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT INTO audit_events(task_id, actor, event_type, payload_json, prev_hash, event_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, actor, event_type, payload_json, previous_hash, event_hash, created_at),
        )''', '''        # v2 binds the audit row's task_id column into the hash.
        hash_version = 2
        material = "\\n".join((previous_hash, task_id or "", actor, event_type, payload_json, created_at))
        event_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT INTO audit_events(task_id, actor, event_type, payload_json, prev_hash, event_hash, created_at, hash_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, actor, event_type, payload_json, previous_hash, event_hash, created_at, hash_version),
        )'''),
('''            material = "\\n".join((expected_prev, row["actor"], row["event_type"], row["payload_json"], row["created_at"]))
            expected_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
            if row["prev_hash"] != expected_prev or row["event_hash"] != expected_hash:
                return {"valid": False, "checked_events": total, "failed_event_id": row["id"], "reason": "hash_or_previous_hash_mismatch"}''', '''            hash_version = row["hash_version"] if "hash_version" in row.keys() else 1
            if hash_version == 1:
                # Compatibility for records created before task_id was included in the hash.
                material = "\\n".join((expected_prev, row["actor"], row["event_type"], row["payload_json"], row["created_at"]))
            elif hash_version == 2:
                material = "\\n".join((expected_prev, row["task_id"] or "", row["actor"], row["event_type"], row["payload_json"], row["created_at"]))
            else:
                return {"valid": False, "checked_events": total, "failed_event_id": row["id"], "reason": "unsupported_hash_version"}
            expected_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
            if row["prev_hash"] != expected_prev or row["event_hash"] != expected_hash:
                return {"valid": False, "checked_events": total, "failed_event_id": row["id"], "reason": "hash_or_previous_hash_mismatch"}''')
])

edit("ai_team_control_plane/registry.py", [
('''            if project.repository:
                if project.repository in repositories:
                    raise ValueError(
                        f"Repository {project.repository!r} is mapped to both "
                        f"{repositories[project.repository]!r} and {project.id!r}"
                    )
                repositories[project.repository] = project.id''', '''            if project.repository:
                repository = project.repository
                parts = repository.split("/")
                if (
                    repository != repository.strip()
                    or len(parts) != 2
                    or any(not part or part != part.strip() for part in parts)
                ):
                    raise ValueError(f"Repository mapping must be an exact owner/name pair without whitespace: {repository!r}")
                repository_key = repository.casefold()
                if repository_key in repositories:
                    raise ValueError(
                        f"Repository {repository!r} is mapped to both "
                        f"{repositories[repository_key]!r} and {project.id!r} (repository names are case-insensitive)"
                    )
                repositories[repository_key] = project.id''')
])

edit("ai_team_control_plane/api.py", [
('''import hmac
import os
''', '''import hmac
import json
import os
'''),
('''    environment = os.getenv("APP_ENV", "development").lower()
    token = os.getenv("AI_TEAM_API_TOKEN", "")
    if environment == "production" and len(token) < 32:
        raise HTTPException(status_code=503, detail="Production API is disabled until AI_TEAM_API_TOKEN is set to a strong token (32+ chars).")
    if token:
        supplied = authorization.removeprefix("Bearer ") if authorization else ""
        if not hmac.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="Valid Bearer token required.")
    elif environment == "production":
        raise HTTPException(status_code=503, detail="Production API token is not configured.")
    actor = (x_actor or "").strip()
    if not actor:
        raise HTTPException(status_code=400, detail="X-Actor header is required for traceability.")
    return actor''', '''    environment = os.getenv("APP_ENV", "development").strip().lower()
    supplied = authorization.removeprefix("Bearer ") if authorization else ""

    if environment == "production":
        # Prefer distinct opaque tokens mapped to fixed actor IDs. X-Actor is not an authentication claim.
        credentials: dict[str, str] = {}
        token_map_raw = os.getenv("AI_TEAM_API_TOKENS", "").strip()
        if token_map_raw:
            try:
                token_map = json.loads(token_map_raw)
            except json.JSONDecodeError as exc:
                raise HTTPException(status_code=503, detail="AI_TEAM_API_TOKENS must be a JSON object mapping tokens to actor IDs.") from exc
            if not isinstance(token_map, dict) or not token_map:
                raise HTTPException(status_code=503, detail="AI_TEAM_API_TOKENS must be a non-empty JSON object.")
            for candidate, identity in token_map.items():
                if not isinstance(candidate, str) or candidate != candidate.strip() or len(candidate) < 32 or not isinstance(identity, str):
                    raise HTTPException(status_code=503, detail="Each production API token must be an exact 32+ character string and map to an actor ID.")
                identity = identity.strip()
                if not identity or len(identity) > 120 or any(ord(ch) < 32 for ch in identity):
                    raise HTTPException(status_code=503, detail="Each production API token must map to a valid actor ID (1–120 characters).")
                credentials[candidate] = identity

        single_token = os.getenv("AI_TEAM_API_TOKEN", "").strip()
        if single_token:
            if len(single_token) < 32:
                raise HTTPException(status_code=503, detail="AI_TEAM_API_TOKEN must be at least 32 characters in production.")
            single_actor = os.getenv("AI_TEAM_API_ACTOR", "api-token").strip()
            if not single_actor or len(single_actor) > 120 or any(ord(ch) < 32 for ch in single_actor):
                raise HTTPException(status_code=503, detail="AI_TEAM_API_ACTOR must be a valid fixed service identity in production.")
            existing = credentials.get(single_token)
            if existing is not None and existing != single_actor:
                raise HTTPException(status_code=503, detail="The same production token cannot map to multiple actor identities.")
            credentials[single_token] = single_actor

        if not credentials:
            raise HTTPException(status_code=503, detail="Production API needs AI_TEAM_API_TOKENS or a strong AI_TEAM_API_TOKEN.")
        matched_actor = None
        for candidate, identity in credentials.items():
            if hmac.compare_digest(supplied, candidate):
                matched_actor = identity
        if matched_actor is None:
            raise HTTPException(status_code=401, detail="Valid Bearer token required.")
        supplied_actor = (x_actor or "").strip()
        if supplied_actor and supplied_actor != matched_actor:
            raise HTTPException(status_code=403, detail="X-Actor cannot override the identity mapped to the production bearer token.")
        return matched_actor

    token = os.getenv("AI_TEAM_API_TOKEN", "").strip()
    if token and not hmac.compare_digest(supplied, token):
        raise HTTPException(status_code=401, detail="Valid Bearer token required.")
    actor = (x_actor or "").strip()
    if not actor:
        raise HTTPException(status_code=400, detail="X-Actor header is required for traceability in development.")
    return actor'''),
('''def claim_task(task_id: str, body: ClaimTask, actor: str = Depends(authenticated_actor)) -> dict:
    try:
        return orchestrator.claim_task(task_id, worker_id=body.worker_id, lease_seconds=body.lease_seconds, actor=actor)''', '''def claim_task(task_id: str, body: ClaimTask, actor: str = Depends(authenticated_actor)) -> dict:
    try:
        # In production, bind lease ownership to the authenticated token identity.
        worker_id = actor if os.getenv("APP_ENV", "development").strip().lower() == "production" else body.worker_id
        return orchestrator.claim_task(task_id, worker_id=worker_id, lease_seconds=body.lease_seconds, actor=actor)''')
])

edit("ai_team_control_plane/orchestrator.py", [
('''    "blocked": {"queued", "in_progress", "cancelled"},''', '''    # A blocked task must be requeued and claimed before work resumes, restoring lease ownership.
    "blocked": {"queued", "cancelled"},'''),
('''class TaskOrchestrator:
    def __init__(self, store: DurableStore, guard: BoundaryGuard):
        self.store = store
        self.guard = guard
        self.gate = EvidenceGate(store)
''', '''class TaskOrchestrator:
    def __init__(self, store: DurableStore, guard: BoundaryGuard):
        self.store = store
        self.guard = guard
        self.gate = EvidenceGate(store)

    def _authorize_task_row(self, row) -> None:
        """Re-check current project policy before any state-changing operation."""
        self.guard.authorize(
            project_id=row["project_id"],
            repository=row["repository"],
            branch=row["branch"],
            paths=json.loads(row["paths_json"]),
            action="write",
        )
'''),
('''            status = row["status"]
            lease_expired = not row["lease_until"] or row["lease_until"] <= now
            if status == "claimed" and row["lease_owner"] != worker_id and not lease_expired:
                raise StateConflict(f"Task is leased by another worker until {row['lease_until']}.")
            if status not in {"queued", "claimed"}:
                raise StateConflict(f"Only queued or expired-lease claimed tasks can be claimed; current status is {status}.")
            deps = conn.execute(''', '''            self._authorize_task_row(row)
            status = row["status"]
            lease_expired = not row["lease_until"] or row["lease_until"] <= now
            if status in {"claimed", "in_progress"} and row["lease_owner"] != worker_id and not lease_expired:
                raise StateConflict(f"Task is leased by another worker until {row['lease_until']}.")
            if status not in {"queued", "claimed", "in_progress"}:
                raise StateConflict(f"Only queued tasks or tasks with a missing/expired lease can be claimed; current status is {status}.")
            deps = conn.execute('''),
('''            old_status = status
            conn.execute(
                "UPDATE tasks SET status='claimed',lease_owner=?,lease_until=?,version=version+1,updated_at=? WHERE id=?",
                (worker_id, lease_until, now, task_id),
            )
            payload = {"task_id": task_id, "from": old_status, "to": "claimed", "worker_id": worker_id, "lease_until": lease_until}
            self.store.append_audit(conn, actor, "task.claimed", payload, task_id)
            self.store.append_outbox(conn, task_id, "task.claimed", payload)''', '''            old_status = status
            new_status = "in_progress" if status == "in_progress" else "claimed"
            conn.execute(
                "UPDATE tasks SET status=?,lease_owner=?,lease_until=?,version=version+1,updated_at=? WHERE id=?",
                (new_status, worker_id, lease_until, now, task_id),
            )
            if status == "in_progress" and (lease_expired or row["lease_owner"] != worker_id):
                event_type = "task.lease_reclaimed"
            elif status in {"claimed", "in_progress"} and not lease_expired:
                event_type = "task.lease_renewed"
            else:
                event_type = "task.claimed"
            payload = {"task_id": task_id, "from": old_status, "to": new_status, "worker_id": worker_id, "lease_until": lease_until}
            self.store.append_audit(conn, actor, event_type, payload, task_id)
            self.store.append_outbox(conn, task_id, event_type, payload)'''),
('''            current = row["status"]
            if expected_version is not None and row["version"] != expected_version:''', '''            self._authorize_task_row(row)
            current = row["status"]
            if expected_version is not None and row["version"] != expected_version:'''),
('''            if row["lease_owner"] and current in {"claimed", "in_progress"} and actor != row["lease_owner"]:
                raise StateConflict(
                    f"Task is leased by '{row['lease_owner']}'; that worker must make the transition or release the lease."
                )''', '''            if current in {"claimed", "in_progress"}:
                lease_until = row["lease_until"]
                if not row["lease_owner"] or not lease_until or lease_until <= now:
                    raise StateConflict("Task lease is missing or expired; reclaim the task before changing its state.")
                if actor != row["lease_owner"]:
                    raise StateConflict(
                        f"Task is leased by '{row['lease_owner']}'; that worker must make the transition or release the lease."
                    )'''),
('''            row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task ID: {task_id}")
            if row["status"] in {"verified", "cancelled"}:''', '''            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task ID: {task_id}")
            self._authorize_task_row(row)
            if row["status"] in {"verified", "cancelled"}:'''),
('''            row = conn.execute("SELECT status,version FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task ID: {task_id}")
            if row["status"] != "evidence_pending":''', '''            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task ID: {task_id}")
            self._authorize_task_row(row)
            if row["status"] != "evidence_pending":''')
])

edit(".env.example", [
('''# Generate a private, high-entropy token (at least 32 characters) before APP_ENV=production.
# Never commit the real token.
AI_TEAM_API_TOKEN=
''', '''# Generate private, high-entropy bearer token(s) (at least 32 characters); never commit real tokens.
AI_TEAM_API_TOKEN=
# Single-token service identity (used with AI_TEAM_API_TOKEN in production).
AI_TEAM_API_ACTOR=api-token
# Optional production JSON map: {"<token-1>":"worker-a","<token-2>":"reviewer-b"}.
# Store real token values only in the hosting secret manager/environment.
AI_TEAM_API_TOKENS=
''')
])

edit("README.md", [
('''- The GitHub account's known repositories are only registered here as candidates when their identity is known. A fourth discovered repository is **quarantined** until its owner and project purpose are confirmed.
- ¤ai-team-control-plane¤ has ¤setup_required¤ and no repository mapped. It cannot schedule write tasks until a **dedicated AI TEAM repository** is created and the registry is updated in a reviewed change.
- The first three registered repositories are exact mappings only. The Guard does not guess that a task can be moved into another repository.
- ¤main¤, ¤master¤, and ¤production¤ are protected from direct writes. The initial allowed write branch prefixes are ¤ai-team/¤, ¤codex/¤, ¤feature/¤, and ¤fix/¤.''', '''- The registry has exact mappings for Axsikent IT, Technikum Digital Platform, the personal Telegram bot, and this dedicated AI TEAM repository. The discovered ¤Maktab-PRO/aqlli-mahalla-temirqishloq¤ repository remains **quarantined** until ownership and project boundaries are confirmed.
- Every task is tied to one exact repository. Repository uniqueness is case-insensitive to match GitHub naming behavior; the Guard never guesses that a task can move to another project.
- ¤main¤, ¤master¤, and ¤production¤ are protected from direct writes. For Axsikent, Technikum, and the Telegram bot, allowed write branch prefixes are ¤ai-team/¤, ¤codex/¤, ¤feature/¤, and ¤fix/¤; the AI TEAM repository is restricted to ¤ai-team/¤ branches.'''),
('''3. **Task Orchestrator** — persists task states, enforces legal state transitions, blocks cross-project dependencies, checks prerequisites, and grants time-limited worker leases.''', '''3. **Task Orchestrator** — persists task states, enforces legal transitions, blocks cross-project dependencies, re-checks current project boundaries on writes, checks prerequisites, and reclaims expired leases without losing in-progress state.'''),
('''6. **Audit Log** — append-only events chained with SHA-256 hashes; ¤/audit/verify¤ detects edits to historical events.''', '''6. **Audit Log** — append-only events chained with versioned SHA-256 hashes. Version 2 binds the task_id column and event content; legacy version-1 events remain verifiable but do not retroactively bind that column.'''),
('''The API binds to ¤127.0.0.1:8000¤ by default. OpenAPI UI is available locally at ¤/docs¤. Mutating routes require ¤X-Actor¤ and an ¤Idempotency-Key¤ for task creation. In production, set ¤APP_ENV=production¤ and a high-entropy ¤AI_TEAM_API_TOKEN¤ at least 32 characters long; the API refuses to run its routes without it. Keep the API private until authentication, rate limits, TLS, and a deployment-ready database are configured.''', '''The API binds to ¤127.0.0.1:8000¤ by default. OpenAPI UI is available locally at ¤/docs¤. Development requests use ¤X-Actor¤; task creation also requires an ¤Idempotency-Key¤. In production, set ¤APP_ENV=production¤ and either a high-entropy ¤AI_TEAM_API_TOKEN¤ (32+ characters) with fixed ¤AI_TEAM_API_ACTOR¤, or ¤AI_TEAM_API_TOKENS¤ as a JSON mapping of 32+ character tokens to fixed actor IDs. Production actor identity is derived from the matched token; a caller cannot override it with ¤X-Actor¤. A shared-token identity is a service identity, not proof of a human identity. This MVP still lacks project-scoped RBAC, rate limits, and provider-backed identity; keep it private until these and TLS plus production database operations are in place.'''),
('''- ¤AI_TEAM_API_TOKEN¤: required for authenticated API calls when set; mandatory in production.
- ¤APP_ENV¤: ¤development¤ by default; set to ¤production¤ for the stricter token requirement.''', '''- ¤AI_TEAM_API_TOKEN¤: optional in development; in production, a single high-entropy bearer token (32+ characters) paired with ¤AI_TEAM_API_ACTOR¤.
- ¤AI_TEAM_API_ACTOR¤: fixed audit identity for the single-token production mode; default ¤api-token¤.
- ¤AI_TEAM_API_TOKENS¤: optional production JSON object mapping distinct 32+ character bearer tokens to actor IDs. Store only in a secret manager/environment, never source control.
- ¤APP_ENV¤: ¤development¤ by default; production enables token-to-identity binding, not project-scoped authorization.''')
])

edit("docs/CONTROL-PLANE-SPEC.md", [
('''**Invariant:** task project ID and repository must match exactly. Mutating tasks must target a non-protected branch with an approved prefix. Relative paths may not traverse outside the repo or reach secret/key/CI-control paths. Destructive/external actions are denied until a separate approval/execution capability is implemented.''', '''**Invariant:** task project ID and repository must match exactly. Repository uniqueness is case-insensitive. Mutating tasks must target a non-protected branch with an approved prefix, and current project policy is rechecked before claim, transition, evidence write, or verification. Relative paths may not traverse outside the repo or reach secret/key/CI-control paths. Destructive/external actions are denied until a separate approval/execution capability is implemented.'''),
('''**Invariant:** all states go through the transition table. Each task has an idempotency key; reuse with the same request returns the original task, while reuse with a different request is a conflict. Dependencies are intra-project and must be verified before claim. Leases are stored transactionally and have bounded expiry.''', '''**Invariant:** all states go through the transition table. Each task has an idempotency key; reuse with the same request returns the original task, while reuse with a different request is a conflict. Dependencies are intra-project and must be verified before claim. Leases are stored transactionally and have bounded expiry; an expired/missing lease cannot transition state until reclaimed. A blocked task must be requeued and claimed before work resumes.'''),
('''**Invariant:** every material task/evidence state change writes an audit event in the same transaction. Events are canonicalized and linked with SHA-256 hashes. The verify endpoint reports the first mismatch; a passing chain signals integrity since the last trusted checkpoint, not proof that the original actor was honest.''', '''**Invariant:** every material task/evidence state change writes an audit event in the same transaction. Events are canonicalized and linked with versioned SHA-256 hashes. Version 2 covers the task_id column and event content; legacy version-1 events remain readable but did not bind that column. A passing chain signals integrity since the last trusted checkpoint, not proof that the original actor was honest.'''),
('''- No claim that SQLite has been validated for concurrent multi-instance production.''', '''- No claim that SQLite has been validated for concurrent multi-instance production. Production bearer tokens map to fixed actor IDs, but project-/role-scoped RBAC, human identity, and rate limiting are not implemented; keep the service private.''')
])

edit("docs/ARCHITECTURE.md", [
('''Other supported transitions: ¤draft → queued¤, ¤claimed → blocked|queued¤, ¤in_progress → blocked|queued|cancelled¤, ¤blocked → queued|in_progress|cancelled¤, ¤evidence_pending → in_progress|rejected¤, ¤rejected → in_progress|cancelled¤. ¤verified¤ and ¤cancelled¤ are terminal. The API cannot set ¤verified¤ directly.''', '''Other supported transitions: ¤draft → queued¤, ¤claimed → blocked|queued¤, ¤in_progress → blocked|queued|cancelled¤, ¤blocked → queued|cancelled¤, ¤evidence_pending → in_progress|rejected¤, ¤rejected → in_progress|cancelled¤. A blocked task must be requeued and claimed to restore a worker lease before work resumes. ¤verified¤ and ¤cancelled¤ are terminal. The API cannot set ¤verified¤ directly.'''),
('''A worker lease and the task state update happen under a write transaction. The database stores an expiry. A different worker cannot steal an unexpired lease. The lease owner must match the actor for ordinary transitions, transitions use a required expected-version precondition through the API, and evidence updates bump the aggregate version. Dependencies must be in the same project and ¤verified¤ before a dependent task can be claimed.''', '''A worker lease and the task state update happen under a write transaction. The database stores an expiry. A different worker cannot steal an unexpired lease; an expired lease must be reclaimed before a state transition, and expired in_progress tasks can be reassigned without losing their state. The lease owner must match the authenticated actor for ordinary transitions, transitions use a required expected-version precondition through the API, and evidence updates bump the aggregate version. Project/repository/branch/path boundaries are rechecked before task-state, evidence, or verification writes. Dependencies must be in the same project and ¤verified¤ before a dependent task can be claimed.'''),
('''SQLite is suitable for the first local/single-instance milestone. For multiple concurrent service instances, migrate the same transaction boundaries and constraints to PostgreSQL.''', '''Audit hash version 2 binds the task_id database column as well as event metadata/content. Earlier version-1 records remain verifiable for compatibility, but their task ID column was not part of the original hash. The current chain has no external signed checkpoint, so a full database rewrite by a privileged attacker is outside the protection boundary.

SQLite is suitable for the first local/single-instance milestone. For multiple concurrent service instances, migrate the same transaction boundaries and constraints to PostgreSQL.'''),
('''Unknown project, missing repository mapping, quarantined project, cross-project repository mismatch, protected branch write, unsafe path, cross-project dependency, invalid transition, expired or conflicting lease, missing/untrusted evidence, or a broken audit chain must fail closed and be visible in the response/logs.''', '''Unknown project, missing repository mapping, quarantined project, cross-project repository mismatch, protected branch write, unsafe path, cross-project dependency, invalid transition, expired or conflicting lease, missing/untrusted evidence, or a broken audit chain must fail closed and be visible in the response/logs. Production API identity is derived from the bearer-token mapping; X-Actor is only a development trace label. This milestone does not implement project-scoped RBAC or a human identity provider.''')
])

api_file = ROOT / "tests/test_api.py"
source = api_file.read_text(encoding="utf-8")
old = '''    authenticated = client.get("/projects", headers={"X-Actor": "api-test", "Authorization": f"Bearer {token}"})
    assert authenticated.status_code == 200
'''
new = '''    authenticated = client.get("/projects", headers={"Authorization": f"Bearer {token}"})
    assert authenticated.status_code == 200
    assert api.authenticated_actor(
        request=None,
        authorization=f"Bearer {token}",
        x_actor="api-token",
    ) == "api-token"
    spoofed_actor = client.get(
        "/projects",
        headers={"X-Actor": "admin-impersonation", "Authorization": f"Bearer {token}"},
    )
    assert spoofed_actor.status_code == 403
'''
if source.count(old) != 1:
    raise SystemExit("tests/test_api.py: production authentication assertion block mismatch")
source = source.replace(old, new, 1)
source += '''

def test_production_api_actor_cannot_be_spoofed_and_is_pinned(monkeypatch, system):
    setup_api(monkeypatch, system)
    token = "production-secret-test-token-0123456789-abcdef"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AI_TEAM_API_TOKEN", token)
    monkeypatch.setenv("AI_TEAM_API_ACTOR", "ci-service")
    client = TestClient(api.app)
    response = client.get("/projects", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert api.authenticated_actor(None, f"Bearer {token}", "ci-service") == "ci-service"
    impersonation = client.get(
        "/projects",
        headers={"Authorization": f"Bearer {token}", "X-Actor": "project-owner"},
    )
    assert impersonation.status_code == 403


def test_production_token_map_binds_distinct_actor_identities(monkeypatch, system):
    setup_api(monkeypatch, system)
    token_a = "token-for-worker-a-0123456789-abcdef"
    token_b = "token-for-reviewer-b-0123456789-abcdef"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("AI_TEAM_API_TOKEN", raising=False)
    monkeypatch.setenv("AI_TEAM_API_TOKENS", __import__("json").dumps({token_a: "worker-a", token_b: "reviewer-b"}))
    client = TestClient(api.app)
    worker_response = client.get("/projects", headers={"Authorization": f"Bearer {token_a}"})
    assert worker_response.status_code == 200
    assert api.authenticated_actor(None, f"Bearer {token_a}", None) == "worker-a"
    wrong_identity = client.get(
        "/projects",
        headers={"Authorization": f"Bearer {token_a}", "X-Actor": "reviewer-b"},
    )
    assert wrong_identity.status_code == 403


def test_production_claim_uses_authenticated_actor_as_lease_owner(monkeypatch, system):
    setup_api(monkeypatch, system)
    token = "worker-identity-secret-0123456789-abcdef"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AI_TEAM_API_TOKEN", token)
    monkeypatch.setenv("AI_TEAM_API_ACTOR", "worker-a")
    client = TestClient(api.app)
    created = client.post(
        "/tasks",
        json=valid_task_payload(),
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "production-lease-bind"},
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    claimed = client.post(
        f"/tasks/{task_id}/claim",
        json={"worker_id": "spoofed-worker", "lease_seconds": 60},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["lease_owner"] == "worker-a"
    progressed = client.post(
        f"/tasks/{task_id}/transition",
        json={"status": "in_progress", "expected_version": claimed.json()["version"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert progressed.status_code == 200, progressed.text
'''
api_file.write_text(source, encoding="utf-8")

orch_file = ROOT / "tests/test_orchestrator.py"
source = orch_file.read_text(encoding="utf-8")
if "test_expired_lease_must_be_reclaimed_before_transition" in source:
    raise SystemExit("lease/audit tests appear to have already been applied")
source += '''

def test_expired_lease_must_be_reclaimed_before_transition(system):
    _, store, _, service = system
    task = create_task(service, idempotency_key="expired-lease-transition")
    service.claim_task(task["id"], worker_id="worker-a", actor="worker-a", lease_seconds=60)
    with store.transaction() as conn:
        conn.execute("UPDATE tasks SET lease_until=? WHERE id=?", ("2000-01-01T00:00:00.000+00:00", task["id"]))
    with pytest.raises(StateConflict, match="lease is missing or expired"):
        service.transition(task["id"], "in_progress", actor="worker-a")
    claimed = service.claim_task(task["id"], worker_id="worker-b", actor="worker-b", lease_seconds=60)
    assert claimed["status"] == "claimed"
    assert claimed["lease_owner"] == "worker-b"
    progressed = service.transition(task["id"], "in_progress", actor="worker-b")
    assert progressed["status"] == "in_progress"


def test_expired_in_progress_lease_can_be_reclaimed_without_losing_state(system):
    _, store, _, service = system
    task = create_task(service, idempotency_key="expired-in-progress-lease")
    service.claim_task(task["id"], worker_id="worker-a", actor="worker-a", lease_seconds=60)
    service.transition(task["id"], "in_progress", actor="worker-a")
    with store.transaction() as conn:
        conn.execute("UPDATE tasks SET lease_until=? WHERE id=?", ("2000-01-01T00:00:00.000+00:00", task["id"]))
    with pytest.raises(StateConflict, match="lease is missing or expired"):
        service.transition(task["id"], "evidence_pending", actor="worker-a")
    reclaimed = service.claim_task(task["id"], worker_id="worker-b", actor="worker-b", lease_seconds=60)
    assert reclaimed["status"] == "in_progress"
    assert reclaimed["lease_owner"] == "worker-b"
    pending = service.request_verification(task["id"], actor="worker-b")
    assert pending["status"] == "evidence_pending"


def test_task_cannot_be_claimed_after_project_is_quarantined(system):
    from dataclasses import replace
    from ai_team_control_plane.models import BoundaryViolation

    registry, _, _, service = system
    task = create_task(service, idempotency_key="quarantine-after-creation")
    project = registry.require("axsikent-it")
    registry._projects[project.id] = replace(project, status="quarantined")
    with pytest.raises(BoundaryViolation, match="not active"):
        service.claim_task(task["id"], worker_id="worker-a", actor="worker-a")


def test_audit_hash_detects_task_id_column_tampering(system):
    _, store, _, service = system
    task = create_task(service, idempotency_key="audit-task-id-binding")
    assert store.verify_audit_chain()["valid"] is True
    with store.transaction() as conn:
        conn.execute("DROP TRIGGER audit_events_no_update")
        conn.execute("UPDATE audit_events SET task_id=? WHERE task_id=?", ("tampered-task", task["id"]))
    result = store.verify_audit_chain()
    assert result["valid"] is False
    assert result["failed_event_id"] == 1


def test_existing_audit_chain_without_hash_version_migrates_as_v1(tmp_path):
    import hashlib
    from ai_team_control_plane.store import ZERO_HASH, canonical_json

    db_path = tmp_path / "legacy.sqlite3"
    payload_json = canonical_json({"legacy": True})
    actor = "legacy-service"
    event_type = "legacy.event"
    created_at = "2026-01-01T00:00:00.000+00:00"
    material = "\\n".join((ZERO_HASH, actor, event_type, payload_json, created_at))
    event_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
    with sqlite3.connect(db_path) as conn:
        conn.execute("""CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, actor TEXT NOT NULL,
            event_type TEXT NOT NULL, payload_json TEXT NOT NULL, prev_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL, created_at TEXT NOT NULL
        )""")
        conn.execute(
            "INSERT INTO audit_events(task_id, actor, event_type, payload_json, prev_hash, event_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (None, actor, event_type, payload_json, ZERO_HASH, event_hash, created_at),
        )
    migrated = DurableStore(db_path)
    assert migrated.verify_audit_chain()["valid"] is True
    with migrated.connection() as conn:
        version = conn.execute("SELECT hash_version FROM audit_events WHERE id=1").fetchone()[0]
    assert version == 1
'''
orch_file.write_text(source, encoding="utf-8")

(ROOT / "tests/test_registry.py").write_text('''import json
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
''', encoding="utf-8")

print("Applied the reviewed security/correctness changes.")
