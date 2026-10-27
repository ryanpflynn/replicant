# Known Issues at Spec Close

**Status:** closed on 2026-10-24 under the user's instruction "blockers only from here; log everything else as known
issues and finish." This file lists what is known to be open, partially verified, or unverified. None of it is a
blocker: every known blocker was fixed and checked (§1).

## 1. Blockers: all closed

| ID | Issue | Resolution | Checked by |
|---|---|---|---|
| R1V-001 | runner-api could not read the bundle pointer, so every createRun would fail | IAM grants `registry/bundles/*` read | Orchestrator, by reading the file |
| R1V-002 | runner-api egress had no route to the `ops` bucket | `cnp-exec-system.yaml` adds the `ops` and `transcripts` hosts | Orchestrator, by reading the file |
| R1V-003 | runner-api could not read namespace labels | ClusterRole `replicant-runner-api-cluster-read` | Orchestrator, by reading the file |
| R1V-004 | run-notifier used a bare KMS alias across accounts | The alias ARN is in the core account, with a Go unit test | Orchestrator, by reading the file; WS-09 `go test` |
| Original R1 blockers (25) | Seams that stopped jobs end to end | 19 fixed by wave 2; the other 6 are fixed in wave 3 by owner report | R1 verification, plus owner reports |

The final WS-10 `STRICT=1` integration gate result is recorded in §6.

## 2. Wave-3 fixes reported by owners but not independently re-verified

`R1-verification.md` listed 187 remaining issues: 4 blocker, 68 major and 115 minor. In wave 3 every owner reported
fixing all of its rows, except the disputes resolved below and anything WS-01 lists in §3. A second independent
verification pass was **not** run, at the user's instruction.

**Treat the R1V table as "fixed per owner, unverified" until an implementation-phase review confirms it.**

Disputes resolved during wave 3:

| Row | Outcome |
|---|---|
| R1V-023 | QA harness interface: WS-03 §4.10 is canonical |
| R1V-032 | Shadow jobs use channel `eval` |
| R1V-063 | No S3 Batch delete exists, so offboarding deletes directly |
| R1V-071 | Closed in WS-11's favour |
| R1V-116 | The setting is `spec.prLifetimeUsd` |

## 3. Not done

- WS-01 rows not completed in wave 3: see §3a, which is filled from WS-01's closing report.
- **R2 red-team findings.** 22 were "partial" at verification. Wave 3 addressed those that appeared as R1V rows; the
  rest stand as recorded in `R2-red-team.md`.
- **`tradeoffs.md` not written** (Z brief deliverable). The 18 trade-off topics are mapped to their ADRs in `99-implementation-plan/traceability.md` §6.
- **Lower-severity stale citations.** Citations to an older minor contract version that are not clearly historical were
  left unchanged, roughly 180 across the spec. Same-minor citations (301) were updated mechanically.

### 3a. WS-01 open rows and unconfirmed cross-workstream items

**Edited by WS-01 but not reconciled with the other workstreams' text:**
- R1V-089: README export host.
- R1V-091: README open-question row.
- R1V-092: Aurora PG 16.15 sizing.

**Coordination entries filed at close and not yet applied by their counterparts:**
- XD-W3-R1V-011: `qa_config_remove: ["imageBuild"]` (WS-05).
- XD-W3-R1V-012: WS-05 must widen `step_id` for the hotfix `qa_again` node and add `blocking_test_ids`.
- XD-W3-R1V-016: extra `build_run_plan` inputs (WS-03).

**Test limitation (V01-28):** the Temporal time-skipping test server ignores versioning overrides. Versioning scenarios
stay on the dev server with a scaled policy clock.

**WS-01 final state:** 44/44 scenarios pass on both engines, all 43 compared traces are identical, unit tests 13/13, and
activity-ID lint is clean.

## 4. Unverified load-bearing claims

The full list, about 70 entries tagged `[unverified]`, is in `spec/_coordination/verification-log.md` and inline in the
sections. The ones that decide go/no-go are gated by benchmarks in the implementation plan:

| Claim | Fallback / gate |
|---|---|
| Kata 4.3.1 runtime-rs + Cloud Hypervisor on m8id nested virtualization: density, cold start, `guestBlockDevice` disk enforcement (B-DISK-01..03), ClusterIP reachability under Cilium (B-NET-01), pod logs under Kata and gVisor (B-LOG-01) | Metal nodes; gVisor standby; virtio-fs fallback at residual 12 with conditions (R2-038, signed off); NLB plus `toFQDNs` for repo-mirror |
| `temporalio-langgraph` is in public preview. LangGraph in-workflow determinism is shown by replay tests on both engines, not guaranteed upstream | The interpreter engine is a drop-in fallback (ADR-01-001) |
| Worker Deployment RPCs are marked Experimental in the protos | Pinned starts verified (V01-01). Upgrade-on-Continue-As-New is not used |
| The enterprise LLM gateway lacks per-run tokens, budgets and usage export | The Replicant gateway shim (WS-03) |
| Artifactory Curation and Xray features | A resolver proxy per ecosystem. The 72 h quarantine risk is accepted for Maven and crates |
| Karpenter `do-not-disrupt` with `expireAfter` (V09-40) | Written fallback |
| EKS per-controller service-account names (R1V-172) | Unverified |
| CloudWatch `AWS/Usage` KMS dimension values (R1V-169) | Unverified |
| Kyverno `ImageValidatingPolicy` and the CEL admission policies | Never run against a live API server. Only envtest is planned (WS-11) |
| Temporal Authorizer Go code | Not compiled (Temporal server modules unavailable); `gofmt` only |
| Codex follows a symlinked `AGENTS.md` and records its text (U-13, U-14) | Fallback: a regular file that is not captured |
| Local gateway Opus 5.5 prices | Illustrative placeholders |
| m8id.24xlarge price assumed to be 2× the 12xlarge (R1V-135) | No cost figure depends on it |

## 5. Accepted risks

| Risk | Rationale |
|---|---|
| R-04 (WS-09 residual) | Accepted for v1 with detection |
| R2-038 virtio-fs fallback residual 12 | Signed off with conditions: Kata ≥ 4.3.1, sandboxed virtiofsd, Tetragon, no restricted tenants, two approvers, 90-day expiry |
| R-09 | An STS session stays usable for up to 1 h after a run ends |
| R2-054 | Switching the graph engine for live jobs is a consented migration, not a flag |
| 72 h package quarantine | Not available for Maven and crates |
| Audit bucket in COMPLIANCE mode for 730 days | Irreversible per object. Acceptable because the bucket holds only digests and references |

## 6. Final gate results

**WS-10 `STRICT=1 validate.sh` (final, PG 17 + pgvector 0.8.2): passed, exit 0.**

- **Framework and migrations:** bootstrap, migrate, rerun, downgrade and re-upgrade pass, as do `alembic check`, squawk
  and the compat self-test. Spec DDL matches the migrations (182 facts).
- **Owner DDL:** all 17 files apply.
- **Partitions:** 39/39 isolated. The negative control still detects its 7 planted leaks.
- **eval-worker cross-tenant reads:** 12/12.
- **audit-exporter column-limited reads:** 7/7.
- **Grants:** OK. Marker roles hold no privileges.
- **RLS:** 0 tables without RLS or FORCE.
- **S3 configs:** valid.
