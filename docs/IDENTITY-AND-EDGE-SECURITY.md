# Human Identity and Edge Security — Integration Contract

**Status: DESIGN GATE ONLY.** No human identity provider, issuer URL, client registration, callback domain, or production credentials have been configured. This document does not enable login and does not authorize deployment.

## Boundary

This contract applies only to the AI TEAM Control Plane in `Maktab-PRO/AI-TEAM`. Do not reuse identities, callback URLs, client secrets, access tokens, or role mappings from Axsikent IT, Technikum Digital Platform, or the personal Telegram bot.

Machine credentials (`AI_TEAM_API_TOKEN` / `AI_TEAM_API_TOKENS`) are service identities for controlled automation. They must not be presented as human login or a substitute for user authentication.

## Provider-neutral OIDC requirements

Before implementation can be enabled, an operator must register an OIDC-compatible identity provider and supply its exact issuer, API audience, client ID, allowed redirect URI(s), and claims/role mapping. These values belong in the host configuration/secret manager as appropriate, not in committed code.

1. **Use Authorization Code with PKCE** for browser sign-in. Require HTTPS for issuer and redirect URIs outside localhost development. Never place access tokens, ID tokens, authorization codes, or client secrets in URLs, logs, screenshots, issue comments, or source control.
2. **Validate access tokens using a maintained library.** Verify signature against keys published by the configured issuer's JWKS, an explicit allowed algorithm set, exact `iss`, exact API `aud`, `exp`, `nbf`, and reasonable `iat` bounds. Do not accept an ID token as an API access token. Do not accept a `jku`, issuer, key URL, or algorithm chosen by an untrusted token.
3. **Resolve keys safely.** Obtain OIDC discovery/JWKS only from the configured HTTPS issuer, cache with bounded lifetime, support planned key rotation, bound response sizes/timeouts, and fail closed when the token cannot be validated. Never fetch arbitrary URLs supplied by a caller.
4. **Map identity to authorization on the server.** Use the stable `(issuer, sub)` pair as the principal key. Do not use email alone as an identity key. Assign AI TEAM roles and project scopes using an explicit trusted claim contract or server-side mapping; unknown users, unknown roles, quarantined projects, unregistered projects, and global `*` scope for non-platform roles must be denied.
5. **Preserve project boundaries.** Each authenticated principal receives one role and an explicit list of active project IDs. Every task read, mutation, evidence operation, audit access, and outbox access continues to enforce scope. Identity claims never grant a repository mapping or allow moving a task to another project.
6. **Keep browser sessions private.** If a browser session is introduced, use `Secure`, `HttpOnly`, and suitable `SameSite` cookies; implement CSRF protection for cookie-authenticated state changes; set an exact CORS allowlist; do not keep long-lived bearer tokens in localStorage. Define logout, expiry, revocation, and role-change invalidation behavior.
7. **Keep machine and human paths distinct.** Retain service-token authentication for automation only where needed. Record whether a principal came from OIDC or a service token, and use a stable non-secret actor reference in the audit trail. Never record credentials or full tokens.

## Rate limiting and ingress

The API has a shared database-backed fixed-window request limit, keyed by HMAC fingerprints of the direct ASGI client peer and any bearer token. Production configuration requires `AI_TEAM_RATE_LIMIT_PER_MINUTE` and a separate random `AI_TEAM_RATE_LIMIT_HASH_KEY` of at least 32 characters. Raw client identities and bearer tokens are not written to the rate-limit table.

This is not sufficient protection by itself. Before public exposure:

- Apply ingress/WAF rate limits to unauthenticated traffic and requests spread across rotating IPs.
- Verify the actual peer address the ASGI server observes behind the selected hosting proxy. Do not blindly trust `X-Forwarded-For`; only use forwarded identity if the proxy path and exact trusted proxy ranges are configured and tested.
- Test that health probes do not create false rate-limit outages and that distributed requests hit the shared limit across two service instances.
- Verify limits, `429`/ `Retry-After`, database-outage fail-closed behavior, and sanitized logs in private staging.

## Required acceptance tests

- Reject bad signatures, unsupported algorithms, wrong issuer/audience, expired/not-yet-valid tokens, missing subject, and unknown signing keys.
- Reject unknown `(issuer, sub)`, unknown role, invalid project scope, non-active project, and any unauthorized cross-project access.
- Verify JWKS timeout/oversized response handling, cache expiry, key rotation, and issuer outage behavior.
- Verify role/project changes invalidate or re-evaluate existing access within a documented bound.
- Verify session cookie flags, CSRF controls, exact CORS policy, logout, and log redaction if browser sessions are enabled.
- Verify rate limits against direct and trusted-proxy paths with more than one application instance.
- Show that all rejection paths fail closed and do not leak tokens, issuer metadata secrets, or internal exception strings.

## Release gate

This work remains blocked until an authorized operator chooses/provisions the provider, registers the callback domain and client, sets the exact issuer/audience and role mapping, and provides sanitized verification evidence. Until then, keep the API private and continue using explicitly scoped service tokens only for controlled automation. No live provider, login UI, callback endpoint, or production deployment is created by this contract.
