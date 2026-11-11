"""inventory: repo profiles, the fleet catalogue, admin decisions, job observations, readiness and support requests
(design §8c, plan ONBOARD Phase 1.3 and 4.1, contract docs/design/onboarding-interfaces.md).

control-plane-api is the only writer (module role svc_cpa_tenancy); github-integration and the worker reach it
through control-plane-api's internal endpoints. Reads go through svc_cpa_read (operator scope for admin reads).

* inventory.repo_profiles   one row per (repo_id, sha) the scanner profiled; `is_current` marks the latest per repo.
                            `profile` is the contract §1 document (names, hashes, parsed commands; no contents),
                            `environment` the §2 inference, `env_digest` the hash of environment + commands.
* inventory.catalog         fleet-wide items (kind, identity) with their class and class default (not tenant data:
                            identities are package names, URL origins, command hashes; no RLS, like web.*).
* inventory.repo_items      which repos currently carry which item (from their current profile).
* inventory.decisions       admin approve / deny / map per item; the latest row is current.
* inventory.observations    what each job loaded, skipped or failed (worker, readiness probes).
* inventory.readiness       per repo: pending | probing | ready | warnings | blocked, the probe job and causes.
* inventory.support_requests project admins asking the platform for an item.

Revision ID: 202611100010_inventory
Revises: 202610300010_memory_trust
Create Date: 2026-11-10
"""
from __future__ import annotations

from replicant_db import owner_ddl

revision: str = "202611100010_inventory"
down_revision: str | None = "202610300010_memory_trust"
branch_labels = None
depends_on = None

replicant_schema: str = "multi"  # new schema inventory + its roles; ck_migration_log_schema has no "inventory"
replicant_compat: str = "expand"
replicant_requires_reader_revision: str | None = None

ULID = "[0-9a-hjkmnp-tv-z]{26}"

