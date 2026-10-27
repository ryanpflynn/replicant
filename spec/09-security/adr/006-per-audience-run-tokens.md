# ADR-09-006: One run token per audience

- **Status:** Accepted; applied in C-RUNNER 2.x (final 2.5.2). Was CCR-09-01 (C-RUNNER `RunTokenClaims.aud`
  becomes a single string; `RunTokenResponse` returns a token set; C-IDS §7 adds
  `REPLICANT_RUN_TOKEN_DIR`).
- **Constraints:** D-11, D-21

## Context

`RunTokenClaims.aud` is an array of `ai-gateway`, `memory-mcp`, `context-mcp`, `repo-mirror`,
`runner-sandbox`, and the sandbox listener returns one token. The same bearer token is therefore
presented to every service. One of them, the internal AI gateway, is **outside Replicant's trust
boundary** (another team's platform, and possibly a third-party proxy). Anyone who can read requests
at the gateway (its operators, a gateway compromise, a misconfigured request log) can replay the
token to repo-mirror and the MCP servers for up to 60 minutes and read the tenant's code and memory
the run could read.

## Options

1. Keep one multi-audience token; rely on short TTL. Leaves T-CRED-02 at L3/I3.
2. **One token per audience** (chosen).
3. Sender-constrained tokens (DPoP or mTLS-bound). Strongest, but the CLIs talk to the gateway
   through their own HTTP clients and cannot do DPoP `[unverified: CLI support for DPoP; assumed
   absent]`.

## Decision

Option 2:

- `RunTokenClaims.aud` is a single string. The Runner mints the set of tokens the grant lists
  (`audiences`) at issuance and at each refresh.
- `POST /sandbox/v1/token:refresh` returns `{"tokens": {"<audience>": {"token": "…", "expires_at": "…"}}}`.
- The controller projects the set into the pod as files: `/run/replicant/tokens/<audience>`;
  `REPLICANT_RUN_TOKEN_DIR=/run/replicant/tokens` replaces `REPLICANT_RUN_TOKEN_FILE` (which remains, during
  the N/N+1 window, pointing at the `runner-sandbox` token).
- Each verifier requires `aud` == its own name.

## Consequences

- A token seen by the gateway is useless at repo-mirror and MCP.
- Five KMS signatures per issuance/refresh instead of one: about 150 runs × 5 / 45 min ≈ 0.3 RPS,
  far under the 1,000 RPS ECC quota [verified: https://docs.aws.amazon.com/kms/latest/developerguide/requests-per-second.html, 2026-10-24].
- Contract change (CC-4): N and N+1 coexist; verifiers accept `aud` as array-containing-self during
  the window, then string-only.

## Revisit when

- The CLIs support sender-constrained tokens to custom base URLs.
