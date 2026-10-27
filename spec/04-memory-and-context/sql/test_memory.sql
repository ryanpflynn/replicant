-- Behavioral test for memory.sql (run as svc_memory_service against a disposable DB).
-- Every block that must fail is wrapped so the script asserts the failure.
-- Usage: psql -v ON_ERROR_STOP=1 -U svc_memory_service -d replicant -f test_memory.sql
\set QUIET on
SET search_path = memory, public;

-- Expected failures are DO blocks: the WS-10 framework revokes TEMP, so there is no temp-schema helper.

-- 1. Admin bootstrap (scope admin:memory): embedding space + org governance policy.
BEGIN;
SET LOCAL replicant.principal_scope = 'admin:memory';
INSERT INTO embedding_spaces (space_id, gateway_model_id, dimensions, state, dedupe_merge_threshold,
                              dedupe_judge_threshold, conflict_probe_threshold, activated_at)
VALUES ('vc4-1024', 'voyage/voyage-code-4', 1024, 'active', 0.920, 0.800, 0.600, now());
INSERT INTO governance_policies (tenant_id, policy_version, document, digest, is_current, created_by)
VALUES ('replicant-system', 1, '{"auto_activation":{"repo_rule":{"enabled":true}}}',
        'sha256:' || encode(sha256('gov-v1'), 'hex'), true, 'usr_01knk8dfr0fvjc7v0b9vhvkzbp');
COMMIT;

-- 2. Write path for tenant 'payments' (scope tenant): a candidate repo rule from PR feedback.
BEGIN;
SET LOCAL replicant.tenant_id = 'payments';
INSERT INTO entries (entry_id, tenant_id, memory_type, origin, state, scope_level, repo, subject_key, created_by,
                     evidence_count, independent_evidence_count, distinct_author_count, distinct_repo_count, candidate_expires_at)
VALUES ('mem_01m42qyvr0dzhe1xg73b1bq38b', 'payments', 'rule', 'pr_feedback', 'candidate', 'repo',
        'github.com/acme-payments/ledger-api', 'money.arithmetic', 'svc_memory-service', 1, 1, 1, 1, now() + interval '30 days');
INSERT INTO entry_versions (entry_id, version, tenant_id, scope_level, repo, languages, subject_key, title, statement, rationale,
                            structured, structured_schema, content_digest, change_kind, created_by, token_estimate)
VALUES ('mem_01m42qyvr0dzhe1xg73b1bq38b', 1, 'payments', 'repo', 'github.com/acme-payments/ledger-api', '{go}',
        'money.arithmetic', 'Use shopspring/decimal for money',
        'Represent monetary amounts with github.com/shopspring/decimal.Decimal; never float64. Round half-even to the currency minor unit only at presentation.',
        'Float rounding caused proration drift in invoices (PR #1288 review).',
        '{"prefer":["github.com/shopspring/decimal"],"avoid":["float32","float64"],"applies_to":"monetary values"}',
        'replicant.memory.rule/v1', 'sha256:' || encode(sha256('v1'), 'hex'), 'create', 'svc_memory-service', 46);
INSERT INTO entry_embeddings (entry_id, version, space_id, tenant_id, memory_type, embedding, input_digest)
VALUES ('mem_01m42qyvr0dzhe1xg73b1bq38b', 1, 'vc4-1024', 'payments', 'rule',
        (SELECT array_agg(sin(i)::real)::halfvec FROM generate_series(1, 1024) i), 'sha256:' || encode(sha256('emb'), 'hex'));
INSERT INTO provenance (entry_id, tenant_id, supports_version, source_kind, source_url, repo, pr_number, comment_id, comment_digest,
                        source_excerpt, author_principal_id, author_repo_permission, author_is_codeowner, job_id, cycle,
                        extraction_prompt_name, extraction_prompt_version, extraction_model, independence_key, observed_at)
VALUES ('mem_01m42qyvr0dzhe1xg73b1bq38b', 'payments', 1, 'pr_review_comment',
        'https://github.com/acme-payments/ledger-api/pull/1288#discussion_r1849302211', 'github.com/acme-payments/ledger-api',
        1288, 1849302211, 'sha256:' || encode(sha256('comment'), 'hex'), 'use decimal.Decimal for proration, floats drift',
        'usr_01knk8dfr0fvjc7v0b9vhvkzbp', 'write', true, 'job_01m5p7x7r0bt69j0h0fbxepw90', 1,
        'memory-extract-generalize', 14, 'anthropic/claude-sonnet-5-5', 'sha256:' || encode(sha256('usr|1288'), 'hex'), now());
