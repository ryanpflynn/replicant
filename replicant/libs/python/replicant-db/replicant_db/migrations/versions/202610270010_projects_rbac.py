"""Projects, project-scoped role bindings and versioned project configuration (v0 CONFIG, DEVIATIONS CONFIG-D*).

* tenancy.projects: tenant -> project (ownership only; owners_group names the group that owns the project).
* tenancy.project_repos: repo -> project (one project per repo).
* tenancy.project_config_versions: versioned team configuration (schemas/project-config.schema.json), project level
  (scope_key '') or a repo override (scope_key = repo_id). Every change is a new version with actor and reason;
  rollback writes a new version that copies an old one.
* identity.role_bindings gains `project_id` and scope 'project' and the role `project_admin`; control-plane-api's
  tenancy module (svc_cpa_tenancy) may write role bindings (identity-service is not in v0; DEVIATIONS CONFIG-D3).

Revision ID: 202610270010_projects_rbac
Revises: 202610270001_ui_preferences
Create Date: 2026-10-27
"""
from __future__ import annotations

from replicant_db import owner_ddl

revision: str = "202610270010_projects_rbac"
down_revision: str | None = "202610270001_ui_preferences"
branch_labels = None
depends_on = None

replicant_schema: str = "multi"  # tenancy + identity
replicant_compat: str = "expand"
replicant_requires_reader_revision: str | None = None

UP = r"""
CREATE TABLE IF NOT EXISTS tenancy.projects (
    project_id   text COLLATE "C" PRIMARY KEY CONSTRAINT ck_projects_id CHECK (project_id ~ '^prj_[a-z0-9][a-z0-9-]{1,40}$'),
    tenant_id    text COLLATE "C" NOT NULL REFERENCES tenancy.tenants (tenant_id),
    name         text NOT NULL CONSTRAINT ck_projects_name CHECK (length(name) BETWEEN 1 AND 100),
    owners_group text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    created_by   text NOT NULL,
    archived_at  timestamptz
);
CREATE INDEX IF NOT EXISTS projects_by_tenant ON tenancy.projects (tenant_id);

CREATE TABLE IF NOT EXISTS tenancy.project_repos (
    repo_id    text COLLATE "C" PRIMARY KEY,                -- one project per repo
    project_id text COLLATE "C" NOT NULL REFERENCES tenancy.projects (project_id),
    tenant_id  text COLLATE "C" NOT NULL REFERENCES tenancy.tenants (tenant_id),
    provider   text NOT NULL DEFAULT 'gitea',
    host       text NOT NULL,
    full_name  text NOT NULL,
    added_at   timestamptz NOT NULL DEFAULT now(),
    added_by   text NOT NULL
);
CREATE INDEX IF NOT EXISTS project_repos_by_project ON tenancy.project_repos (project_id);

CREATE TABLE IF NOT EXISTS tenancy.project_config_versions (
    project_id text COLLATE "C" NOT NULL REFERENCES tenancy.projects (project_id),
    scope_key  text COLLATE "C" NOT NULL DEFAULT '',        -- '' = project level, else the repo_id of an override
    tenant_id  text COLLATE "C" NOT NULL REFERENCES tenancy.tenants (tenant_id),
    version    integer NOT NULL CONSTRAINT ck_project_config_versions_version CHECK (version >= 1),
    config     jsonb NOT NULL,                              -- schemas/project-config.schema.json
    is_current boolean NOT NULL DEFAULT true,
    actor      text NOT NULL,
    reason     text NOT NULL CONSTRAINT ck_project_config_versions_reason CHECK (length(reason) BETWEEN 1 AND 500),
    rolled_back_from integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, scope_key, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS project_config_current_uq ON tenancy.project_config_versions (project_id, scope_key)
    WHERE is_current;

SELECT ops.apply_tenant_rls('tenancy.projects');
SELECT ops.apply_tenant_rls('tenancy.project_repos');
SELECT ops.apply_tenant_rls('tenancy.project_config_versions');
GRANT SELECT ON tenancy.projects, tenancy.project_repos, tenancy.project_config_versions TO r_tenancy_read;
GRANT SELECT, INSERT, UPDATE ON tenancy.projects, tenancy.project_repos, tenancy.project_config_versions
    TO r_tenancy_write;
GRANT DELETE ON tenancy.project_repos TO r_tenancy_write;

-- identity.role_bindings: project scope and project_admin (identity.md §5 roles + v0 CONFIG).
ALTER TABLE identity.role_bindings ADD COLUMN IF NOT EXISTS project_id text;
ALTER TABLE identity.role_bindings DROP CONSTRAINT IF EXISTS role_bindings_role_check;
ALTER TABLE identity.role_bindings ADD CONSTRAINT role_bindings_role_check CHECK (role IN ('viewer', 'engineer',
    'approver', 'tenant_admin', 'project_admin', 'operator', 'release_operator', 'release_manager', 'security_signer',
    'security_responder', 'eval_curator', 'identity_admin', 'platform_admin'));
ALTER TABLE identity.role_bindings DROP CONSTRAINT IF EXISTS role_bindings_scope_check;
ALTER TABLE identity.role_bindings ADD CONSTRAINT role_bindings_scope_check CHECK (scope IN ('tenant', 'platform', 'project'));
ALTER TABLE identity.role_bindings DROP CONSTRAINT IF EXISTS role_bindings_check;
ALTER TABLE identity.role_bindings ADD CONSTRAINT role_bindings_check CHECK (
    ((scope = 'platform') = (tenant_id IS NULL)) AND ((scope = 'project') = (project_id IS NOT NULL)));
ALTER TABLE identity.role_bindings DROP CONSTRAINT IF EXISTS role_bindings_project_id_check;
ALTER TABLE identity.role_bindings ADD CONSTRAINT role_bindings_project_id_check CHECK (
    project_id IS NULL OR project_id ~ '^prj_[a-z0-9][a-z0-9-]{1,40}$');

-- control-plane-api's tenancy module writes bindings (v0: no identity-service). It sees every row.
GRANT SELECT, INSERT, UPDATE, DELETE ON identity.role_bindings, identity.role_binding_versions TO svc_cpa_tenancy;
GRANT USAGE ON SCHEMA identity TO svc_cpa_tenancy;
DROP POLICY IF EXISTS rls_role_bindings_cpa_tenancy ON identity.role_bindings;
CREATE POLICY rls_role_bindings_cpa_tenancy ON identity.role_bindings AS PERMISSIVE FOR ALL TO svc_cpa_tenancy
    USING (true) WITH CHECK (true);
-- the role resolver reads every binding (operator scope reads go through r_cross_tenant_read)
DROP POLICY IF EXISTS rls_role_bindings_cpa_read ON identity.role_bindings;
CREATE POLICY rls_role_bindings_cpa_read ON identity.role_bindings AS PERMISSIVE FOR SELECT TO svc_cpa_read
    USING (true);
"""

