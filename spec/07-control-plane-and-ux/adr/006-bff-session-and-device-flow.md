# ADR-07-006: Browser sessions via a BFF; CLI via OAuth device authorization grant

- **Status:** Accepted (WS-07, 2026-10-24)
- **Constraints:** D-18, D-21 (credentials assumed exfiltratable), D-25 (no cached grants)

## Context

The SPA needs Okta login; the CLI needs login on laptops and headless boxes.

## Options

1. SPA holds Okta tokens (auth code + PKCE in the browser). Tokens in JavaScript reach any XSS.
2. **BFF**: control-plane-api is a confidential OIDC client; the browser gets an `HttpOnly; Secure;
   SameSite=Lax` `__Host-` cookie with an opaque session ID; refresh tokens stay server-side encrypted.
3. CLI: loopback-redirect auth code + PKCE vs **device authorization grant** (Okta native app type;
   verified: https://developer.okta.com/docs/guides/device-authorization-grant/main/, 2026-10-24).

## Decision

BFF for the browser; device grant for the CLI (works over SSH and in containers). Authorization is
evaluated per request from the SCIM replica (plus live Okta for decisions), so deprovisioning takes effect
on the next request regardless of token or session lifetime.

## Consequences

- CSRF protection needed for cookie auth (SameSite plus double-submit header).
- The API is stateful for sessions (rows in `identity.sessions`, 30 s in-process cache with NOTIFY eviction).

## Revisit when

- Okta Universal Logout / token revocation hooks become the enterprise standard for session termination.
