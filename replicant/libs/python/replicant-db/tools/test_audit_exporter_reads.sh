#!/usr/bin/env bash
# Behavioral probe for audit-exporter's approval-record reconciler (XD-W2-08-03, ruling on XD-W2-10-03).
# Usage: test_audit_exporter_reads.sh <container> [database]. Seeds one gate and one decision in each of two
# tenants as the superuser, then checks as svc_audit_exporter: under system:all-tenants it reads packet_key,
# memory_refs, text_key and text_sha256 for both tenants; under the tenant scope with no tenant it reads nothing;
# it cannot write approvals.
set -euo pipefail
C=${1:?container}; DB=${2:-replicant}
q() { podman exec -i "$C" psql -U postgres -d "$DB" -v ON_ERROR_STOP=1 -qAtX "$@"; }
G1=gate_01m5p7x7r0bt69j0h0fbxepw80; G2=gate_01m5p7x7r0bt69j0h0fbxepw81
q <<SQL
DELETE FROM approvals.gates WHERE gate_id IN ('$G1','$G2');
INSERT INTO approvals.gates (gate_id, job_id, tenant_id, seq, kind, state, requester, opened_at, packet_revision,
                             presented_digest, packet_key, channels, memory_refs)
VALUES ('$G1', 'job_01m5p7x7r0bt69j0h0fbxepw90', 'payments', 1, 'plan_approval', 'approved', 'usr_01m5p7x7r0bt69j0h0fbxepw70',
        now(), 1, 'sha256:$(printf 'a%.0s' {1..64})', 'tenants/payments/jobs/j/gates/$G1/packet-1.json', '{ui}',
        '{mem_01m5p7x7r0bt69j0h0fbxepw60@2}'),
       ('$G2', 'job_01m5p7x7r0bt69j0h0fbxepw92', 'commerce', 1, 'publish_approval', 'approved', 'usr_01m5p7x7r0bt69j0h0fbxepw71',
        now(), 1, 'sha256:$(printf 'b%.0s' {1..64})', 'tenants/commerce/jobs/j/gates/$G2/packet-1.json', '{ui}', '{}');
INSERT INTO approvals.gate_decisions (decision_id, gate_id, tenant_id, outcome, decision, principal_id, channel, text_key,
                                      text_sha256, decided_at, presented_digest)
VALUES ('dec_01m5p7x7r0bt69j0h0fbxepw50', '$G1', 'payments', 'accepted', 'approve', 'usr_01m5p7x7r0bt69j0h0fbxepw72', 'ui',
        'tenants/payments/jobs/j/approval-text/t1.json', '$(printf 'c%.0s' {1..64})', now(), 'sha256:$(printf 'a%.0s' {1..64})'),
       ('dec_01m5p7x7r0bt69j0h0fbxepw51', '$G2', 'commerce', 'accepted', 'approve', 'usr_01m5p7x7r0bt69j0h0fbxepw73', 'slack',
        NULL, NULL, now(), 'sha256:$(printf 'b%.0s' {1..64})');
SQL
FAILED=0
check() { local name="$1" sql="$2" want="$3" out got
  out=$(q -c "SET ROLE svc_audit_exporter; $sql" 2>&1) || true
  got=$(printf '%s\n' "$out" | grep -m1 ERROR || printf '%s\n' "$out" | tail -1)
  if [[ "$got" == $want ]]; then echo "ok    $name -> $got"; else echo "FAIL  $name -> $got (want $want)"; FAILED=1; fi; }
READ="SELECT count(DISTINCT g.tenant_id) FROM approvals.gates g JOIN approvals.gate_decisions d ON d.gate_id = g.gate_id
      WHERE g.gate_id IN ('$G1','$G2') AND g.packet_key IS NOT NULL AND g.memory_refs IS NOT NULL
        AND (d.text_key IS NULL) = (d.text_sha256 IS NULL)"
check "system scope reads both tenants"  "SELECT set_config('replicant.principal_scope','system:all-tenants',false); $READ" 2
check "memory_refs readable"             "SELECT set_config('replicant.principal_scope','system:all-tenants',false); SELECT array_to_string(memory_refs, ',') FROM approvals.gates WHERE gate_id = '$G1'" "mem_*@2"
check "text_sha256 readable"             "SELECT set_config('replicant.principal_scope','system:all-tenants',false); SELECT length(text_sha256) FROM approvals.gate_decisions WHERE decision_id = 'dec_01m5p7x7r0bt69j0h0fbxepw50'" 64
check "operator scope sees nothing"      "SELECT set_config('replicant.principal_scope','operator:all-tenants',false); $READ" 0
check "tenant scope, no tenant: nothing" "SELECT set_config('replicant.principal_scope','tenant',false); $READ" 0
check "free-text column not readable" "SELECT set_config('replicant.principal_scope','system:all-tenants',false); SELECT title FROM approvals.gates WHERE gate_id = '$G1'" "*permission denied*"
check "cannot update approvals"          "SELECT set_config('replicant.principal_scope','system:all-tenants',false); UPDATE approvals.gates SET state = 'open' WHERE gate_id = '$G1'" "*permission denied*"
q -c "DELETE FROM approvals.gates WHERE gate_id IN ('$G1','$G2')"
[ "$FAILED" = 0 ] && echo "audit-exporter reads OK" || { echo "audit-exporter reads FAILED"; exit 1; }
