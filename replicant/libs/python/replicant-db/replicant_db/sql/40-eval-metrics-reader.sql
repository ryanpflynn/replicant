-- =============================================================================
-- WS-10 · eval-worker's cross-tenant reads (CCR-06-15, accepted with constraint; XD-FIX-R1-285)
-- =============================================================================
-- Ruling: eval-worker may read jobs_read, intake (job_requests), approvals and cost across tenants
-- ONLY under the system:all-tenants scope, ONLY IDs, status, metrics and digests (no free-text column),
-- and EVERY read is audited. Implemented as nine read functions in `ops`:
--   * SECURITY DEFINER, owned by the NOLOGIN role eval_metrics_reader, which holds column-level SELECT
--     on exactly the allowlisted columns below and nothing else (no title, request, snapshot, url, repo,
--     requester, packet or decision text);
--   * the function pins replicant.principal_scope = 'system:all-tenants' for its own duration (function SET
--     clause: restored on return), and the owner holds r_cross_tenant_write, so the canonical
--     rls_*_xt_write policies admit the read; nothing else is widened;
--   * every call emits one audit event `data.cross_tenant_read` through audit.emit (details: function,
--     window, row count). If the emit fails, the call fails: no unaudited read;
--   * windows are capped at 46 days (the online-metrics horizon is 45 days) and 200,000 rows.
-- Wave 2 (XD-W2-06-01): ops.eval_job_runs, ops.eval_job_memory_usage, ops.eval_job_outcomes; PR timestamps in
-- ops.eval_job_prs. EXECUTE goes to svc_eval_worker only. svc_eval_worker holds no USAGE on these four schemas.
-- Applied by the migrate image AFTER the owners' DDL (the columns must exist), as replicant_migrator.
-- This is WS-10's second documented table-privilege exception (the first is bg_readonly); tools/check_grants.py
-- verifies that eval_metrics_reader holds only column privileges, only on these columns.
-- Validated on PostgreSQL 17 + pgvector 0.8.2 (tools/validate.sh, 2026-10-24).
-- =============================================================================
\set ON_ERROR_STOP on
SET ROLE replicant_migrator;

-- Column allowlist (IDs, status, metrics, digests, timestamps). Keep in sync with tools/check_grants.py EVAL_COLUMNS.
GRANT SELECT (job_id, tenant_id, workflow_type, status, is_terminal, phase, evaluation_mode, release_bundle_id,
              release_cohort, migrated, worker_build_id, memory_snapshot_id, cycle, repair_used, repair_max,
              risk_level, terminal_outcome, terminal_at, created_at)
  ON jobs_read.jobs TO eval_metrics_reader;
GRANT SELECT (job_id, tenant_id, repo_id, pr_number, state, job_open, opened_at, merged_at, closed_at)
  ON jobs_read.job_prs TO eval_metrics_reader;
GRANT SELECT (run_id, job_id, tenant_id, node_key, cycle, ordinal, role, phase, reason, result_outcome, qa_verdict,
              started_at, finished_at)
  ON jobs_read.job_runs TO eval_metrics_reader;                       -- XD-W2-06-01 (a)
GRANT SELECT (job_id, tenant_id, memory_snapshot_id)
  ON jobs_read.job_memory_usage TO eval_metrics_reader;               -- XD-W2-06-01 (b)
-- XD-W2-06-01 (d): per-job outcomes come from WS-04's security_invoker view memory.v_job_outcomes (codes, flags,
-- counts, IDs and timestamps; no free text). WS-04's DDL grants eval_metrics_reader the column-level SELECT on the
-- view and on the base columns it reads (owner-authoritative; XD-W2-10-07), so all eight functions pin
-- system:all-tenants.
GRANT SELECT (job_id, tenant_id, workflow_type, evaluation_mode, release_bundle_id, assignment_reason,
              release_cohort, start_status, received_at, started_at, terminal_at)
  ON intake.job_requests TO eval_metrics_reader;
