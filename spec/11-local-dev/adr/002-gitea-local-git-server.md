# ADR-11-002: Gitea is the local Git server; real GitHub is exercised through the dev GitHub App

- **Status:** Accepted (WS-11, 2026-10-24)
- **Constraints:** D-15, D-19, D-20, D-24; C-JOBREQ (`repo.provider` enum `github | gitea`), C-MSG
  (`PrEventSignal.delivery_id` mentions Gitea)

## Context

Offline tests need repositories, pull requests, reviews (approve / request changes / comment), inline
review comments, issue comments, webhooks with HMAC signatures, branch protection, and a
collaborator-permission API. The contracts already name `gitea` as the only non-GitHub provider.

Verified facts (Gitea source, `main` and tag `v28.1.0`, checked 2026-10-24):

- Gitea sends `X-Gitea-Delivery`, `X-Gitea-Event`, `X-Gitea-Event-Type`, `X-Gitea-Signature` (hex
  HMAC-SHA256) **and** GitHub-compatible `X-GitHub-Delivery`, `X-GitHub-Event`, `X-Hub-Signature-256:
  sha256=<hex>`. [verified: https://github.com/go-gitea/gitea/blob/main/services/webhook/deliver.go]
- The `X-GitHub-Event` value is Gitea's own event name, not GitHub's: review approvals arrive as
  `pull_request_approved`, rejections as `pull_request_rejected`, review comments as
  `pull_request_comment`, PR comments as `issue_comment`. Payload shapes also differ from GitHub's.
  [verified: https://github.com/go-gitea/gitea/blob/main/modules/webhook/type.go]
- Event types include `pull_request`, `pull_request_sync`, `pull_request_review_approved`,
  `pull_request_review_rejected`, `pull_request_review_comment`, `pull_request_comment`,
  `pull_request_review_request`, `issues`, `issue_comment`. [verified: same file]
- API: `POST /repos/{owner}/{repo}/pulls`, `POST /repos/{owner}/{repo}/pulls/{index}/reviews`
  (`event`, `body`, `commit_id`, `comments[{path, body, new_position, old_position}]`),
  `GET /repos/{owner}/{repo}/collaborators/{collaborator}/permission`, `POST /admin/users`,
  `POST /users/{username}/tokens` (`name`, `scopes` only: **no expiry**, scopes are not per repository),
  `DELETE /users/{username}/tokens/{token}`, `POST /orgs/{org}/hooks`,
  `POST /repos/{owner}/{repo}/branch_protections` (`required_approvals`, `block_on_rejected_reviews`, …).
  [verified: https://gitea.com/swagger.v1.json (API 29.0.0+dev), 2026-10-24]
- Webhooks to private addresses need `[security] ALLOWED_HOST_LIST` to include `private` in 28.x.
  [verified: https://github.com/go-gitea/gitea/blob/v28.1.0/custom/conf/app.example.ini]
- Image: `gitea/gitea:28.1.0-rootless` (2026-10-06). [verified: Docker Hub tag API, 2026-10-24]

## Options

1. **A replicant-built GitHub API emulator.** Same adapter as production, but a large surface to emulate
   faithfully, and it would drift from GitHub; bugs in the emulator would mask bugs in the adapter.
2. **Gitea (chosen).** Real Git hosting with PRs and reviews. Needs a provider adapter in
   github-integration, publisher-worker, identity-service, and github-token-broker.
3. **Forgejo.** API-compatible fork of Gitea today, diverging over time. The contracts say `gitea`.

## Decision

Gitea, behind an `ScmProvider` interface with two implementations (`github`, `gitea`) in the shared
Python package `replicant-scm` (WS-07 owns it; XD-11-05). The Gitea adapter normalizes Gitea payloads into
the same internal event model the GitHub adapter produces, so everything downstream (JobRequest,
`pr.event` Signal, gate decisions, publisher) is provider-agnostic. Real GitHub is covered by the dev
GitHub App path (walkthrough 11) and by recorded GitHub webhook fixtures in contract tests.

## Consequences

- The adapter layer itself is not production code on the local path. parity.md P-10 rates this
  **high risk** and lists the compensating tests: recorded-GitHub-fixture contract tests run in CI on
  every change to github-integration, and walkthrough 11 is part of the release checklist.
- Gitea tokens cannot be scoped to one repository or expire. The broker deletes them after one hour
  (parity.md P-12).
- Gitea identities use `channel_identity.kind = gitea` (CCR-11-01, applied in C-JOBREQ 1.1.0 and C-MSG 1.1.0).

## Revisit when

- Forgejo or Gitea ship GitHub-identical event names and payloads, or GitHub provides an official local
  emulator.
