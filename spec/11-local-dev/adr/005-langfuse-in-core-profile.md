# ADR-11-005: Langfuse runs in the `core` profile, not only in `observability`

- **Status:** Accepted (WS-11, 2026-10-24); contract text applied by the orchestrator (CCR-11-03, C-ARCH 1.0.1)
- **Constraints:** D-03, D-17, D-20; C-DEF §3, §4.3, §6

## Context

The brief lists Langfuse under the `observability` profile. But Langfuse prompt management is not only
observability in this design: bundle builds resolve `prompt/<name>@label:production` by calling
Langfuse `get_prompt`, record `{name, version, digest}` in the bundle, and the context assembler fetches
prompts from Langfuse by `(name, version)` at run time (C-DEF §4.3, §6). Without Langfuse, no bundle
can be built and no job can run.

## Options

1. Langfuse only in `observability`; a file-based prompt stub in `core`. A different code path for the
   most behavior-relevant configuration in the system. Rejected (D-20: same code paths).
2. **Langfuse (web, worker, ClickHouse, Valkey, `postgres-langfuse`) in `core` (chosen).** The
   `observability` profile adds Grafana, Loki, Tempo, Prometheus, Alloy.

## Decision

Option 2. Langfuse `4.50.0` (2026-10-02) images, headless initialization (`LANGFUSE_INIT_*`) creates
org `replicant-local`, project `replicant-local`, and the project API keys before any Replicant service
starts. Its own Postgres is separate from the Replicant database (D-09). ClickHouse is pinned to
`25.12.11.4-alpine`, the 25.12 line that the upstream compose file uses. [verified:
https://github.com/langfuse/langfuse/blob/main/docker-compose.yml; Docker Hub tags; 2026-10-24]

Trace export to Langfuse goes through Alloy, so traces appear in Langfuse only with
`--with-observability`. Prompt management works without it.

## Consequences

- `core` needs about 2.5 GB more RAM (ClickHouse ~1.5 GB, web + worker ~1 GB) than it otherwise would.
  stack.md §6 sizes this in.
- Langfuse applies its own Postgres and ClickHouse migrations at startup by default
  (`LANGFUSE_AUTO_POSTGRES_MIGRATION_DISABLED` / `LANGFUSE_AUTO_CLICKHOUSE_MIGRATION_DISABLED` default
  `false`). [verified: https://langfuse.com/self-hosting/configuration, 2026-10-24]. This resolves the
  `[unverified]` in C-PG §1 for local use; WS-08 confirms for production.
- Langfuse's docs position Docker Compose as a non-production setup and recommend ≥ 4 cores and
  16 GiB for a VM dedicated to it. [verified:
  https://langfuse.com/self-hosting/deployment/docker-compose, 2026-10-24]. Locally it shares the VM;
  ingestion volume is tiny, so it runs in far less (measured target in stack.md §6, to be confirmed).

## Revisit when

- Prompts move out of Langfuse into the registry (a WS-03/WS-06 decision), at which point Langfuse can
  return to the `observability` profile.
