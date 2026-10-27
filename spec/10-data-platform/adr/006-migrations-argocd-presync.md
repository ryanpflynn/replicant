# ADR-WS10-006: Migrations run as an Argo CD PreSync hook Job

- **Status:** Accepted (WS-10, 2026-10-24)
- **Constraints:** D-26 ("Argo CD PreSync or a Helm pre-upgrade hook; decide"), C-ARCH (`migrate` Job)

## Context

| Fact | Source |
|---|---|
| Argo CD v3.5.6 is current. PreSync "Executes prior to the application of the manifests". "If any of them fails the whole sync process will stop and will be marked as failed". The default delete policy is `BeforeHookCreation`. Avoid `ttlSecondsAfterFinished` on hook Jobs, because Argo CD reads the Job to learn the hook phase | [verified: https://github.com/argoproj/argo-cd/blob/master/docs/user-guide/sync-waves.md, 2026-10-24] |
| Argo CD maps `helm.sh/hook: pre-install` and `pre-upgrade` to PreSync. It "cannot know if it is running a first-time install or an upgrade", so both run | [verified: argo-cd docs/user-guide/helm.md, 2026-10-24] |
| Helm (v4.3.1) `pre-upgrade` Jobs block the release until they finish. The default `--timeout` is 5 min | [verified: https://github.com/helm/helm-www/blob/main/docs/topics/charts_hooks.md, 2026-10-24] |

## Options

1. **Helm `pre-upgrade` hook**, deployed by `helm upgrade` from CI.
2. **Argo CD PreSync hook (chosen)**, with the Job rendered by the same Helm chart but annotated with
   `argocd.argoproj.io/hook: PreSync`.
3. Init containers in every service. Rejected: they race (N pods migrate at once), and they give
   service pods DDL credentials.

## Decision

Option 2. The platform is GitOps (Argo CD per cluster). PreSync blocks every Deployment change in the
`replicant-control` Application until migrations succeed. The ordering is:

1. wave −3 ServiceAccount and RBAC
2. wave −1 PreSync `replicant-db-migrate` Job ([config/k8s/migrate-job.yaml](../config/k8s/migrate-job.yaml))
3. normal sync: the Deployments, whose startup compatibility check confirms the revision

Other Applications that read the database (`replicant-workers`, `replicant-context`, `replicant-integrations`,
`replicant-eval`) declare a sync dependency on `replicant-control` through ApplicationSet progressive
sync ordering. Their pods also refuse readiness until the schema is compatible.

Helm hook annotations are **not** added as well. The Argo annotations are authoritative, and doubling
them would run the Job twice under Argo's mapping. A non-Argo install (local `kind` profile)
uses `replicant-db migrate apply` from the dev CLI.

## Consequences

- A failed migration fails the sync, so no service updates. The old pods keep running against an
  unchanged schema, because each revision is its own transaction. The alert is
  `ReplicantDbMigrationFailed` (schema-management.md §7.6).
- Job timeout: `activeDeadlineSeconds: 1800`. Longer work belongs in batched data migrations, never
  inline.
- Temporal's own schema Job uses the same pattern in the `temporal` Application
  ([config/temporal/temporal-schema-job.yaml](../config/temporal/temporal-schema-job.yaml)).

## Revisit when

- The platform leaves Argo CD.