UP = rf"""
CREATE SCHEMA IF NOT EXISTS inventory AUTHORIZATION replicant_migrator;
REVOKE ALL ON SCHEMA inventory FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE replicant_migrator IN SCHEMA inventory REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'r_inventory_read') THEN CREATE ROLE r_inventory_read NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'r_inventory_write') THEN CREATE ROLE r_inventory_write NOLOGIN; END IF;
END
$$;
GRANT USAGE ON SCHEMA inventory TO r_inventory_read, r_inventory_write, svc_cpa_read, svc_cpa_tenancy;

CREATE TABLE IF NOT EXISTS inventory.repo_profiles (
    profile_id      text COLLATE "C" PRIMARY KEY CONSTRAINT ck_repo_profiles_id CHECK (profile_id ~ '^rp_{ULID}$'),
    tenant_id       text COLLATE "C" NOT NULL,
    repo_id         text COLLATE "C" NOT NULL,
    full_name       text NOT NULL,
    default_branch  text NOT NULL,
    sha             text NOT NULL CONSTRAINT ck_repo_profiles_sha CHECK (sha ~ '^[0-9a-f]{{40}}([0-9a-f]{{24}})?$'),
    scanner_version integer NOT NULL,
    scanned_at      timestamptz NOT NULL,
    profile         jsonb NOT NULL,
    environment     jsonb NOT NULL,
    env_digest      text NOT NULL,
    is_current      boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_repo_profiles_sha UNIQUE (repo_id, sha)
);
CREATE UNIQUE INDEX IF NOT EXISTS repo_profiles_current_uq ON inventory.repo_profiles (repo_id) WHERE is_current;
CREATE INDEX IF NOT EXISTS repo_profiles_by_tenant ON inventory.repo_profiles (tenant_id, repo_id);

CREATE TABLE IF NOT EXISTS inventory.catalog (
    item_id        text COLLATE "C" PRIMARY KEY CONSTRAINT ck_catalog_id CHECK (item_id ~ '^inv_{ULID}$'),
    kind           text NOT NULL CONSTRAINT ck_catalog_kind CHECK (kind IN ('mcp_server', 'hook', 'toolchain',
                                                                         'probe_failure')),
    identity       text NOT NULL CONSTRAINT ck_catalog_identity CHECK (length(identity) BETWEEN 1 AND 400),
    class          text NOT NULL,
    default_action text NOT NULL CONSTRAINT ck_catalog_default CHECK (default_action IN ('allow', 'skip', 'map',
                                                                                       'warn', 'block')),
    details        jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    first_seen     timestamptz NOT NULL DEFAULT now(),
    last_seen      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_catalog_kind_identity UNIQUE (kind, identity)
);
CREATE INDEX IF NOT EXISTS catalog_by_kind ON inventory.catalog (kind, last_seen DESC);

CREATE TABLE IF NOT EXISTS inventory.repo_items (
    tenant_id  text COLLATE "C" NOT NULL,
    repo_id    text COLLATE "C" NOT NULL,
    item_id    text COLLATE "C" NOT NULL REFERENCES inventory.catalog (item_id),
    profile_id text COLLATE "C" REFERENCES inventory.repo_profiles (profile_id),
    seen_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo_id, item_id)
);
CREATE INDEX IF NOT EXISTS repo_items_by_item ON inventory.repo_items (item_id);

CREATE TABLE IF NOT EXISTS inventory.decisions (
    decision_id text COLLATE "C" PRIMARY KEY CONSTRAINT ck_decisions_id CHECK (decision_id ~ '^ivd_{ULID}$'),
    item_id     text COLLATE "C" NOT NULL REFERENCES inventory.catalog (item_id),
    decision    text NOT NULL CONSTRAINT ck_decisions_decision CHECK (decision IN ('approve', 'deny', 'map')),
    map_to      text CONSTRAINT ck_decisions_map CHECK ((decision = 'map') = (map_to IS NOT NULL)),
    reason      text NOT NULL CONSTRAINT ck_decisions_reason CHECK (length(reason) BETWEEN 1 AND 1000),
    actor       text NOT NULL,
    audit_id    text,
    is_current  boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS decisions_current_uq ON inventory.decisions (item_id) WHERE is_current;

CREATE TABLE IF NOT EXISTS inventory.observations (
    observation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id      text COLLATE "C" NOT NULL,
    job_id         text COLLATE "C" NOT NULL CONSTRAINT ck_observations_job CHECK (job_id ~ '^job_{ULID}$'),
    run_id         text COLLATE "C" NOT NULL DEFAULT '',
    repo_id        text COLLATE "C" NOT NULL,
    workflow_type  text,
    role           text,
    item_id        text COLLATE "C" NOT NULL REFERENCES inventory.catalog (item_id),
    status         text NOT NULL CONSTRAINT ck_observations_status CHECK (status IN ('loaded', 'skipped', 'failed')),
    cause          text,
    tier           text,
    model          text,
    settings_hash  text,
    observed_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_observations UNIQUE (job_id, run_id, item_id)
);
CREATE INDEX IF NOT EXISTS observations_by_item ON inventory.observations (item_id, job_id);
CREATE INDEX IF NOT EXISTS observations_by_repo ON inventory.observations (repo_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS inventory.readiness (
    repo_id            text COLLATE "C" PRIMARY KEY,
    tenant_id          text COLLATE "C" NOT NULL,
    profile_id         text COLLATE "C" REFERENCES inventory.repo_profiles (profile_id),
    env_digest         text,
    status             text NOT NULL CONSTRAINT ck_readiness_status CHECK (status IN ('pending', 'probing', 'ready',
                                                                                     'warnings', 'blocked')),
    probe_job_id       text COLLATE "C",
    probe_requested_at timestamptz,
    attempts           integer NOT NULL DEFAULT 0,
    next_attempt_at    timestamptz NOT NULL DEFAULT now(),
    last_error         text,
    causes             jsonb NOT NULL DEFAULT '[]'::jsonb,
    probed_at          timestamptz,
    updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS readiness_pending ON inventory.readiness (next_attempt_at) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS inventory.support_requests (
    request_id text COLLATE "C" PRIMARY KEY CONSTRAINT ck_support_requests_id CHECK (request_id ~ '^srq_{ULID}$'),
    tenant_id  text COLLATE "C" NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    item_id    text COLLATE "C" NOT NULL REFERENCES inventory.catalog (item_id),
    reason     text NOT NULL CONSTRAINT ck_support_requests_reason CHECK (length(reason) BETWEEN 1 AND 2000),
    actor      text NOT NULL,
    status     text NOT NULL DEFAULT 'open' CONSTRAINT ck_support_requests_status CHECK (status IN ('open', 'resolved')),
    audit_id   text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS support_requests_by_item ON inventory.support_requests (item_id, created_at DESC);

SELECT ops.apply_tenant_rls('inventory.repo_profiles');
SELECT ops.apply_tenant_rls('inventory.repo_items');
SELECT ops.apply_tenant_rls('inventory.observations');
SELECT ops.apply_tenant_rls('inventory.readiness');
SELECT ops.apply_tenant_rls('inventory.support_requests');

GRANT SELECT ON ALL TABLES IN SCHEMA inventory TO r_inventory_read;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA inventory TO r_inventory_write;
GRANT DELETE ON inventory.repo_items TO r_inventory_write;
GRANT r_inventory_read TO svc_cpa_read WITH INHERIT TRUE;
GRANT r_inventory_write TO svc_cpa_tenancy WITH INHERIT TRUE;
"""

DOWN = """
DROP TABLE IF EXISTS inventory.support_requests;
DROP TABLE IF EXISTS inventory.readiness;
DROP TABLE IF EXISTS inventory.observations;
DROP TABLE IF EXISTS inventory.decisions;
DROP TABLE IF EXISTS inventory.repo_items;
DROP TABLE IF EXISTS inventory.catalog;
DROP TABLE IF EXISTS inventory.repo_profiles;
DROP SCHEMA IF EXISTS inventory;
"""


def upgrade() -> None:
    owner_ddl.execute(UP)


def downgrade() -> None:
    owner_ddl.execute(DOWN)