DOWN = r"""
DROP POLICY IF EXISTS rls_role_bindings_cpa_read ON identity.role_bindings;
DROP POLICY IF EXISTS rls_role_bindings_cpa_tenancy ON identity.role_bindings;
REVOKE SELECT, INSERT, UPDATE, DELETE ON identity.role_bindings, identity.role_binding_versions FROM svc_cpa_tenancy;
DELETE FROM identity.role_bindings WHERE scope = 'project' OR role = 'project_admin';
ALTER TABLE identity.role_bindings DROP CONSTRAINT IF EXISTS role_bindings_project_id_check;
ALTER TABLE identity.role_bindings DROP CONSTRAINT IF EXISTS role_bindings_check;
ALTER TABLE identity.role_bindings ADD CONSTRAINT role_bindings_check CHECK ((scope = 'tenant') = (tenant_id IS NOT NULL));
ALTER TABLE identity.role_bindings DROP CONSTRAINT IF EXISTS role_bindings_scope_check;
ALTER TABLE identity.role_bindings ADD CONSTRAINT role_bindings_scope_check CHECK (scope IN ('tenant', 'platform'));
ALTER TABLE identity.role_bindings DROP CONSTRAINT IF EXISTS role_bindings_role_check;
ALTER TABLE identity.role_bindings ADD CONSTRAINT role_bindings_role_check CHECK (role IN ('viewer', 'engineer',
    'approver', 'tenant_admin', 'operator', 'release_operator', 'release_manager', 'security_signer',
    'security_responder', 'eval_curator', 'identity_admin', 'platform_admin'));
ALTER TABLE identity.role_bindings DROP COLUMN IF EXISTS project_id;
DROP TABLE IF EXISTS tenancy.project_config_versions;
DROP TABLE IF EXISTS tenancy.project_repos;
DROP TABLE IF EXISTS tenancy.projects;
"""


def upgrade() -> None:
    owner_ddl.execute(UP)


def downgrade() -> None:
    owner_ddl.execute(DOWN)
