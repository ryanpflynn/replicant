# ADR-09-005: Core-signed run grants bound what the exec side can mint

- **Status:** Accepted; applied in C-RUNNER 2.x (final 2.5.2) and C-CRD 2.x (final 2.4.0). Was CCR-09-02 (C-RUNNER `RunSpec.run_grant`,
  `RunTokenClaims.grant`/`grant_jti`; C-CRD `spec.runGrant`).
- **Constraints:** D-04, D-21; ADR-002, ADR-007

## Context

runner-api issues run tokens signed with an exec-account KMS key (C-RUNNER). Every verifier
(AI gateway, memory-mcp, context-mcp, repo-mirror) trusts any token that key signs. The exec
cluster is the most exposed part of the system (ADR-007). If runner-api is compromised, or an
attacker on an exec node obtains its credentials, it can sign tokens for **any** tenant, run,
repository list, and scope: read every tenant's code through repo-mirror, query every tenant's
memory and code search, and spend any budget (threat-model §7.3).

## Options

1. **Accept** and rely on detection.
2. **Verifiers call `GET /v1/runs/{run_id}`** to confirm the run exists. Circular: the compromised
   Runner answers.
3. **Core-signed run grant** (chosen). The orchestrator, which decides that a run should exist,
   signs a short capability document; the Runner's tokens must embed it; verifiers check both
   signatures and that the token is within the grant.
4. Move token issuance to core entirely. Sandboxes would then need a path to a core issuer for
   refresh, adding a core service reachable from sandboxes; refresh still needs run-state checks
   that live in exec (etcd).

## Decision

Option 3.

- The `runner.start_run` activity (replicant-worker, core) signs a JWS with KMS key class
  `run-grant` (ECC_NIST_P256, core account) before calling `POST /v1/runs`, and puts it in
  `RunSpec.run_grant`. Claims:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://schemas.replicant.example.com/security/run-grant/v1.json",
  "title": "Run grant claims",
  "type": "object",
  "additionalProperties": false,
  "required": ["iss", "typ", "jti", "iat", "exp", "tenant_id", "job_id", "run_id", "role", "repo_ids", "audiences", "scopes", "budget_usd", "acting_for", "spec_digest"],
  "properties": {
    "iss": {"const": "replicant-<env>-core"},
    "typ": {"const": "replicant-run-grant+jwt"},
    "jti": {"type": "string", "pattern": "^rgr_[0-9a-hjkmnp-tv-z]{26}$"},
    "iat": {"type": "integer"},
    "exp": {"type": "integer", "description": "startup + active + finalize + 300 s after iat, matching the workflow's overall run timer"},
    "tenant_id": {"type": "string", "pattern": "^[a-z][a-z0-9]*(-[a-z0-9]+)*$"},
    "job_id": {"type": "string", "pattern": "^job_[0-9a-hjkmnp-tv-z]{26}$"},
    "run_id": {"type": "string", "pattern": "^run_[0-9a-hjkmnp-tv-z]{26}$"},
    "role": {"enum": ["planner", "implementer", "qa", "reviewer", "repair", "takeover", "eval"]},
    "repo_ids": {"type": "array", "maxItems": 5, "items": {"type": "string", "maxLength": 64}},
    "audiences": {"type": "array", "items": {"enum": ["ai-gateway", "memory-mcp", "context-mcp", "repo-mirror", "runner-sandbox"]}},
    "scopes": {"type": "array", "items": {"enum": ["gateway:invoke", "memory:read", "memory:search", "context:read", "code_search:read", "repo:fetch", "inbox:read", "credentials:aws"]}},
    "budget_usd": {"type": "number", "minimum": 0},
    "acting_for": {"type": "string", "pattern": "^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_[a-z][a-z0-9-]{2,40})$"},
    "spec_digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$", "description": "C-RUNNER spec_digest: sha256(JCS(RunSpec)) excluding run_grant and the per-attempt fields C-RUNNER 2.4.1 enumerates (trace_context, …); binds the grant to the exact spec"}
  }
}
```

- `run_id` in the grant equals `derived("run", tenant_id, idempotency_key)`; the activity computes it
  (C-IDS). `repo_ids` are the job's targets and additional repos as authorized at intake and
  recorded in `intake.job_requests` (the activity reads them from workflow state, which came from the
  intake record).
- runner-api verifies the grant against the core grant JWKS (cached 10 minutes) and that
  `spec_digest` matches the request, then stores the grant in `AgentRun.spec.runGrant`.
- Every run token embeds the grant (`grant`) and its `grant_jti`; token claims must be a subset of
  the grant (audience ∈ `audiences`, scopes ⊆, `repo_ids` ⊆, `budget_usd` ≤, `exp` ≤ grant `exp`).
- Verifiers check the token (Runner JWKS) **and** the grant (core JWKS), plus a core revocation list keyed by the grant's `job_id` and `run_id` (C-RUNNER 2.1.0). Keying by `job_id`/`run_id` rather than grant `jti` loses nothing: each grant names exactly one `run_id` and is bound to one RunSpec digest, and a deliberate re-run gets a new `run_id`, so revoking the `run_id` revokes exactly that grant, and revoking the `job_id` covers every grant of the job. `grant_jti` remains in tokens for audit correlation only.

## Consequences

- A compromised Runner can mint tokens only for grants it holds, which are grants of runs currently
  in flight (stored in etcd), within each grant's scope and lifetime. "All tenants' code" becomes
  "repos of jobs currently running", for at most the remaining grant lifetime.
- The grant is not a bearer credential: without a Runner signature it is useless, and verifiers
  require both.
- Token size grows by about 700 bytes (well under the 4,096-byte `RunTokenResponse.token` limit).
- The replicant-worker gains a signing key. Its compromise already implied the ability to start any
  run through the Runner; the grant does not add authority beyond that.
- STS sessions remain Runner-asserted (R-04); S3 cannot verify grants.

## Revisit when

- AWS offers verification of external signed claims in session policies (then bind STS to grants).
