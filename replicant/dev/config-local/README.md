# dev/config-local (v0)

Local overlays published by `replicant dev seed` (step `04-registry-publish`, `POST /v1/registry/publish`, source
`config_repo`) and the bundle request it builds (step `05-bundle-build`, `bundles/requests/local.yaml`).

`definitions/` starts as **verbatim copies** of the spec examples (agents implementer-claude, reviewer-codex,
qa-runner; environment go-service; graph bugfix; policies budgets, codebase-bindings, repair-limits,
router-rules, runner-polling). They are the starting point for v0, not reviewed local values: WORKER and CONTROL
may edit files here (PLATFORM owns the directory and the seed mechanism; see docs/v0/INTERFACES-LOG.md PLATFORM-04).
Validate against the kind schemas in `spec/03-agents-and-tools/schemas`, `spec/01-orchestration/schemas` and
`spec/06-releases-and-evals/schemas` before changing them.
