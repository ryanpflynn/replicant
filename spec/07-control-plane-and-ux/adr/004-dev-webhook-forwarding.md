# ADR-07-004: Dev GitHub App webhooks via Cloudflare quick tunnel (overrides WS-11's provisional smee default)

- **Status:** Accepted (WS-07, 2026-10-24). WS-07 owns this decision (C-ARCH §1.4); supersedes the
  provisional default in WS-11 ADR-11-006 / XD-11-07.
- **Constraints:** D-24

## Context

The dev App must deliver real GitHub App webhooks to a laptop, and github-integration verifies
`X-Hub-Signature-256` over the **raw body** with no bypass (WS-11 agrees there is no "skip signature" switch).

Facts: `gh webhook forward` only works with repository and organization webhooks, one forwarder per repo
or org [verified: https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/using-the-github-cli-to-forward-webhooks-for-testing, 2026-10-24].
`smee-client` 5.0.0 re-serializes the payload (`const body = JSON.stringify(data.body)` in `index.js`)
[verified: npm package source, 2026-10-24], so signature verification passes only if GitHub's original
bytes equal `JSON.stringify` output `[unverified for all payloads]`; GitHub says not to use Smee in
production [verified: GitHub App tutorial, 2026-10-24]. Cloudflare quick tunnels need no account, proxy
HTTP unchanged, change hostname on each start, allow 200 in-flight requests, and are for testing only
[verified: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/, 2026-10-24].
The App's webhook URL can be updated with `PATCH /app/hook/config` (JWT) [verified: https://docs.github.com/en/rest/apps/webhooks, 2026-10-24].

## Options

1. smee.io + smee-client: no account; body re-serialized (signature risk); channel URL is a bearer capability.
2. **Cloudflare quick tunnel** + automatic `PATCH /app/hook/config` on each start.
3. Cloudflare named tunnel or ngrok static domain: stable URL; requires an account.
4. `gh webhook forward`: not applicable to App webhooks.

## Decision

Option 2 by default (`replicant dev github-app forward`), option 3 as supported alternatives
(`--forwarder named|ngrok`), smee only as a last-resort opt-in (`--forwarder smee`) that `Replicant dev
github-app doctor` tests by sending a ping and reporting whether the signature verified.

## Consequences

- WS-11 changes the `github-dev` compose profile's `webhook-forwarder` service to run `cloudflared`
  (filed in cross-ws-dependencies).
- The laptop port is reachable from the internet while the tunnel runs; only github-integration's
  `/github/webhook` route is exposed through it, and the dev App is installed only on the developer's
  personal org.

## Revisit when

- GitHub supports App webhooks in `gh webhook forward`, or smee preserves raw bodies.
