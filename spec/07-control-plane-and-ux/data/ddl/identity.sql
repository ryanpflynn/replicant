-- Schema identity. Writer: identity-service (svc_identity_service). DDL owner: WS-07 (WS-09 reviews).
-- SCIM-provisioned replica of Okta users/groups (Okta is upstream), identity links, role bindings, sessions.
CREATE SCHEMA IF NOT EXISTS identity;

CREATE TABLE identity.users (
    principal_id       text PRIMARY KEY CHECK (principal_id ~ '^usr_[0-9a-hjkmnp-tv-z]{26}$'),
    okta_sub           text NOT NULL UNIQUE,                 -- OIDC sub (Dex userID locally)
    user_name          text NOT NULL UNIQUE,                 -- SCIM userName (Okta login)
    display_name       text NOT NULL,
    email              text,                                 -- never used for authorization or linking proof
    timezone           text,
    manager_principal  text CHECK (manager_principal ~ '^usr_[0-9a-hjkmnp-tv-z]{26}$'),
    status             text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'deprovisioned')),
    scim_external_id   text,
    recheck_required_at timestamptz,                         -- set by Okta event hooks; privileged actions fail closed while set
    last_okta_sync_at  timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    deprovisioned_at   timestamptz,
    tombstone_until    timestamptz                           -- 7 years after deprovisioning (audit, C-STATE)
);

CREATE TABLE identity.service_principals (
    principal_id text PRIMARY KEY CHECK (principal_id ~ '^svc_[a-z][a-z0-9-]{2,40}$'),
    description  text NOT NULL,
    k8s_subject  text UNIQUE,                                -- system:serviceaccount:<ns>:<sa>
    allowed_actions text[] NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now(),
    disabled_at  timestamptz
);

CREATE TABLE identity.groups (
    group_id      text PRIMARY KEY,                          -- SCIM id we return to Okta (grp_<ulid>)
    okta_group_id text UNIQUE,                               -- externalId from Okta
    display_name  text NOT NULL UNIQUE,                      -- e.g. payments-approvers, replicant-admins
    created_at    timestamptz NOT NULL DEFAULT now(),
    deleted_at    timestamptz
);

CREATE TABLE identity.memberships (
    group_id     text NOT NULL REFERENCES identity.groups (group_id),
    principal_id text NOT NULL REFERENCES identity.users (principal_id),
    added_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, principal_id)
);
CREATE INDEX memberships_by_principal ON identity.memberships (principal_id);