GRANT SELECT (gate_id, job_id, tenant_id, kind, state, risk_level, release_bundle_id, opened_at, expires_at,
              escalation_level, packet_revision, presented_digest, min_approvers, approvals_recorded, decided_at)
  ON approvals.gates TO eval_metrics_reader;
-- XD-W3-R1V-036: served model per eval job (parent table only; partitions keep no grants, XD-ORCH-04).
GRANT SELECT (job_id, tenant_id, served_model, occurred_at)
  ON cost.usage_events TO eval_metrics_reader;
GRANT SELECT (job_id, tenant_id, llm_cost_micros, worker_llm_cost_micros, compute_cost_micros, budget_micros,
              extensions, updated_at)
  ON cost.job_costs TO eval_metrics_reader;

-- Shared guard + audit. Not SECURITY DEFINER itself: it runs inside the definer functions below.
CREATE OR REPLACE FUNCTION ops.eval_read_audit(p_fn text, p_from timestamptz, p_to timestamptz, p_rows bigint)
RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
  PERFORM audit.emit(jsonb_build_object(
    'event_type', 'data.cross_tenant_read',
    'outcome', 'success',
    'channel', 'system',
    'tenant_id', 'replicant-system',          -- platform read spanning tenants; audit.events.tenant_id is NOT NULL
    'actor_principal_id', 'svc_eval-worker',
    'source_service', 'eval-worker',
    'target_kind', 'db_function',
    'target_id', p_fn,
    'details', jsonb_build_object('window_from', p_from::text, 'window_to', p_to::text, 'row_count', p_rows,
                                  'db_user', session_user::text)));
END
$$;

CREATE OR REPLACE FUNCTION ops.eval_check_window(p_from timestamptz, p_to timestamptz)
RETURNS void
LANGUAGE plpgsql IMMUTABLE
SET search_path = pg_catalog
AS $$
BEGIN
  IF p_from IS NULL OR p_to IS NULL OR p_to <= p_from OR p_to - p_from > interval '46 days' THEN
    RAISE EXCEPTION 'eval read window must be non-empty and at most 46 days' USING ERRCODE = '22023';
  END IF;
END
$$;