INSERT INTO state_transitions (entry_id, tenant_id, from_state, to_state, entry_version, actor_kind, actor_principal_id,
                               governance_tenant_id, governance_policy_version, reason_code)
VALUES ('mem_01m42qyvr0dzhe1xg73b1bq38b', 'payments', NULL, 'candidate', 1, 'system_policy', 'svc_memory-service',
        'replicant-system', 1, 'extracted_from_review');
COMMIT;

-- 3. Auto-activation under policy (candidate -> active) after merge.
BEGIN;
SET LOCAL replicant.tenant_id = 'payments';
INSERT INTO state_transitions (entry_id, tenant_id, from_state, to_state, entry_version, actor_kind, actor_principal_id,
                               governance_tenant_id, governance_policy_version, reason_code)
VALUES ('mem_01m42qyvr0dzhe1xg73b1bq38b', 'payments', 'candidate', 'active', 1, 'system_policy', 'svc_memory-service',
        'replicant-system', 1, 'auto_activate_repo_rule_merged');
UPDATE entries SET state = 'active', state_seq = state_seq + 1, probation_until = now() + interval '14 days'
 WHERE entry_id = 'mem_01m42qyvr0dzhe1xg73b1bq38b';
COMMIT;

-- 4. Negative tests (session-level tenant for the block).
SELECT set_config('replicant.tenant_id', 'payments', false);
DO $t$ BEGIN
  BEGIN
    EXECUTE $q$
  UPDATE memory.entries SET state = 'disabled', state_seq = state_seq + 1 WHERE entry_id = 'mem_01m42qyvr0dzhe1xg73b1bq38b';
$q$;
  EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'ok (expected failure) state change without a transition row: %', SQLERRM; RETURN;
  END;
  RAISE EXCEPTION 'expected failure did not happen: state change without a transition row';
END $t$;
DO $t$ BEGIN
  BEGIN
    EXECUTE $q$
  UPDATE memory.entry_versions SET statement = 'tampered' WHERE entry_id = 'mem_01m42qyvr0dzhe1xg73b1bq38b'
$q$;
  EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'ok (expected failure) entry_versions immutable: %', SQLERRM; RETURN;
  END;
  RAISE EXCEPTION 'expected failure did not happen: entry_versions immutable';
END $t$;
DO $t$ BEGIN
  BEGIN
    EXECUTE $q$
  INSERT INTO memory.state_transitions (entry_id, tenant_id, from_state, to_state, entry_version, actor_kind, actor_principal_id, reason_code)
  VALUES ('mem_01m42qyvr0dzhe1xg73b1bq38b', 'payments', 'active', 'disabled', 1, 'admin', 'usr_01knk8dfr0fvjc7v0b9vhvkzbp', 'no_action_id')
$q$;
  EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'ok (expected failure) admin transition requires curator_action_id: %', SQLERRM; RETURN;
  END;
  RAISE EXCEPTION 'expected failure did not happen: admin transition requires curator_action_id';
END $t$;
DO $t$ BEGIN
  BEGIN
    EXECUTE $q$
  INSERT INTO memory.state_transitions (entry_id, tenant_id, from_state, to_state, entry_version, actor_kind, actor_principal_id,
     governance_tenant_id, governance_policy_version, reason_code)
  VALUES ('mem_01m42qyvr0dzhe1xg73b1bq38b', 'payments', 'disabled', 'active', 1, 'system_safety', 'svc_memory-service', 'replicant-system', 1, 'bad')
$q$;
  EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'ok (expected failure) system_safety cannot activate: %', SQLERRM; RETURN;
  END;
  RAISE EXCEPTION 'expected failure did not happen: system_safety cannot activate';
END $t$;
DO $t$ BEGIN
  BEGIN
    EXECUTE $q$
  INSERT INTO memory.entries (entry_id, tenant_id, memory_type, origin, state, scope_level, created_by)
  VALUES ('mem_01m42qyvr0dzhe1xg73b1bq38c', 'payments', 'rule', 'curator', 'candidate', 'org', 'svc_memory-service')
$q$;
  EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'ok (expected failure) org scope must use tenant replicant-system: %', SQLERRM; RETURN;
  END;
  RAISE EXCEPTION 'expected failure did not happen: org scope must use tenant replicant-system';
END $t$;

