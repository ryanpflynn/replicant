# Registries

Routes `/registries/:kind` (`agent | environment | graph | policy | nodecatalog`) and
`/registries/:kind/:name[/versions/:v]`. Data: `GET /v1/registry/definitions/{kind}`,
`…/{name}/versions`, `GET /v1/registry/rightsizing-proposals`. Read for `release:read`; there is
no edit in the UI: definitions change through the config repo (D-12, C-DEF §4), and the UI links to
the source file at the publishing commit.

```
┌ Registries / Environments ───────────────────────────────────────────────── [⌘K] ◐ ┐
│ [Agents 9][Environments 14][Graphs 8][Policies 31][Node catalogs 4]   scope: all ▾   │
├────────────────────────┬──────┬──────────┬───────────┬───────────┬──────────────────┤
│ NAME                   │ SCOPE│ LATEST   │ STATE     │ IN BUNDLES│ RIGHT-SIZING     │
│ go-service             │global│ @13      │ active    │ 2         │ —                │
│ nextjs-app             │global│ @7       │ active    │ 2         │ ◆ open rsp_01m5… │ (1)
│ go-service.payments    │tenant│ @3       │ active    │ 31 derived│ —                │
├────────────────────────┴──────┴──────────┴───────────┴───────────┴──────────────────┤
│ nextjs-app @7  sha256:1b3d…  commit 4e1d9c0 · published Oct 22 by svc_config-publisher│
│ Versions  @7 active  @6 deprecated  @5 deprecated          [Compare @6 ↔ @7]          │ (2)
│ Right-sizing proposal rsp_01m5… · trigger disk_failures (3 in 7 days)                 │ (3)
│   workspace sizeLimit 40Gi → 46Gi · p95 34.8 GiB (n = 146) · est. +$41/month          │
│   PR acme/replicant-config#812 · waiting for CODEOWNERS review   [Review PR ↗]          │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

1. Open proposals are attention-coloured because a human must act (WS-03 §5.6).
2. Compare shows a JSON diff of the two version documents (canonical order), with fields that change
   the bundle closure highlighted.
3. The approval is the config-repo PR review by CODEOWNERS; the UI shows the evidence and the PR state.

## States

Loading (table skeleton) · Empty kind ("No policies of type budgets yet") · Version revoked (danger
banner "Revoked: new bundles cannot select this version; promoted bundles containing it were
notified") · Error (region).
