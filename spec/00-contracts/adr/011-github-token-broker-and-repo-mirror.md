# ADR-011: GitHub token broker and read-only repo mirror

- **Status:** Accepted (orchestrator, OQ-00-04). **Amended by CCR-09-06 (C-ARCH 1.1.0):** caller
  allowlist extended and a never-granted permission list added. CCR-09-06 also covers WS-07's
  CCR-07-01 (identity-service as a caller).
- **Constraints:** D-21 (no push credentials in sandboxes), D-24 (GitHub App; dev and prod
  registrations)

## Context

The WS-07 brief says only the Publisher and the webhook/reply service hold installation tokens.
Sandboxes still need to clone private repositories quickly (p95 sandbox-ready under 90 s), and
something must hold read credentials for that. Installation tokens expire after 1 hour and can be
scoped to specific repositories and to a subset of permissions [verified:
https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app,
2026-10-24]. The App private key can mint tokens for every installation, so it is the most
sensitive GitHub secret.

## Options

1. **Each service holds the private key** (publisher, integration, mirror). Three copies of the
   most sensitive secret.
2. **Sandbox fetches with a read-only, single-repo installation token.** Credentials in the
   sandbox, which D-21 allows if short-lived and scoped. But any token is exfiltratable, and a
   read token for a private repo is valuable.
3. **Token broker plus mirror (chosen).**

## Decision

- **`github-token-broker`** (core, `replicant-integrations`) is the only holder of the App private
  key(s). It mints installation tokens on request from these allowlisted callers only:
  - `publisher-worker`: `contents:write`, `pull_requests:write`, one repository per request;
  - `github-integration`: `issues:write`, `pull_requests:write`, `checks:write`, one repository;
  - `repo-mirror`: `contents:read`, `metadata:read`;
  - `identity-service`: `metadata:read` (collaborator permission lookups, one repository per
    request) and `members:read` (org membership, D-15);
  - `zoekt-indexer` and `ecosystem-indexer`: `contents:read`, `metadata:read`, for opted-in
    repositories only.

  **Never granted:** no caller may request `workflows`, `administration`, `secrets`, or `actions`,
  and the App registration does not hold these permissions, so the broker could not mint them even
  if misconfigured. The broker rejects any request whose permission set or repository list exceeds
  the caller's allowlist entry.

  Each request is audited (`audit.emit`). Callers authenticate with projected ServiceAccount tokens.
- **`repo-mirror`** (exec cluster) serves read-only smart HTTP to sandboxes. Sandboxes authenticate
  with their `repo-mirror`-audience run token, and the mirror allows only `repo_ids` listed in the token. It holds
  short-lived read tokens from the broker in memory.
- Sandboxes never hold GitHub tokens of any kind.

## Consequences

- The set of installation-token holders is {publisher-worker, github-integration, repo-mirror,
  identity-service, zoekt-indexer, ecosystem-indexer}, and only the broker holds the key. Every
  holder except publisher-worker and github-integration is read-only.
- The mirror is a cache: a fetch-latency win, and a dependency on the exec side. If it is down,
  runs fail with `RepoFetchFailed`, which is retryable.
- The dev App registration has its own broker configuration and key. A leaked dev key reaches only
  the developer's personal org installations (WS-09).

## Revisit when

- GitHub offers per-request downscoped tokens without the App key (for example via an OIDC trust
  for workloads).