-- 5. RLS: another tenant cannot see or write payments' rows.
BEGIN;
SET LOCAL replicant.tenant_id = 'checkout';
DO $$ BEGIN
  IF (SELECT count(*) FROM memory.entries) <> 0 THEN RAISE EXCEPTION 'RLS leak: checkout sees payments entries'; END IF;
  IF (SELECT count(*) FROM memory.governance_policies) <> 1 THEN RAISE EXCEPTION 'org governance must be readable'; END IF;
  RAISE NOTICE 'ok RLS read isolation';
END $$;
COMMIT;
SELECT set_config('replicant.tenant_id', 'checkout', false);
DO $t$ BEGIN
  BEGIN
    EXECUTE $q$
  INSERT INTO memory.entries (entry_id, tenant_id, memory_type, origin, state, scope_level, repo, created_by)
  VALUES ('mem_01m42qyvr0dzhe1xg73b1bq38d', 'payments', 'rule', 'curator', 'candidate', 'repo', 'github.com/acme-payments/x', 'svc_memory-service')
$q$;
  EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'ok (expected failure) RLS write isolation: %', SQLERRM; RETURN;
  END;
  RAISE EXCEPTION 'expected failure did not happen: RLS write isolation';
END $t$;
SELECT set_config('replicant.tenant_id', '', false);
BEGIN;
SET LOCAL replicant.tenant_id = 'payments';
DO $$ BEGIN
  IF (SELECT count(*) FROM memory.curator_actions) <> 0 OR (SELECT count(*) FROM memory.quarantines) <> 0 THEN
    RAISE EXCEPTION 'unexpected'; END IF;
  RAISE NOTICE 'ok admin tables hidden from tenant scope (count 0 by policy)';
END $$;
COMMIT;

-- 6. Hybrid retrieval over the scope-filtered set (exact scoring, retrieval-injection.md §2).
BEGIN;
SET LOCAL replicant.tenant_id = 'payments';
SET LOCAL hnsw.iterative_scan = 'relaxed_order';
WITH q AS (SELECT websearch_to_tsquery('english', 'proration rounding money float') AS tsq,
                  (SELECT array_agg(sin(i + 0.01)::real)::halfvec FROM generate_series(1, 1024) i) AS qv),
cand AS (
  SELECT e.entry_id, v.version, v.tsv, emb.embedding, e.confidence
    FROM memory.entries e
    JOIN memory.entry_versions v ON v.entry_id = e.entry_id AND v.version = e.current_version
    LEFT JOIN memory.entry_embeddings emb ON emb.entry_id = v.entry_id AND emb.version = v.version AND emb.space_id = 'vc4-1024'
   WHERE e.state = 'active' AND e.tenant_id IN ('payments', 'replicant-system')
     AND (v.repo IS NULL OR v.repo = 'github.com/acme-payments/ledger-api')),
lex AS (SELECT entry_id, rank() OVER (ORDER BY ts_rank_cd(tsv, q.tsq) DESC) AS r FROM cand, q WHERE tsv @@ q.tsq),
vec AS (SELECT entry_id, rank() OVER (ORDER BY (embedding::halfvec(1024)) <=> q.qv) AS r FROM cand, q WHERE embedding IS NOT NULL)
SELECT c.entry_id, c.version,
       coalesce(1.0 / (60 + lex.r), 0) + coalesce(1.0 / (60 + vec.r), 0) AS rrf, c.confidence
  FROM cand c LEFT JOIN lex USING (entry_id) LEFT JOIN vec USING (entry_id)
 ORDER BY rrf DESC;
COMMIT;

-- 7. Admin purge: ledger IDs recorded first, content nulled, embeddings removed, tombstone kept.
BEGIN;
SET LOCAL replicant.principal_scope = 'admin:memory';
INSERT INTO curator_actions (action_id, tenant_id, admin_principal_id, action, target, request_digest, idempotency_key,
                             okta_groups_checked, audit_event_id)
VALUES ('mact_01m6aaaaaaaaaaaaaaaaaaaaaa', 'payments', 'usr_01knk8dfr0fvjc7v0b9vhvkzbp', 'purge',
        '{"entry_ids":["mem_01m42qyvr0dzhe1xg73b1bq38b"]}', 'sha256:' || encode(sha256('req'), 'hex'), 'idem-0001-test',
        '{replicant-admins}', 'aud_01m6aaaaaaaaaaaaaaaaaaaaaa');
