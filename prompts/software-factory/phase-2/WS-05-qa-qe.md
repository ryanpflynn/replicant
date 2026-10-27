# WS-05: QA/QE Stage

**Output directory:** `spec/05-qa/`
**Constraints most relevant:** D-16, D-06, D-07, D-12, D-08

## Purpose

Design the stage that proves a change works before a human sees it: build, run, check health, and
for web apps, validate in a headless browser, all inside an AgentRun, producing reviewable
artifacts.

## Required outputs

### 1. Stage orchestration (`stage-design.md`)

- Where QA sits in each graph shape (coordinate with WS-01), and whether it is a separate AgentRun
  from implementation (recommended: separate, with the diff applied to a fresh checkout; decide).
- Steps inside the AgentRun: checkout + apply diff, dependency install, unit/integration test
  commands, build (including container image with a daemonless builder: BuildKit rootless or
  buildah; verify runtime compatibility with WS-02's recommendation), start dependencies, start the
  app, health checks, headless validation, teardown, artifact upload.
- Deterministic harness vs agent-driven QA: which parts run as fixed scripts from the environment
  definition, and where an agent (CLI) writes or adapts tests or explores the app. Recommend a split
  and justify it.

### 2. Dependencies of the app under test (`dependencies.md`)

Required decision with ADR. Options: sidecar containers in the AgentRun pod, services started inside
the sandbox, contract-based stubs/mocks of downstream services (generated from extracted contracts
per WS-04), ephemeral managed resources (out of scope for v1 unless justified). Testcontainers and
other Docker-socket approaches: supported or not, and the substitute (for example a rootless
Podman socket inside a Kata guest, or Testcontainers' alternative backends). Data seeding and
fixtures.

### 3. Health criteria (`health.md`)

Declarative health spec per codebase: readiness endpoints, log patterns, port checks, smoke requests,
startup timeout, stability window. Example definitions for a Go gRPC service, a Next.js app, and a
Python batch pipeline.

### 4. Headless web validation (`headless-validation.md`)

Playwright in the sandbox (browser binaries baked into images, versions pinned), test sources
(existing repo e2e tests, factory-generated smoke flows derived from the plan, visual checks),
DOM assertions, screenshots, Playwright traces and video (decide), visual diff against a baseline
from the base branch (built and run in the same AgentRun or cached), accessibility checks
(axe-core). No GUI, VNC, or window manager.

### 5. Failure feedback (`failure-feedback.md`)

Structured QA result schema (per check: status, evidence URIs, logs excerpt, classification:
code-caused vs environment vs flaky). How failures route back: to the implementer for a bounded fix
loop, to an environment-definition issue for operators, or to the user. Flake detection and retry
policy.

### 6. Artifacts (`artifacts.md`)

What is retained (build logs, test reports in JUnit XML, screenshots, Playwright traces, health
logs, image digests and SBOMs), artifact types registered per `artifact-layout.md`, and how they
appear in the approval view and PR comment.

### 7. Per-codebase configuration (`configuration.md`)

QA config schema as part of the environment definition (WS-03), with complete examples for the three
codebase types above.

## Must answer

- How does QA run an app that needs Postgres and Kafka, inside the chosen runtime, within the disk
  and memory profile?
- What does a reviewer see when QA passed? When it failed?
- How is a flaky test distinguished from a regression caused by the change?

## Acceptance criteria

- Dependency strategy decided with an ADR consistent with WS-02's runtime recommendation.
- Complete config examples that validate against the schema.
- QA result schema defined and consumed by the graph.
