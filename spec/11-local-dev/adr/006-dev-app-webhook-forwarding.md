# ADR-11-006: Dev GitHub App webhooks reach the laptop through smee.io (provisional; WS-07 decides)

- **Status:** **Superseded** by WS-07 [ADR-07-004](../../07-control-plane-and-ux/adr/004-dev-webhook-forwarding.md)
  (Cloudflare quick tunnel default; smee only as an opt-in last resort). Kept for history. The compose profile
  `github-dev` now runs `cloudflare/cloudflared:2026.10.3` behind `webhook-ingress`. The earlier smee-client version
  pin (4.4.3) is withdrawn: npm `latest` is 5.0.0, which WS-07 verified re-serializes the payload.
- **Constraints:** D-24

## Context

D-24 names smee.io, `gh webhook forward`, and Cloudflare/ngrok tunnels as candidates.

- `gh webhook forward` works **only with repository and organization webhooks**, not GitHub App
  webhooks, and only one person at a time per repository or organization. [verified:
  https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/using-the-github-cli-to-forward-webhooks-for-testing,
  2026-10-24]. Replicant consumes **App** webhooks (installation events, App-scoped deliveries, the
  App delivery-redelivery API), so it is disqualified for the primary path.
- GitHub's own GitHub App tutorial uses smee.io: `npx smee -u WEBHOOK_PROXY_URL -t
  http://localhost:3000/api/webhook`, and says not to use it in production. [verified:
  https://docs.github.com/en/apps/creating-github-apps/writing-code-for-a-github-app/building-a-github-app-that-responds-to-webhook-events,
  2026-10-24]. Current `smee-client` is v4.4.3. [verified: GitHub releases redirect, 2026-10-24]
- smee-client re-serializes the body (WS-07 ADR-07-004 [verified: npm package source, 2026-10-24]), so
  `X-Hub-Signature-256` verifies only when GitHub's bytes equal `JSON.stringify` output; this is why ADR-07-004
  made the Cloudflare quick tunnel the default.
- A smee channel URL is a bearer capability: anyone with it can read the payloads.

## Options

1. **smee.io + smee-client (chosen as default).** No account, works behind NAT.
2. **Cloudflare quick tunnel** (`cloudflared tunnel --url http://localhost:<port>`, cloudflared
   2026.10.3). Preserves the raw body (plain HTTP reverse proxy); exposes a local port to the internet
   for the tunnel's lifetime. Quick tunnels need no account [verified in WS-07 ADR-07-004:
   https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/, 2026-10-24].
3. `gh webhook forward`: rejected (no App webhooks).

## Decision

Default smee (compose profile `github-dev`, service `webhook-forwarder`). github-integration verifies
the signature exactly as in production; there is **no** "skip signature" switch. If the signature fails
through smee, `replicant dev github-app doctor` detects it (it sends a ping through the channel and checks
the verification result) and tells the developer to switch to `--forwarder cloudflared`, which starts
a cloudflared container instead and updates the App's webhook URL through the App's
`PATCH /app/hook/config` endpoint (JWT) [verified in WS-07 ADR-07-004: https://docs.github.com/en/rest/apps/webhooks, 2026-10-24].

The dev App is installed only on the developer's personal org with sample repos, so payloads crossing
smee contain no company data.

## Revisit when

- WS-07 picks a different forwarder, or GitHub adds App-webhook support to `gh webhook forward`.