INSERT INTO purges (purge_id, tenant_id, path, requested_by, reason_code, reason_text, entry_ids, deletion_ledger_ids, status, curator_action_id)
VALUES ('mprg_01m6aaaaaaaaaaaaaaaaaaaaaa', 'payments', 'admin', 'usr_01knk8dfr0fvjc7v0b9vhvkzbp', 'pii', 'test',
        '{mem_01m42qyvr0dzhe1xg73b1bq38b}', '{del_01m6aaaaaaaaaaaaaaaaaaaaaa}', 'db_purged', 'mact_01m6aaaaaaaaaaaaaaaaaaaaaa');
SELECT memory.purge_entry('mem_01m42qyvr0dzhe1xg73b1bq38b', 'mprg_01m6aaaaaaaaaaaaaaaaaaaaaa');
DO $$ BEGIN
  IF (SELECT state FROM memory.entries WHERE entry_id = 'mem_01m42qyvr0dzhe1xg73b1bq38b') <> 'purged' THEN RAISE EXCEPTION 'not purged'; END IF;
  IF (SELECT statement FROM memory.entry_versions WHERE entry_id = 'mem_01m42qyvr0dzhe1xg73b1bq38b') IS NOT NULL THEN RAISE EXCEPTION 'content kept'; END IF;
  IF EXISTS (SELECT 1 FROM memory.entry_embeddings WHERE entry_id = 'mem_01m42qyvr0dzhe1xg73b1bq38b') THEN RAISE EXCEPTION 'embedding kept'; END IF;
  IF (SELECT state FROM memory.state_as_of('mem_01m42qyvr0dzhe1xg73b1bq38b', now())) <> 'purged' THEN RAISE EXCEPTION 'as-of wrong'; END IF;
  RAISE NOTICE 'ok admin purge tombstone';
END $$;
COMMIT;
SELECT set_config('replicant.principal_scope', 'admin:memory', false);
DO $t$ BEGIN
  BEGIN
    EXECUTE $q$
  INSERT INTO memory.purges (purge_id, tenant_id, path, requested_by, reason_code, reason_text, entry_ids, deletion_ledger_ids, status)
  VALUES ('mprg_01m6bbbbbbbbbbbbbbbbbbbbbb', 'payments', 'admin', 'usr_01knk8dfr0fvjc7v0b9vhvkzbp', 'pii', 'x',
          '{mem_01m42qyvr0dzhe1xg73b1bq38b}', '{}', 'db_purged')
$q$;
  EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'ok (expected failure) purge requires deletion-ledger IDs and a curator action: %', SQLERRM; RETURN;
  END;
  RAISE EXCEPTION 'expected failure did not happen: purge requires deletion-ledger IDs and a curator action';
END $t$;
SELECT set_config('replicant.principal_scope', '', false);
DO $$ BEGIN
  IF (SELECT count(*) FROM pg_indexes WHERE schemaname = 'memory' AND tablename LIKE 'entry_embeddings_t_%'
        AND indexdef LIKE '%USING hnsw%') < 2 THEN RAISE EXCEPTION 'tenant partitions lack HNSW index'; END IF;
  RAISE NOTICE 'ok per-tenant embedding partitions carry the partial HNSW index';
END $$;

-- 8. Leak purge path (R1-103) and episode horizon purge (R1-106), both service-executed.
BEGIN;
SET LOCAL replicant.tenant_id = 'payments';
INSERT INTO entries (entry_id, tenant_id, memory_type, origin, state, scope_level, repo, created_by)
VALUES ('mem_01m7cccccccccccccccccccccc', 'payments', 'episode', 'episodic_auto', 'expired', 'repo',
        'github.com/acme-payments/ledger-api', 'svc_memory-service'),
       ('mem_01m7dddddddddddddddddddddd', 'payments', 'rule', 'pr_feedback', 'candidate', 'repo',
        'github.com/acme-payments/ledger-api', 'svc_memory-service');
INSERT INTO entry_versions (entry_id, version, tenant_id, scope_level, repo, statement, structured_schema, content_digest, change_kind, created_by)
VALUES ('mem_01m7cccccccccccccccccccccc', 1, 'payments', 'repo', 'github.com/acme-payments/ledger-api',
        'bugfix job_01m5p7x7r0bt69j0h0fbxepw90 merged: 3 files, 1 review round, QA first pass', 'replicant.memory.episode/v2',
        'sha256:' || encode(sha256('ep'), 'hex'), 'create', 'svc_memory-service'),
       ('mem_01m7dddddddddddddddddddddd', 1, 'payments', 'repo', 'github.com/acme-payments/ledger-api',
        'Never log AKIA-style keys', 'replicant.memory.rule/v1', 'sha256:' || encode(sha256('lk'), 'hex'), 'create', 'svc_memory-service');
