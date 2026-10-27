# ADR-02-006: repo-mirror implementation

- **Status:** Accepted (WS-02, 2026-10-24); ADR-011 defines its trust position.
- **Constraints:** D-21, D-24; ADR-011; sandbox-ready p95 < 90 s

## Context

Sandboxes fetch exact commits quickly without GitHub credentials. The mirror holds read-only
installation tokens from github-token-broker and authorizes sandboxes by run token `repo_ids`.
WS-06 adds a Forgejo eval Git server as a second upstream.

## Options

1. Gitaly or a full Git hosting product as a cache.
2. **Go service wrapping `git http-backend` (smart HTTP, protocol v2) over bare repos on local or
   gp3 storage (chosen).**
3. Pre-built git bundles in S3 per repo.

## Decision

Option 2: upload-pack only (`git-receive-pack` is rejected with 403), `uploadpack.allowFilter`,
`uploadpack.allowAnySHA1InWant`, bitmaps and commit-graph, on-demand fetch of missing SHAs under a
per-repo lock, sharded StatefulSet with two copies per repo, upstream selected by
`repo.provider`/`repo.host`, and tenant checks for eval repositories.

## Consequences

- Blobless partial fetches of exact SHAs in seconds; lazy blob fetches go through agentd.
- The mirror is a dependency: outage → `RepoFetchFailed` (retryable).

## Revisit when

- Monorepos exceed the prep budget (add bundle-URI for those), or GitHub offers workload-identity
  downscoped tokens (ADR-011).