-- Membership history (for post-hoc contested-decision checks, identity.md §10).
CREATE TABLE identity.membership_events (
    event_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_id     text NOT NULL,
    principal_id text NOT NULL,
    change       text NOT NULL CHECK (change IN ('added', 'removed')),
    source       text NOT NULL CHECK (source IN ('scim', 'okta_event_hook', 'okta_live_reconcile', 'dev_seed')),
    published_at timestamptz,                                -- Okta event published time when known
    recorded_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX membership_events_principal ON identity.membership_events (principal_id, recorded_at);

CREATE TABLE identity.okta_event_dedupe (
    event_id    text PRIMARY KEY,                            -- Okta eventId
    received_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE identity.links (
    link_id        text PRIMARY KEY,                         -- lnk_<ulid>
    principal_id   text NOT NULL REFERENCES identity.users (principal_id),
    kind           text NOT NULL CHECK (kind IN ('github', 'slack', 'jira', 'gitea')),
    namespace      text NOT NULL,                            -- github host | slack enterprise_id or team_id | jira site | gitea host
    external_id    text NOT NULL,                            -- GitHub numeric id, Slack user id, Jira accountId, Gitea id
    login          text,
    method         text NOT NULL CHECK (method IN ('saml_external_identity', 'oauth', 'oidc', 'scim', 'dev_seed')),
    status         text NOT NULL CHECK (status IN ('pending', 'active', 'suspended', 'revoked', 'rejected')),  -- canonical link status set (R1-175), used by API filters and schemas
    verified_at    timestamptz,
    revoked_at     timestamptz,
    revoked_reason text,
    created_at     timestamptz NOT NULL DEFAULT now()
);
-- An external identity links to at most one principal; a principal has at most one active link per kind+namespace.
CREATE UNIQUE INDEX links_external_active_uq ON identity.links (kind, namespace, external_id) WHERE status IN ('pending', 'active');
CREATE UNIQUE INDEX links_principal_active_uq ON identity.links (principal_id, kind, namespace) WHERE status = 'active';

CREATE TABLE identity.link_flows (
    state_hash   text PRIMARY KEY,                           -- sha256 of the OAuth state nonce
    principal_id text NOT NULL REFERENCES identity.users (principal_id),
    kind         text NOT NULL,
    pkce_verifier_enc bytea NOT NULL,                        -- KMS data-key encrypted
    expires_at   timestamptz NOT NULL,
    consumed_at  timestamptz
);

CREATE TABLE identity.role_bindings (
    version    integer NOT NULL CHECK (version >= 1),
    binding_id integer NOT NULL,
    group_name text NOT NULL,
    scope      text NOT NULL CHECK (scope IN ('tenant', 'platform')),
    tenant_id  text CHECK (tenant_id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$'),
    role       text NOT NULL CHECK (role IN ('viewer', 'engineer', 'approver', 'tenant_admin',
                                             'operator', 'release_operator', 'release_manager', 'security_signer',
                                             'security_responder', 'eval_curator', 'identity_admin', 'platform_admin')),
    -- 'replicant_admin' is deliberately not an allowed role: memory:admin comes only from the admin group (D-25)
    repo_globs text[],
    PRIMARY KEY (version, binding_id),
    CHECK ((scope = 'tenant') = (tenant_id IS NOT NULL))
);
CREATE TABLE identity.role_binding_versions (
    version             integer PRIMARY KEY,
    replicant_admin_group text NOT NULL DEFAULT 'replicant-admins',
    is_current          boolean NOT NULL DEFAULT false,
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          text NOT NULL,
    reason              text
);
CREATE UNIQUE INDEX role_binding_versions_current_uq ON identity.role_binding_versions (is_current) WHERE is_current;

CREATE TABLE identity.delegations (
    delegation_id  text PRIMARY KEY CHECK (delegation_id ~ '^dlg_[0-9a-hjkmnp-tv-z]{26}$'),
    from_principal text NOT NULL REFERENCES identity.users (principal_id),
    to_principal   text NOT NULL REFERENCES identity.users (principal_id),   -- may be non-eligible (R2-025); repo permission still checked at decision
    tenants        text[] NOT NULL DEFAULT '{}',
    starts_at      timestamptz NOT NULL,
    ends_at        timestamptz NOT NULL,
    reason         text,
    ended_at       timestamptz,
    CHECK (from_principal <> to_principal),
    CHECK (ends_at > starts_at AND ends_at <= starts_at + interval '21 days')   -- one maximum (R1-178); written via identity-service only
);
CREATE INDEX delegations_active ON identity.delegations (to_principal, starts_at, ends_at) WHERE ended_at IS NULL;

CREATE TABLE identity.sessions (
    session_id        text PRIMARY KEY,                      -- 256-bit random, base64url; cookie carries id + HMAC
    principal_id      text NOT NULL REFERENCES identity.users (principal_id),
    okta_sid          text,
    refresh_token_enc bytea,
    user_agent_hash   text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    last_seen_at      timestamptz NOT NULL DEFAULT now(),
    idle_expires_at   timestamptz NOT NULL,
    absolute_expires_at timestamptz NOT NULL,
    revoked_at        timestamptz,
    revoked_reason    text
);
CREATE INDEX sessions_by_principal ON identity.sessions (principal_id) WHERE revoked_at IS NULL;

-- Outbox of principal changes (identity.md §3.2), relayed to control-plane-api.
CREATE TABLE identity.principal_events (
    notice_id    text PRIMARY KEY CHECK (notice_id ~ '^ntc_[0-9a-hjkmnp-tv-z]{26}$'),
    principal_id text NOT NULL,
    change       text NOT NULL CHECK (change IN ('deprovisioned', 'suspended', 'groups_changed', 'identity_link_removed', 'decision_identity_contested')),
    decision_id  text,                                           -- set for decision_identity_contested (CCR-07-06)
    effective_at timestamptz NOT NULL,
    relayed_at   timestamptz,
    attempts     integer NOT NULL DEFAULT 0
);
CREATE INDEX principal_events_pending ON identity.principal_events (effective_at) WHERE relayed_at IS NULL;

-- role_bindings carries tenant_id (NULL for platform-scope bindings): RLS enabled and forced
-- (XD-FIX-R1-160). Platform rows are readable by everyone who can read the table; identity-service, the only
-- writer and the evaluator for every tenant, sees all rows.
SELECT ops.apply_tenant_rls('identity.role_bindings');
CREATE POLICY rls_role_bindings_platform_read ON identity.role_bindings AS PERMISSIVE FOR SELECT TO PUBLIC
    USING (tenant_id IS NULL);
CREATE POLICY rls_role_bindings_sole_writer ON identity.role_bindings AS PERMISSIVE FOR ALL TO svc_identity_service
    USING (true) WITH CHECK (true);

-- Grant model v2 (XD-FIX-R1-160, C-PG 2.0.0): WS-10 owns roles, logins and schema USAGE; this file grants on
-- parent tables only (never ON ALL TABLES, never on a partition, no default privileges): reads to
-- r_<schema>_read, DML to r_<schema>_write, column-narrowed reads to a named login. Never to the login
-- svc_control_plane_api (its modules inherit r_<schema>_write; svc_cpa_read inherits the read roles).
GRANT SELECT, INSERT, UPDATE, DELETE ON identity.users, identity.groups, identity.memberships, identity.membership_events,
    identity.links, identity.link_flows, identity.role_bindings, identity.role_binding_versions, identity.service_principals,
    identity.sessions, identity.delegations, identity.okta_event_dedupe, identity.principal_events TO r_identity_write;
-- Readers (control-plane-api evaluates replicant-authz in process, I-4; integrations resolve linked actors):
-- never sessions' secrets, link flows (PKCE verifiers), dedupe or event queues.
GRANT SELECT ON identity.users, identity.service_principals, identity.groups, identity.memberships,
                identity.links, identity.role_bindings, identity.role_binding_versions, identity.delegations
             TO r_identity_read;
GRANT SELECT (session_id, principal_id, idle_expires_at, absolute_expires_at, revoked_at) ON identity.sessions TO svc_cpa_read;