INSERT INTO purges (purge_id, tenant_id, path, requested_by, approved_by, leak_id, reason_code, reason_text, entry_ids, deletion_ledger_ids, status,
                    governance_tenant_id, governance_policy_version)
VALUES ('mprg_01m7eeeeeeeeeeeeeeeeeeeeee', 'payments', 'leak', 'svc_replicant-worker',
        '{usr_01knk8dfr0fvjc7v0b9vhvkzbp,usr_01knk8dfr0fvjc7v0b9vhvkzbq}', 'leak_01m7eeeeeeeeeeeeeeeeeeeeee',
        'secret_leak', 'LeakPurge', '{mem_01m7dddddddddddddddddddddd}', '{del_01m7eeeeeeeeeeeeeeeeeeeeee}', 'db_purged', NULL, NULL),
       ('mprg_01m7ffffffffffffffffffffff', 'payments', 'horizon', 'svc_memory-service', '{}', NULL,
        'episode_horizon', 'episode older than 2 years', '{mem_01m7cccccccccccccccccccccc}', '{del_01m7ffffffffffffffffffffff}', 'db_purged', 'replicant-system', 1);
COMMIT;
BEGIN;
SET LOCAL replicant.tenant_id = 'payments';
SELECT memory.purge_entry('mem_01m7dddddddddddddddddddddd', 'mprg_01m7eeeeeeeeeeeeeeeeeeeeee');
SELECT memory.purge_entry('mem_01m7cccccccccccccccccccccc', 'mprg_01m7ffffffffffffffffffffff');
DO $$ BEGIN
  IF (SELECT count(*) FROM memory.state_transitions WHERE to_state = 'purged' AND actor_kind = 'leak_purge'
        AND leak_id = 'leak_01m7eeeeeeeeeeeeeeeeeeeeee') <> 1 THEN RAISE EXCEPTION 'leak purge transition missing'; END IF;
  IF (SELECT count(*) FROM memory.state_transitions WHERE to_state = 'purged' AND actor_kind = 'system_policy'
        AND entry_id = 'mem_01m7cccccccccccccccccccccc') <> 1 THEN RAISE EXCEPTION 'horizon purge transition missing'; END IF;
  RAISE NOTICE 'ok leak and horizon purge paths';
END $$;
COMMIT;
DO $t$ BEGIN
  BEGIN
    EXECUTE $q$ DELETE FROM memory.entries $q$;
  EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'ok (expected failure) entries never deleted: %', SQLERRM; RETURN;
  END;
  RAISE EXCEPTION 'expected failure did not happen: entries never deleted';
END $t$;

-- 9. Snapshots: blob-first export key required, strictly append-only; items carry no holdout (R1-096, R1-102).
BEGIN;
SET LOCAL replicant.tenant_id = 'payments';
INSERT INTO entries (entry_id, tenant_id, memory_type, origin, state, scope_level, repo, created_by)
VALUES ('mem_01m8gggggggggggggggggggggg', 'payments', 'rule', 'curator', 'candidate', 'repo', 'github.com/acme-payments/ledger-api', 'svc_memory-service');
INSERT INTO entry_versions (entry_id, version, tenant_id, scope_level, repo, statement, structured_schema, content_digest, change_kind, created_by)
VALUES ('mem_01m8gggggggggggggggggggggg', 1, 'payments', 'repo', 'github.com/acme-payments/ledger-api', 'Use zap for logging',
        'replicant.memory.rule/v1', 'sha256:' || encode(sha256('zap'), 'hex'), 'create', 'svc_memory-service');
INSERT INTO snapshots (snapshot_id, tenant_id, policy_ref, policy_digest, spec_digest, item_count, export_key, export_sha256, renderer_version, created_at)
VALUES ('msnap_my7g26p6dy194fr4kjdehzb5hr', 'payments', 'policy/memory-snapshot-default@3', 'sha256:' || encode(sha256('p'), 'hex'),
        'sha256:' || encode(sha256('s'), 'hex'), 1, 'tenants/payments/snapshots/msnap_my7g26p6dy194fr4kjdehzb5hr.json',
        encode(sha256('blob'), 'hex'), 'replicant.memory-block/v1@1.0.0', '2026-10-24T09:00:00Z');
