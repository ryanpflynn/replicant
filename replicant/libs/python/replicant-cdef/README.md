# replicant-cdef (CONTROL; shared with WORKER)

One implementation of the C-DEF 3.0.0 registry digests and S3 layout (definitions-contracts.md §4.1, §4.3, §6;
release-bundle.md §2; C-ART `registry_blob`), with pinned test vectors in `src/replicant_cdef/vectors.json`.

| Object | digest | blob key | blob body |
|---|---|---|---|
| definition | `sha256(JCS(document))` | `blobs/sha256/<hex>.json` | `JCS(document)` |
| release bundle | `sha256(JCS(spec))`, `rb_ = b32_130(digest)` | `blobs/sha256/<hex>.json` | `JCS(spec)` (not the ReleaseBundle document) |
| bundle pointer | — | `bundles/<rb>.json` | `JCS({release_bundle_id, digest})` |

Readers verify `sha256(body) == digest` on the raw bytes (`verify_blob`, `read_bundle_spec`).

```sh
.venv-ctl/bin/python -m pytest -q libs/python/replicant-cdef/tests
```
