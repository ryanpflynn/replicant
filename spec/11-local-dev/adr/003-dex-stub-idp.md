# ADR-11-003: Dex is the local stand-in for Okta OIDC; SCIM is emulated by the seeder

- **Status:** Accepted (WS-11, 2026-10-24)
- **Constraints:** D-18, D-25 (admin group enforced server-side), D-26

## Context

Locally we need OIDC login for the control plane, Temporal UI, and Grafana; a `groups` claim to
exercise D-25 (`replicant-admins`) and tenant roles; a device flow for the `replicant` CLI; and SCIM
provisioning, because identity-service's user/group tables are a SCIM-provisioned replica (C-STATE §1.4)
and D-25 says membership changes take effect on the next request.

Verified facts:
- Dex `v2.46.0` (2026-10-07) static password users have a `groups` field (`storage.Password.Groups`),
  and the server includes the device-flow handler. [verified:
  https://github.com/dexidp/dex/blob/v2.46.0/storage/storage.go and server/server.go, 2026-10-24]
- Keycloak `26.7.5` (2026-09-30) supports groups mappers, device flow and token exchange, but uses
  roughly 0.5–1 GB RAM in dev mode `[unverified: typical footprint; not measured]`.
- Neither pushes SCIM to an arbitrary SCIM server out of the box `[unverified for Keycloak 26.7]`.

## Options

1. **Dex (chosen).** ~30 MB, config-file driven, static users with groups, device flow.
2. **Keycloak.** Closer to a real enterprise IdP (admin console, group management UI, token exchange),
   heavier, realm JSON to maintain.

## Decision

Dex. SCIM is emulated by `replicant dev seed`, which calls identity-service's SCIM 2.0 endpoints
(`POST /scim/v2/Users`, `POST /scim/v2/Groups`, `PATCH /scim/v2/Groups/{id}`) with the local SCIM bearer
token, exactly as Okta would, using the same user IDs as Dex's `userID` (the OIDC `sub`).
`replicant dev idp set-groups <user> <groups…>` changes both sides in one step: it rewrites Dex's static
config (and restarts Dex) and sends the SCIM PATCH. That is how walkthrough 7 demonstrates
"group change takes effect on the next request".

## Consequences

- OIDC claims and SCIM data can disagree locally if someone edits one side by hand. `replicant dev doctor`
  compares them.
- Keycloak remains a documented alternative (`REPLICANT_IDP=keycloak`, a compose override file) for teams
  that need token-exchange testing (WS-07 CLI token exchange).

## Revisit when

- WS-07 requires OAuth 2.0 Token Exchange for the CLI, which Dex does not provide `[unverified: Dex token
  exchange support beyond its connector-token exchange]`.