INSERT INTO snapshot_items (snapshot_id, entry_id, version, tenant_id, section, precedence_rank, scope_specificity, base_score, created_at)
VALUES ('msnap_my7g26p6dy194fr4kjdehzb5hr', 'mem_01m8gggggggggggggggggggggg', 1, 'payments', 'rule', 1, 3, 0.5, '2026-10-24T09:00:00Z');
INSERT INTO holdout_assignments (job_id, entry_id, tenant_id, arm, experiment)
VALUES ('job_01m5p7x7r0bt69j0h0fbxepw90', 'mem_01m8gggggggggggggggggggggg', 'payments', 'holdout', 'probation');
COMMIT;
SELECT set_config('replicant.tenant_id', 'payments', false);
DO $t$ BEGIN
  BEGIN
    EXECUTE $q$ UPDATE memory.snapshots SET item_count = 2 $q$;
  EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'ok (expected failure) snapshots append-only: %', SQLERRM; RETURN;
  END;
  RAISE EXCEPTION 'expected failure did not happen: snapshots append-only';
END $t$;
DO $t$ BEGIN
  BEGIN
    EXECUTE $q$
  INSERT INTO memory.snapshots (snapshot_id, tenant_id, policy_ref, policy_digest, spec_digest, item_count, renderer_version)
  VALUES ('msnap_aaaaaaaaaaaaaaaaaaaaaaaaaa', 'payments', 'policy/x@1', 'sha256:' || encode(sha256('p'), 'hex'),
          'sha256:' || encode(sha256('s'), 'hex'), 0, 'replicant.memory-block/v1@1.0.0')
$q$;
  EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'ok (expected failure) snapshot row requires the export written first: %', SQLERRM; RETURN;
  END;
  RAISE EXCEPTION 'expected failure did not happen: snapshot row requires the export written first';
END $t$;

-- 10. Flags (R2-028) and correction requests (R2-018): one open flag per reporter; holds are not states.
INSERT INTO memory.entry_flags (entry_id, entry_version, tenant_id, kind, source, reporter_principal_id, job_id, reason_code, due_at)
VALUES ('mem_01m8gggggggggggggggggggggg', 1, 'payments', 'viewer_flag', 'ui_job_view', 'usr_01knk8dfr0fvjc7v0b9vhvkzbp',
        'job_01m5p7x7r0bt69j0h0fbxepw90', 'wrong', now() + interval '72 hours');
DO $t$ BEGIN
  BEGIN
    EXECUTE $q$
  INSERT INTO memory.entry_flags (entry_id, entry_version, tenant_id, kind, source, reporter_principal_id, reason_code, due_at)
  VALUES ('mem_01m8gggggggggggggggggggggg', 1, 'payments', 'viewer_flag', 'ui_job_view', 'usr_01knk8dfr0fvjc7v0b9vhvkzbp', 'wrong', now())
$q$;
  EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'ok (expected failure) one open flag per reporter: %', SQLERRM; RETURN;
  END;
  RAISE EXCEPTION 'expected failure did not happen: one open flag per reporter';
END $t$;
UPDATE memory.entries SET activation_hold = true, hold_reason = 'human_flags' WHERE entry_id = 'mem_01m8gggggggggggggggggggggg';
DO $t$ BEGIN
  BEGIN
    EXECUTE $q$
  UPDATE memory.entries SET activation_hold = true, hold_reason = NULL WHERE entry_id = 'mem_01m8gggggggggggggggggggggg'
$q$;
  EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'ok (expected failure) hold needs a reason: %', SQLERRM; RETURN;
  END;
  RAISE EXCEPTION 'expected failure did not happen: hold needs a reason';
END $t$;
SELECT set_config('replicant.tenant_id', '', false);

-- 11. Tenant scope cannot write governance; FORCE RLS applies to the owner too.
SELECT set_config('replicant.tenant_id', 'payments', false);
DO $t$ BEGIN
  BEGIN
    EXECUTE $q$
  INSERT INTO memory.governance_policies (tenant_id, policy_version, document, digest, created_by)
  VALUES ('payments', 1, '{}', 'sha256:' || encode(sha256('x'), 'hex'), 'usr_01knk8dfr0fvjc7v0b9vhvkzbp')
$q$;
  EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'ok (expected failure) governance write requires admin:memory: %', SQLERRM; RETURN;
  END;
  RAISE EXCEPTION 'expected failure did not happen: governance write requires admin:memory';
END $t$;
SELECT set_config('replicant.tenant_id', '', false);
\echo 'test_memory.sql: all assertions passed'
