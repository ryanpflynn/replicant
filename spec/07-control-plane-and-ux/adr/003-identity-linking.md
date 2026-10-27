# ADR-07-003: Identity linking by provider proof, never by email alone

- **Status:** Accepted (WS-07, 2026-10-24)
- **Constraints:** D-18 (unlinked identities cannot approve or trigger), D-15

## Context

Replicant must map GitHub, Slack, Jira, and (locally) Gitea identities to Okta principals. Email
matching is spoofable or ambiguous (personal GitHub emails, shared aliases, Slack display emails).

## Options

1. **Email matching** (Slack `users.lookupByEmail`, GitHub commit emails). Zero friction, weak proof.
2. **Provider SCIM / SAML linkage.** GitHub Enterprise Cloud SAML external identities give an
   authoritative Okta NameID ↔ GitHub user mapping, readable by a GitHub App installation token with
   members read [verified: https://docs.github.com/en/graphql/reference/orgs, 2026-10-24]. Slack SCIM's
   user ID mapping is not documented as equal to the Slack user ID `[unverified]`.
3. **OAuth/OIDC link flows** (GitHub App user authorization with PKCE; Sign in with Slack; Atlassian 3LO;
   Gitea OAuth2): the user proves control of both accounts in one browser session.

## Decision

Use 2 where authoritative (GitHub SAML external identities, nightly and on membership events), otherwise 3.
Email may only *suggest* a link (Slack ephemeral "Link as Priya Raman" button), which still runs the
OIDC flow. One external identity links to at most one principal; conflicts go to identity admins.

## Consequences

- First-time Slack approvers click once to link; GitHub users in SAML orgs need nothing.
- Unlinked users get a one-time, rate-limited link prompt in the channel they used.

## Revisit when

- Okta provisions Slack and GitHub with a documented stable mapping to the provider user IDs.
