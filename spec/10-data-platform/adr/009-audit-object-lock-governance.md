# ADR-WS10-009: Audit bucket Object Lock in COMPLIANCE mode, with no bypass role

- **Status:** Accepted (WS-10). Revised in wave 1 (2026-10-24) after the orchestrator's R2-005 decision; the
  first version chose governance mode with a break-glass erasure role.
- **Constraints:** D-08, D-19, D-21; C-ART 2.0.0 §1 (decision first recorded against C-ART 1.9.0) (`audit`: "mode (governance or compliance) is WS-10's
  decision"), rule 4a; R2-004, R2-005.

## Context

Until wave 1, the audit bucket held two kinds of content that might someday need deleting:
- approval records containing approver free text and job titles (R2-004);
- the full workflow history export, whose codec-encrypted payloads can contain a leaked secret (R2-005).

Governance mode existed so that a `replicant-leak-purge` or erasure role could hold `s3:BypassGovernanceRetention`.
R2 showed that bypass to be weaker than it looks: it is a standing IAM permission, the two-person approval is
a workflow gate rather than an AWS control, and anyone who can run code as the worker could skip the gate.

Wave 0 changed the contract (C-ART 1.9.0):
- `approval_record` is reference-only, and the free text lives in `approval_text` in `artifacts`.
- The history export is split. `workflow_history_skeleton` (payload-free, with payload digests) goes to
  `audit`, and the full `workflow_history_export` goes to `artifacts`, where it is purgeable.
- **No role holds `s3:BypassGovernanceRetention`.**

Every object type left in `audit` is therefore free of content: approval records, history skeletons,
audit event and partition exports, the Okta System Log export, deletion-ledger entries, and leak-purge
records. None can contain a secret or memory text (rule 5), so none ever needs deleting before expiry.

| Fact | Source |
|---|---|
| Compliance mode: a protected version "can't be overwritten or deleted by any user, including the root user", and its retention can't be shortened. Governance can be bypassed with `s3:BypassGovernanceRetention` plus the header | [verified: https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html, 2026-10-24] |
| A **legal hold** is independent of the retention mode. It "remains in effect until removed", needs `s3:PutObjectLegalHold` to place or remove, and bypassing governance does not lift it | same |
| Lifecycle runs on locked objects, but "a locked version … cannot be deleted by a S3 Lifecycle expiration policy" | [verified: https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-managing.html, 2026-10-24] |
| Object Lock can be enabled on an existing bucket and never disabled. Replication requires Object Lock on the destination, and the retention is replicated | [verified: https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-configure.html; …/replication-what-is-isnot-replicated.html, 2026-10-24] |

## Options

1. **Governance with no role holding the bypass.** This is equivalent to compliance in practice, except that
   any administrator who gains IAM write access can grant the bypass to themselves.
2. **Compliance (chosen).** Not even the root user can delete or shorten. The mode matches the bucket's
   contents: evidence that never contains content.

## Decision

- `audit` default retention: **COMPLIANCE, 730 days**
  ([config/s3/prod/audit.object-lock.json](../config/s3/prod/audit.object-lock.json)). r7y objects set
  `x-amz-object-lock-mode: COMPLIANCE` and a retain-until of 7 years at upload. Producers' IAM caps it at
  2,557 days through `s3:object-lock-remaining-retention-days` (WS-09 policies), so a typo cannot lock an
  object for a century.
- The bucket policy denies `s3:BypassGovernanceRetention` to everyone (`NobodyBypassesRetention`, present in
  every bucket) and denies every delete (`NoDeletes`). Lifecycle still removes delete markers and versions
  once retention ends.
- **Rollout.** Staging runs compliance mode with a **1-day** default for 30 days. The goal is to prove that
  every audit writer sends correct retention headers and that exporters never need to rewrite an object.
  Production then switches to compliance with 730 days. The setting is irreversible per object version, and
  the 30-day staging trial is how that risk is managed. Local (WS-11 parity P-05) uses GOVERNANCE with a
  1-day lock, so a developer can wipe their stack.
- **Legal holds.** Role `replicant-<env>-legal-hold`, assumable only by the legal team through WS-09's approval
  flow, is the only principal allowed `s3:PutObjectLegalHold` (`LegalHoldsOnlyByLegalRole`).
  - Holds are allowed in exactly two places (orchestrator ruling on CCR-10-10; C-ART 1.10.3): `audit`, which is
    never purged, and the legal bucket. The tenant buckets (`artifacts`, `transcripts`, `session`) have no
    Object Lock, so purgeable content cannot be held in place.
  - Litigation preservation of purgeable content must **not** go into `audit`, because that would put content
    under a compliance lock. Legal copies the named job prefixes into the legal bucket
    `<org>-replicant-<env>-legal` (CCR-10-10, accepted; [config/s3/prod/legal.policy.json](../config/s3/prod/legal.policy.json)):
    Object Lock **governance** with no default retention, key `alias/replicant/<env>/legal`, every write write-once.
    Two principals touch its objects: `replicant-<env>-legal-hold` (writes, reads, places and releases holds) and
    `replicant-<env>-leak-purge` (reads, writes the superseding object, and `DeleteObjectVersion`; it cannot place
    holds or plain-delete).
  - Interactions:
    1. Leak purge never touches `audit`, so a hold there cannot block a purge.
    2. A leaked secret inside a legal-bucket copy is purged like any other copy **unless that version is held**.
       S3 refuses `DeleteObjectVersion` on a held version, so the purge records `blocked_by_legal_hold` in the
       purge record and escalates through the `leak_purge` gate to legal and security. The secret is rotated
       regardless (lifecycle.md §6), so the residual risk is disclosure of a dead credential.
    3. Tenant offboarding: held objects outlive both their retention and the offboarding, by design.
    4. Lifecycle cannot remove a held object. A hold is removed explicitly when the matter closes, which is
       audited (`audit.emit` event `audit.legal_hold_released`).
- Bucket Keys are enabled, with the platform `audit` key.

## Consequences

- No GDPR erasure path exists inside `audit`, and none is needed: it holds IDs, enums, digests, and timestamps
  only. Any erasure request that touches content is served from `artifacts`, `transcripts`, or `session`, plus
  Postgres, and is recorded in the deletion ledger. That ledger is itself in `audit` and is content-free.
- A producer that ever writes content into `audit` creates a 2-to-7-year problem that cannot be fixed. Two
  guards prevent it: the WS-08 exporters' schema validation (skeleton: no payload fields), and the daily
  storage audit flagging any object larger than its type's C-ART maximum (16 MiB for skeletons, 64 KiB for
  approval records).
- A wrong default retention cannot be shortened, which is why the staging trial exists.

## Revisit when

- An object type that can carry content must live in `audit`. That would be a C-ART change, and this ADR would
  have to be revisited first.
