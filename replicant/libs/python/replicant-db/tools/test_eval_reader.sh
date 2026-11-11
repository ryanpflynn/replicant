#!/usr/bin/env bash
# Behavioral test of eval-worker's audited cross-tenant reads (CCR-06-15; ddl/40-eval-metrics-reader.sql).
# Usage: test_eval_reader.sh <container> [database]   (the database already has the framework, the owners'
# DDL and 40-eval-metrics-reader.sql). Seeds two tenants' jobs as the superuser, then checks, as svc_eval_worker:
# the function returns both tenants, no free-text column is reachable, direct table access is denied, every
# call writes exactly one audit event, an over-long window is refused, and the caller's scope is unchanged after.
set -euo pipefail
ENGINE="${REPLICANT_CONTAINER_CLI:-podman}"
C=${1:?container}; DB=${2:-replicant}
q() { "$ENGINE" exec -i "$C" psql -U postgres -d "$DB" -v ON_ERROR_STOP=1 -qAtX "$@"; }
had_grant=$(q -c "SELECT has_function_privilege('r_audit_emit', 'audit.emit(jsonb)', 'EXECUTE')")
q <<'SQL'
INSERT INTO audit.event_types (event_type, category, description)
  SELECT 'data.cross_tenant_read', 'admin', 'stand-in until WS-08 adds it (XD-W2-10-01)'
  WHERE NOT EXISTS (SELECT 1 FROM audit.event_types WHERE event_type = 'data.cross_tenant_read');
-- Stand-in until WS-08's audit.sql grants it (XD-W2-10-02); a no-op once it does.
GRANT EXECUTE ON FUNCTION audit.emit(jsonb) TO r_audit_emit;
DELETE FROM jobs_read.jobs WHERE job_id IN ('job_01m5p7x7r0bt69j0h0fbxepw90','job_01m5p7x7r0bt69j0h0fbxepw92');
INSERT INTO jobs_read.jobs (job_id, tenant_id, seq, as_of, workflow_type, status, phase, requester, release_bundle_id,
                            created_at, snapshot, title)
VALUES ('job_01m5p7x7r0bt69j0h0fbxepw90', 'payments', 1, now(), 'feature', 'running', 'implement', 'usr_secret_a',
        'rb_01m5p7x7r0bt69j0h0fbxepw91', now() - interval '1 day', '{}', 'confidential title A'),
       ('job_01m5p7x7r0bt69j0h0fbxepw92', 'commerce', 1, now(), 'bugfix', 'succeeded', 'done', 'usr_secret_b',
        'rb_01m5p7x7r0bt69j0h0fbxepw91', now() - interval '2 days', '{}', 'confidential title B');
DELETE FROM memory.job_outcomes WHERE job_id IN ('job_01m5p7x7r0bt69j0h0fbxepw90','job_01m5p7x7r0bt69j0h0fbxepw92');
INSERT INTO memory.job_outcomes (job_id, tenant_id, repo, workflow_type, release_bundle_id, terminal_status, pr_merged, merged_at)
VALUES ('job_01m5p7x7r0bt69j0h0fbxepw90', 'payments', 'acme/pay-secret-repo', 'feature', 'rb_01m5p7x7r0bt69j0h0fbxepw91', 'succeeded', true, now() - interval '1 day'),
       ('job_01m5p7x7r0bt69j0h0fbxepw92', 'commerce', 'acme/shop-secret-repo', 'bugfix', 'rb_01m5p7x7r0bt69j0h0fbxepw91', 'succeeded', true, now() - interval '2 days');
SQL
FAILED=0
check() { local name="$1" sql="$2" want="$3" got
  local out; out=$(q -c "SET ROLE svc_eval_worker; $sql" 2>&1) || true
  got=$(printf '%s\n' "$out" | grep -m1 ERROR || printf '%s\n' "$out" | tail -1)
  if [[ "$got" == $want ]]; then echo "ok    $name -> $got"; else echo "FAIL  $name -> $got (want $want)"; FAILED=1; fi; }
before=$(q -c "SELECT count(*) FROM audit.events WHERE event_type = 'data.cross_tenant_read'")
check "both tenants via function"   "SELECT count(DISTINCT tenant_id) FROM ops.eval_jobs(now() - interval '7 days', now())" 2
check "no free text exposed"        "SELECT count(*) FROM ops.eval_jobs(now() - interval '7 days', now()) AS e WHERE row_to_json(e)::text LIKE '%confidential%'" 0
check "direct table read denied"    "SELECT count(*) FROM jobs_read.jobs" "*permission denied*"
check "window over 46 days refused" "SELECT count(*) FROM ops.eval_jobs(now() - interval '60 days', now())" "*at most 46 days*"
check "scope unchanged after call"  "SELECT count(*) FROM ops.eval_gates(now() - interval '7 days', now()); SELECT ops.principal_scope()" tenant
check "requests/prs/costs callable" "SELECT (SELECT count(*) FROM ops.eval_job_requests(now()-interval '7 days', now())) + (SELECT count(*) FROM ops.eval_job_prs(now()-interval '7 days', now())) + (SELECT count(*) FROM ops.eval_job_costs(now()-interval '7 days', now()))" 0
check "runs/memory usage callable"   "SELECT (SELECT count(*) FROM ops.eval_job_runs(now()-interval '7 days', now())) + (SELECT count(*) FROM ops.eval_job_memory_usage(now()-interval '7 days', now()))" 0
check "served models callable"      "SELECT count(*) FROM ops.eval_job_served_models(now() - interval '7 days', now())" 0
check "outcomes span both tenants"  "SELECT count(DISTINCT tenant_id) FROM ops.eval_job_outcomes(now() - interval '7 days', now())" 2
check "outcomes carry no repo name" "SELECT count(*) FROM ops.eval_job_outcomes(now() - interval '7 days', now()) AS o WHERE row_to_json(o)::text LIKE '%secret-repo%'" 0
check "other login cannot execute"  "RESET ROLE; SET ROLE svc_replicant_worker; SELECT count(*) FROM ops.eval_jobs(now() - interval '7 days', now())" "*permission denied*"
after=$(q -c "SELECT count(*) FROM audit.events WHERE event_type = 'data.cross_tenant_read'")
# 11 successful calls (eval_jobs ×2, gates, requests, prs, costs, runs, memory usage, served models, outcomes ×2); refused and denied
# calls write nothing.
if [ $((after - before)) = 11 ]; then echo "ok    audit events written: $((after - before))"; else echo "FAIL  audit events: $((after - before)) (want 11)"; FAILED=1; fi
q -c "DELETE FROM memory.job_outcomes WHERE job_id IN ('job_01m5p7x7r0bt69j0h0fbxepw90','job_01m5p7x7r0bt69j0h0fbxepw92')"
q -c "DELETE FROM jobs_read.jobs WHERE job_id IN ('job_01m5p7x7r0bt69j0h0fbxepw90','job_01m5p7x7r0bt69j0h0fbxepw92')"
# Remove the stand-in grant again so check_grants.py still reports WS-08's missing grant (XD-W2-10-02).
[ "$had_grant" = t ] || q -c "REVOKE EXECUTE ON FUNCTION audit.emit(jsonb) FROM r_audit_emit"
[ "$FAILED" = 0 ] && echo "eval reader OK" || { echo "eval reader FAILED"; exit 1; }