CREATE OR REPLACE FUNCTION ops.eval_jobs(p_from timestamptz, p_to timestamptz)
RETURNS TABLE (job_id text, tenant_id text, workflow_type text, status text, is_terminal boolean, phase text,
               evaluation_mode text, release_bundle_id text, release_cohort text, migrated boolean,
               worker_build_id text, memory_snapshot_id text, cycle integer, repair_used integer,
               repair_max integer, risk_level text, terminal_outcome text, terminal_at timestamptz,
               created_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog
SET replicant.principal_scope = 'system:all-tenants'
AS $$
DECLARE n bigint;
BEGIN
  PERFORM ops.eval_check_window(p_from, p_to);
  RETURN QUERY
    SELECT j.job_id, j.tenant_id, j.workflow_type, j.status, j.is_terminal, j.phase, j.evaluation_mode,
           j.release_bundle_id, j.release_cohort, j.migrated, j.worker_build_id, j.memory_snapshot_id, j.cycle,
           j.repair_used, j.repair_max, j.risk_level, j.terminal_outcome, j.terminal_at, j.created_at
    FROM jobs_read.jobs j
    WHERE j.created_at >= p_from AND j.created_at < p_to
    LIMIT 200000;
  GET DIAGNOSTICS n = ROW_COUNT;
  PERFORM ops.eval_read_audit('ops.eval_jobs', p_from, p_to, n);
END
$$;

CREATE OR REPLACE FUNCTION ops.eval_job_prs(p_from timestamptz, p_to timestamptz)
RETURNS TABLE (job_id text, tenant_id text, repo_id text, pr_number integer, state text, job_open boolean,
               opened_at timestamptz, merged_at timestamptz, closed_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog
SET replicant.principal_scope = 'system:all-tenants'
AS $$
DECLARE n bigint;
BEGIN
  PERFORM ops.eval_check_window(p_from, p_to);
  RETURN QUERY
    -- Windowed on PR open time, as WS-06's merge-rate query (online-metrics.md §4) needs.
    SELECT p.job_id, p.tenant_id, p.repo_id, p.pr_number, p.state, p.job_open, p.opened_at, p.merged_at, p.closed_at
    FROM jobs_read.job_prs p
    WHERE p.opened_at >= p_from AND p.opened_at < p_to
    LIMIT 200000;
  GET DIAGNOSTICS n = ROW_COUNT;
  PERFORM ops.eval_read_audit('ops.eval_job_prs', p_from, p_to, n);
END
$$;

CREATE OR REPLACE FUNCTION ops.eval_job_requests(p_from timestamptz, p_to timestamptz)
RETURNS TABLE (job_id text, tenant_id text, workflow_type text, evaluation_mode text, release_bundle_id text,
               assignment_reason text, release_cohort text, start_status text, received_at timestamptz,
               started_at timestamptz, terminal_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog
SET replicant.principal_scope = 'system:all-tenants'
AS $$
DECLARE n bigint;
BEGIN
  PERFORM ops.eval_check_window(p_from, p_to);
  RETURN QUERY
    SELECT r.job_id, r.tenant_id, r.workflow_type, r.evaluation_mode, r.release_bundle_id, r.assignment_reason,
           r.release_cohort, r.start_status, r.received_at, r.started_at, r.terminal_at
    FROM intake.job_requests r
    WHERE r.received_at >= p_from AND r.received_at < p_to
    LIMIT 200000;
  GET DIAGNOSTICS n = ROW_COUNT;
  PERFORM ops.eval_read_audit('ops.eval_job_requests', p_from, p_to, n);
END
$$;

CREATE OR REPLACE FUNCTION ops.eval_gates(p_from timestamptz, p_to timestamptz)
RETURNS TABLE (gate_id text, job_id text, tenant_id text, kind text, state text, risk_level text,
               release_bundle_id text, opened_at timestamptz, expires_at timestamptz, escalation_level integer,
               packet_revision integer, presented_digest text, min_approvers integer, approvals_recorded integer,
               decided_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog
SET replicant.principal_scope = 'system:all-tenants'
AS $$
DECLARE n bigint;
BEGIN
  PERFORM ops.eval_check_window(p_from, p_to);
  RETURN QUERY
    SELECT g.gate_id, g.job_id, g.tenant_id, g.kind, g.state, g.risk_level, g.release_bundle_id, g.opened_at,
           g.expires_at, g.escalation_level, g.packet_revision, g.presented_digest, g.min_approvers,
           g.approvals_recorded, g.decided_at
    FROM approvals.gates g
    WHERE g.opened_at >= p_from AND g.opened_at < p_to
    LIMIT 200000;
  GET DIAGNOSTICS n = ROW_COUNT;
  PERFORM ops.eval_read_audit('ops.eval_gates', p_from, p_to, n);
END
$$;

CREATE OR REPLACE FUNCTION ops.eval_job_costs(p_from timestamptz, p_to timestamptz)
RETURNS TABLE (job_id text, tenant_id text, llm_cost_micros bigint, worker_llm_cost_micros bigint,
               compute_cost_micros bigint, budget_micros bigint, extensions smallint, updated_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog
SET replicant.principal_scope = 'system:all-tenants'
AS $$
DECLARE n bigint;
BEGIN
  PERFORM ops.eval_check_window(p_from, p_to);
  RETURN QUERY
    SELECT c.job_id, c.tenant_id, c.llm_cost_micros, c.worker_llm_cost_micros, c.compute_cost_micros,
           c.budget_micros, c.extensions, c.updated_at
    FROM cost.job_costs c
    WHERE c.updated_at >= p_from AND c.updated_at < p_to
    LIMIT 200000;
  GET DIAGNOSTICS n = ROW_COUNT;
  PERFORM ops.eval_read_audit('ops.eval_job_costs', p_from, p_to, n);
END
$$;

CREATE OR REPLACE FUNCTION ops.eval_job_runs(p_from timestamptz, p_to timestamptz)
RETURNS TABLE (run_id text, job_id text, tenant_id text, node_key text, cycle integer, ordinal integer, role text,
               phase text, reason text, result_outcome text, qa_verdict text, started_at timestamptz,
               finished_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog
SET replicant.principal_scope = 'system:all-tenants'
AS $$
DECLARE n bigint;
BEGIN
  PERFORM ops.eval_check_window(p_from, p_to);
  RETURN QUERY
    SELECT r.run_id, r.job_id, r.tenant_id, r.node_key, r.cycle, r.ordinal, r.role, r.phase, r.reason,
           r.result_outcome, r.qa_verdict, r.started_at, r.finished_at
    FROM jobs_read.job_runs r
    WHERE r.started_at >= p_from AND r.started_at < p_to
    LIMIT 200000;
  GET DIAGNOSTICS n = ROW_COUNT;
  PERFORM ops.eval_read_audit('ops.eval_job_runs', p_from, p_to, n);
END
$$;

CREATE OR REPLACE FUNCTION ops.eval_job_memory_usage(p_from timestamptz, p_to timestamptz)
RETURNS TABLE (job_id text, tenant_id text, memory_snapshot_id text)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog
SET replicant.principal_scope = 'system:all-tenants'
AS $$
DECLARE n bigint;
BEGIN
  PERFORM ops.eval_check_window(p_from, p_to);
  RETURN QUERY
    SELECT u.job_id, u.tenant_id, u.memory_snapshot_id
    FROM jobs_read.job_memory_usage u JOIN jobs_read.jobs j ON j.job_id = u.job_id
    WHERE j.created_at >= p_from AND j.created_at < p_to
    LIMIT 200000;
  GET DIAGNOSTICS n = ROW_COUNT;
  PERFORM ops.eval_read_audit('ops.eval_job_memory_usage', p_from, p_to, n);
END
$$;

CREATE OR REPLACE FUNCTION ops.eval_job_outcomes(p_from timestamptz, p_to timestamptz)
RETURNS TABLE (job_id text, tenant_id text, release_bundle_id text, workflow_type text, terminal_status text,
               pr_merged boolean, merged_at timestamptz, post_merge_reverted boolean, outcome_report_count integer,
               review_rounds integer, first_pass_qa boolean, first_review_approved boolean, report_kind text,
               reported_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog
SET replicant.principal_scope = 'system:all-tenants'
AS $$
DECLARE n bigint;
BEGIN
  PERFORM ops.eval_check_window(p_from, p_to);
  RETURN QUERY
    -- Windowed on merged_at (the 14-day revert-rate guardrail counts merged PRs); unmerged jobs have no outcome to read.
    SELECT o.job_id, o.tenant_id, o.release_bundle_id, o.workflow_type, o.terminal_status, o.pr_merged, o.merged_at,
           o.post_merge_reverted, o.outcome_report_count, o.review_rounds, o.first_pass_qa, o.first_review_approved,
           o.report_kind, o.reported_at
    FROM memory.v_job_outcomes o
    WHERE o.merged_at >= p_from AND o.merged_at < p_to
    LIMIT 200000;
  GET DIAGNOSTICS n = ROW_COUNT;
  PERFORM ops.eval_read_audit('ops.eval_job_outcomes', p_from, p_to, n);
END
$$;

CREATE OR REPLACE FUNCTION ops.eval_job_served_models(p_from timestamptz, p_to timestamptz)
RETURNS TABLE (job_id text, tenant_id text, served_model text, requests bigint)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog
SET replicant.principal_scope = 'system:all-tenants'
AS $$
DECLARE n bigint;
BEGIN
  PERFORM ops.eval_check_window(p_from, p_to);
  -- Provider response-body model per job (C-IDS 2.0.0, CCR-03-10); never the X-Replicant-Served-Model header.
  RETURN QUERY
    SELECT u.job_id, u.tenant_id, u.served_model, count(*)::bigint
    FROM cost.usage_events u
    WHERE u.occurred_at >= p_from AND u.occurred_at < p_to AND u.job_id IS NOT NULL
    GROUP BY u.job_id, u.tenant_id, u.served_model
    LIMIT 200000;
  GET DIAGNOSTICS n = ROW_COUNT;
  PERFORM ops.eval_read_audit('ops.eval_job_served_models', p_from, p_to, n);
END
$$;

-- Ownership: the definer must be eval_metrics_reader (column grants only), never replicant_migrator (owns
-- every table). ALTER ... OWNER needs the new owner to hold CREATE on ops for the duration.
GRANT CREATE ON SCHEMA ops TO eval_metrics_reader;
ALTER FUNCTION ops.eval_jobs(timestamptz, timestamptz)         OWNER TO eval_metrics_reader;
ALTER FUNCTION ops.eval_job_prs(timestamptz, timestamptz)      OWNER TO eval_metrics_reader;
ALTER FUNCTION ops.eval_job_requests(timestamptz, timestamptz) OWNER TO eval_metrics_reader;
ALTER FUNCTION ops.eval_gates(timestamptz, timestamptz)        OWNER TO eval_metrics_reader;
ALTER FUNCTION ops.eval_job_costs(timestamptz, timestamptz)    OWNER TO eval_metrics_reader;
ALTER FUNCTION ops.eval_job_runs(timestamptz, timestamptz)     OWNER TO eval_metrics_reader;
ALTER FUNCTION ops.eval_job_memory_usage(timestamptz, timestamptz) OWNER TO eval_metrics_reader;
ALTER FUNCTION ops.eval_job_outcomes(timestamptz, timestamptz) OWNER TO eval_metrics_reader;
ALTER FUNCTION ops.eval_job_served_models(timestamptz, timestamptz) OWNER TO eval_metrics_reader;
REVOKE CREATE ON SCHEMA ops FROM eval_metrics_reader;

REVOKE ALL ON FUNCTION ops.eval_read_audit(text, timestamptz, timestamptz, bigint), ops.eval_check_window(timestamptz, timestamptz),
                       ops.eval_jobs(timestamptz, timestamptz), ops.eval_job_prs(timestamptz, timestamptz),
                       ops.eval_job_requests(timestamptz, timestamptz), ops.eval_gates(timestamptz, timestamptz),
                       ops.eval_job_costs(timestamptz, timestamptz), ops.eval_job_runs(timestamptz, timestamptz),
                       ops.eval_job_memory_usage(timestamptz, timestamptz), ops.eval_job_outcomes(timestamptz, timestamptz),
                       ops.eval_job_served_models(timestamptz, timestamptz)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops.eval_read_audit(text, timestamptz, timestamptz, bigint), ops.eval_check_window(timestamptz, timestamptz)
  TO eval_metrics_reader;
GRANT EXECUTE ON FUNCTION ops.eval_jobs(timestamptz, timestamptz), ops.eval_job_prs(timestamptz, timestamptz),
                          ops.eval_job_requests(timestamptz, timestamptz), ops.eval_gates(timestamptz, timestamptz),
                          ops.eval_job_costs(timestamptz, timestamptz), ops.eval_job_runs(timestamptz, timestamptz),
                          ops.eval_job_memory_usage(timestamptz, timestamptz), ops.eval_job_outcomes(timestamptz, timestamptz),
                          ops.eval_job_served_models(timestamptz, timestamptz)
  TO svc_eval_worker;
RESET ROLE;
