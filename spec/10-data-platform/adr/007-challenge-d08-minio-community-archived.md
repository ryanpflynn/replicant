# ADR-WS10-007: Challenge: D-08. MinIO community edition is archived; choose the local S3 store by conformance

- **Status:** Raised by WS-10 on 2026-10-24. **Resolved by orchestrator amendment** of D-08 via CCR-11-02
  (C-ARCH 1.0.1): the local store is SeaweedFS (compose service `s3`), with MinIO built from source as
  profile `s3-minio`. This ADR keeps the evidence and the conformance requirement. WS-11 owns the local
  stack and the conformance suite.
- **Constraint challenged:** D-08, "S3 in production, **MinIO locally**". D-20 lists MinIO in the
  one-command local stack.

## Evidence

- MinIO's community repository is archived. It went "source code only. We will no longer provide
  pre-compiled binary releases" (2025-10-15), then into "maintenance mode": "No new features,
  enhancements, or pull requests will be accepted" (2025-12-03), then "THIS REPOSITORY IS NO LONGER
  MAINTAINED" (2026-02-11), and was archived on 2026-04-25. The last community tag is
  `RELEASE.2025-10-15T17-29-55Z`. The Docker Hub repositories `minio/minio` and `minio/mc` no longer
  resolve [verified: https://github.com/minio/minio (commit history and archive banner); Docker Hub
  API, 2026-10-24].
- Parity gaps even for the last release: Object Lock can be enabled **only at bucket creation**
  (AWS allows enabling it on existing buckets). The static KMS key (`MINIO_KMS_SECRET_KEY`) supports
  exactly **one** key name, so per-tenant SSE-KMS keys (C-ART §1) need KES or MinIO KMS [verified:
  minio source `internal/kms/secret-key.go`, docs/bucket/retention/README.md, 2026-10-24].
- An unmaintained server with no security fixes in every developer's stack, and in the `kind` CI
  profile, is a supply-chain liability. D-20 makes it the most-run piece of software in Replicant.

## What Replicant needs from the local store (from C-ART and object-storage.md)

| Capability | Why | AWS S3 |
|---|---|---|
| `If-None-Match: *` conditional PUT, including multipart complete | write-once manifests, registry blobs (C-ART §2 rule 3) | yes |
| Object tags + lifecycle filters on tags | retention classes (C-ART §1) | yes |
| Versioning + Object Lock (governance) | audit bucket | yes |
| SSE-KMS with several keys | per-tenant keys, crypto-shred tests | yes |
| Bucket policies with `s3:RequestObjectTag/*`, `s3:x-amz-server-side-encryption*` conditions | policy tests | yes |
| Presigned GET (SigV4) with expiry enforced | UI downloads | yes |

Verified support among actively maintained candidates (source-level, 2026-10-24):

| Store | Tag lifecycle | Object Lock | If-None-Match | Multi-key SSE-KMS | Bucket policies | Status |
|---|---|---|---|---|---|---|
| SeaweedFS 4.49 (Apache-2.0) | yes | yes (governance bypass) | yes (PUT) | yes (local, AWS, OpenBao providers) | partial `[unverified]` | active |
| Ceph RGW v21.2 | yes | yes | yes (PUT and complete) | yes (Vault/Barbican/KMIP) | yes | active, heavy |
| RustFS 1.0.3 (Apache-2.0) | yes | yes | yes | yes (local/static dev KMS) | `[unverified]` | active, young |
| Garage v2.4.1 | **no** tags, no versioning, no Object Lock, no SSE-KMS | | | | | unsuitable |
| LocalStack | metadata only (lifecycle not enforced); accepts expired presigned URLs by default | | | | | repo archived; plan required |

Sources: SeaweedFS `s3lifecycle/rule.go`; Ceph https://docs.ceph.com/en/latest/radosgw/s3/; RustFS
README; https://garagehq.deuxfleurs.fr/documentation/reference-manual/s3-compatibility/;
https://docs.localstack.cloud/aws/services/s3/ [verified 2026-10-24].

## Proposed replacement text for D-08 (local clause)

> "S3 in production. Locally, an **actively maintained, S3-compatible object store selected by the
> Replicant's S3 conformance suite** (WS-11). The suite covers conditional writes, tag-filtered
> lifecycle, versioning and Object Lock, multi-key SSE-KMS, bucket-policy conditions, and presigned
> URL expiry. The default is **SeaweedFS**. MinIO is no longer maintained in open source and is not
> used."

## Is the replacement a strict superset?

Yes. It keeps D-08's intent (a local S3 store with production-like semantics) and replaces the named
product, which no longer exists as maintained open source. Per shared-context §3, WS-10 keeps designing
against the constraint: every local artifact (bucket names `local-replicant-local-<class>`, lifecycle
JSON generated for `env=local`, bucket-init script) uses only S3 APIs. Nothing is MinIO-specific (no
`mc` commands), so the store is swappable without touching producers. If the challenge is rejected,
the same files work with a source-built MinIO `RELEASE.2025-10-15T17-29-55Z` image, with the parity
gaps listed above.

## Consequences

- WS-11 builds `s3-conformance` (pytest + boto3). It runs against the local store **and** against a
  real S3 bucket in the staging account in CI. Every row of the capability table is a test case.
  A local store that fails a row is listed as a parity gap in WS-11 `parity.md`.
- Local SSE-KMS uses the store's dev KMS with one key per seeded tenant (`payments`, `search`,
  `replicant-eval`). Crypto-shred is tested only in staging (real KMS).

## Revisit when

- A maintained MinIO-compatible community distribution with full parity becomes the obvious default.
