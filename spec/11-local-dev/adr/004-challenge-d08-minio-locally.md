# ADR-11-004: Challenge: D-08. "MinIO locally" names a product that is no longer distributed

- **Status:** **Accepted** by the orchestrator (CCR-11-02 applied; C-ARCH 1.0.1, D-08 amended). Fix round
  (R1-269): SeaweedFS runs as compose profile `s3-seaweedfs` (always enabled by `replicant dev up`), and `s3-minio`
  replaces it under the same `s3.replicant.localhost` name, so `--s3 minio` is a real swap.
- **Constraint challenged:** D-08, the clause "S3 in production, MinIO locally". Also C-ARCH §1.4 and
  §3, which list `minio` as a local component.

## Context and evidence

- The `minio/minio` repository README says "THIS REPOSITORY IS NO LONGER MAINTAINED", states the
  community edition is source-only ("We will no longer provide pre-compiled binary releases for the
  community version"), and the repository was archived on 2026-04-25. [verified:
  https://github.com/minio/minio, 2026-10-24]
- The last community release tag is `RELEASE.2025-10-15T17-29-55Z`. [verified: `git ls-remote --tags
  https://github.com/minio/minio`, 2026-10-24]
- The Docker Hub repository `minio/minio` returns no tags through the Hub API today. Third-party
  reports say image publishing to Docker Hub and Quay stopped in October 2025. [verified: Docker Hub API
  query, 2026-10-24; date of the stop is from secondary sources and unverified]
- Langfuse's own compose file now uses `cgr.dev/chainguard/minio`, a third-party rebuild. [verified:
  https://github.com/langfuse/langfuse/blob/main/docker-compose.yml, 2026-10-24]

Designing the local stack on an archived, unpatched object store would mean every developer runs a
server that will never receive a security fix, and pinning an image digest that may disappear.

## Requirements the local S3 server must meet (from C-ART and C-RUNNER)

PutObject with `If-None-Match: *` returning 412; object tagging on upload (`x-amz-tagging`); bucket
versioning; Object Lock in governance mode for the `audit` class; tag-filtered lifecycle rules
(desirable); presigned GET; multipart upload; STS `AssumeRole` with a session policy for per-run
prefix-scoped credentials; SSE (KMS desirable).

## Options

1. **Keep MinIO, build from the last source tag.** Meets the API needs today; unmaintained; the build
   takes minutes on every clean machine.
2. **SeaweedFS (`chrislusf/seaweedfs:4.48`, 2026-09-28) (chosen).** Actively released (4.49 on
   2026-10-23). Its S3 API documentation lists object tagging, versioning, Object Lock and retention,
   conditional headers on all operations, presigned URLs, multipart, SSE-S3 and SSE-KMS, and an STS
   `AssumeRole` endpoint on the S3 port. [verified:
   https://github.com/seaweedfs/seaweedfs/wiki/Amazon-S3-API and
   https://github.com/seaweedfs/seaweedfs/releases, 2026-10-24]. Session-policy semantics for
   AssumeRole and tag-filtered lifecycle are `[unverified]`.
3. **Garage, RustFS, Ceph RGW, LocalStack.** Garage lacks Object Lock and STS `[unverified]`; Ceph RGW
   has the fullest S3 surface but is heavy for a laptop; LocalStack's S3 is an emulator, not a server
   `[unverified: current LocalStack licensing]`.

## Proposed replacement text for D-08 (first sentence)

> "S3 in production; locally, an S3-compatible open-source server (SeaweedFS by default) behind the
> same `replicant-storage` client, which uses only the S3 API, so the local server is replaceable."

## Is the replacement a strict superset?

No, it is a substitution of one local product for another. Per shared context §3, WS-11 therefore
**keeps a MinIO path available as written**: compose profile `s3-minio` builds MinIO from
`RELEASE.2025-10-15T17-29-55Z` source (`dev/minio-src/Dockerfile`) and serves the same buckets on the
same port, and the C-ARCH alias `minio` is honored. The default is SeaweedFS because it is maintained.
If the orchestrator rejects this challenge, flipping the default is one line in `replicant dev up`
(`REPLICANT_S3_IMPL=minio`).

## Consequences

- Every S3 client in Replicant must be configured only through `REPLICANT_S3_*` variables and use
  path-style addressing locally; no MinIO admin APIs (`mc admin`) are used anywhere in the design.
- The Runner's per-run credential vending must work with SeaweedFS STS, or fall back to per-run IAM
  identities created through SeaweedFS's IAM API and deleted at terminal (WS-02; XD-11-03).
- Contract test RC-ART-01..08 (test-strategy.md §5.4) runs against both S3 implementations in CI so a
  divergence is caught before it reaches a developer.

## Revisit when

- A maintained MinIO-compatible distribution with official images becomes the obvious default, or
  SeaweedFS fails one of the RC-ART tests.
