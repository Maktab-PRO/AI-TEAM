# Recommended Notion workspace (human knowledge only)

Use a separate top-level page named **AI TEAM**. Keep it as the human-readable decision and operating manual, not a shadow task database.

Recommended subpages:

1. `Operating Rules` — one-stage-at-a-time process, evidence policy, approvals, and stop conditions.
2. `Project Boundaries` — one page per immutable Project ID; include exact repository, owners, active/quarantined status, and links to reviewed registry commits.
3. `Architecture Decisions` — ADR ID, date, decision, alternatives, impact, evidence, superseding ADR.
4. `Runbooks` — GitHub CI failure, Render outage, backup/restore, security incident, release rollback.
5. `Verification Records` — links to immutable GitHub Actions runs/PRs, Render health results, scan reports; do not paste tokens or secrets.

The live Notion API integration is intentionally not activated in milestone 0.1. Before automating it, create a dedicated integration token with only the AI TEAM parent page shared to it; store the token in the hosting provider's secret store, never in GitHub source or the project registry. Sync direction should initially be control-plane → Notion, with provider IDs and idempotency, and no sync-back that can override task state.
