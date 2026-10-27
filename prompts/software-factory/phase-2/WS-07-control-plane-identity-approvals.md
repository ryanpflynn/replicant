# WS-07: Control Plane, User Experience, Identity, and Approvals

**Output directory:** `spec/07-control-plane-and-ux/`
**Constraints most relevant:** D-02, D-18, D-19, D-14, D-03

## Purpose

Design every surface humans touch: the control-plane UI, Slack, GitHub, the CLI, and the identity
and authorization model behind them. Design the experience concretely (screens, states, messages,
empty and error states), not just the plumbing.

## Users

- **Requesters**: engineers who trigger work, watch it, steer it, and take it over.
- **Approvers / reviewers**: engineers and leads who decide at gates and review PRs.
- **Platform operators**: manage releases and versions, definitions, policies, quotas, and
  incidents.
- **Factory admins** (members of the admin Okta group): the only users with memory management tools
  (D-25), in addition to anything else granted to them.
- **Tenant admins**: manage their tenant's policies and approval channels. No memory management
  unless they are also factory admins.

## Required outputs

### 1. Information architecture and screens (`ui/`)

Wireframes (ASCII or Mermaid, annotated) with states for:

- **Job list and job detail**: live phase and node, timeline merged from Temporal events and
  AgentRun status, cost so far, artifacts (diff viewer, QA screenshots, transcripts), Langfuse trace
  and Grafana log deep links, pinned Release Bundle, memory entries used.
- **Steer / cancel / pause / take over** actions, with what each does and its confirmation UX.
  Take-over delivers the branch, a workspace archive, the handoff document, and transcript.
- **Approval inbox**: pending gates across channels, what the approver sees (plan, diff, QA,
  risk rationale and the rules that fired, cost, memory used), approve / reject / request changes,
  bulk actions where safe, expiry and escalation indicators.
- **Releases and versions**: list Release Bundles and worker deployment versions, bundle diffs,
  pinned workflow counts per version, ramp controls, rollback, drain, retire, migration of eligible
  waiting workflows, eval results per bundle (operations defined by WS-01 and WS-06).
- **Registries**: agent, environment, graph, and policy definitions with version history.
- **Memory curation** (admin group only, D-25): candidate queue, entry detail with provenance and
  usage, conflicts, bulk disable (requirements from WS-04). Show how the navigation looks for an
  admin vs a non-admin, the read-only memory panel non-admins see on job detail and approval views,
  and what a non-admin sees if they follow a direct link to a memory management URL.
- **Cost and quotas**: per tenant, per job, budgets and alerts.
- **Admin**: tenant config, approval channel configuration, identity link status, database
  migration status (current revision, pending, history) and the admin operations that replace
  direct database access (D-26).

### 1a. Visual design system and rendered prototype (`design/`)

Meet the visual design bar in shared context section 4. This is a deliverable, not a styling pass.

- **Design direction** (`design/direction.md`): the product's visual identity in a page: references
  you are drawing from and what specifically you take from each, what makes this tool recognizable,
  and how each banned pattern from shared context is avoided on each screen.
- **Tokens** (`design/tokens.json` plus a rendered token sheet): type scale (choose typefaces
  deliberately, including a monospace; justify), spacing scale, radii, borders, elevation (minimal),
  neutral ramp, one accent, semantic status colors for job/gate/run/memory states, light and dark
  themes, focus rings. Check contrast to WCAG 2.2 AA and status-color distinguishability for common
  color-vision deficiencies; report the numbers.
- **Component inventory** (`design/components.md`): tables, timelines, diff viewer, log viewer,
  status pills, approval panel, command palette, empty/loading/error states. If you build on a
  headless or copy-in library (for example Radix primitives or shadcn/ui), specify how it is restyled
  so the stock look never ships.
- **Rendered prototype** (`design/prototype/`): static HTML/CSS (or a small Vite app) of at least
  the job detail, approval inbox, releases/versions, and memory curation screens, in both themes,
  populated with realistic data (real-looking repo names, diffs, Temporal events, costs, approver
  names). Render screenshots with headless Playwright at 1440 px and 1024 px widths into
  `design/screenshots/`.
- **Self-critique**: review your own screenshots against the banned list and the design direction,
  write what you changed and why in `design/critique.md`, and iterate at least twice before
  finishing. R2 will review the screenshots independently.

### 2. Frontend and backend architecture (`architecture.md`)

Framework choices, the control-plane API (OpenAPI), how it reads the Postgres read models and calls
Temporal (Updates for gates and steering), real-time updates (SSE or WebSockets; decide),
authorization middleware, and the Temporal UI's role (operators only, behind Okta, with the codec
server).

### 3. GitHub App (`github-app.md`)

Implements D-24. Verify every API detail against current GitHub documentation.

- **App manifest** (`github-app/manifest.json`, real and valid) for the GitHub App manifest flow, so
  a developer can register their own dev app with one click and one command (for example
  `factory dev github-app create --org <personal-org>`), which completes the manifest code exchange
  and stores the app ID, private key, and webhook secret in the local secret store (never in the
  repo).
- Minimum permissions with justification for each (issues, pull requests, contents, checks,
  metadata, members if needed for permission checks), and subscribed events (`issues`,
  `issue_comment`, `pull_request`, `pull_request_review`, `pull_request_review_comment`,
  `installation`, `installation_repositories`, and others you need).
- Webhook ingestion service: signature verification (`X-Hub-Signature-256`), delivery dedupe on
  `X-GitHub-Delivery`, replay of missed deliveries via the deliveries API, fast acknowledgement and
  async processing, and mapping events to `JobRequest`s or to Temporal Updates on existing jobs.
- Trigger rules: which issues start a job (label, assignee, `/factory` command, or any opened issue
  in opted-in repos; configurable per installation/repo), and the acknowledgement comment the app
  posts back on the issue with a link to the job.
- Installation-token use: only the Publisher and the webhook/reply service hold installation
  tokens; tokens are minted per action with the narrowest repository and permission scope.
- **Dev vs production registrations**: same code and manifest, separate apps, config selecting which
  app the factory runs as, and how a developer's dev app is isolated from production data.
- Webhook delivery to a laptop: choose the forwarding mechanism (smee.io, `gh webhook forward`, or a
  tunnel), with the trade-offs and the exact setup.

### 3a. Triggers and the CLI (`triggers.md`)

GitHub (via the GitHub App above), Jira, Slack (slash command, message
shortcut), UI form, factory CLI (`factory run`, `factory status`, `factory approve`,
`factory takeover`), and schedules. Normalization into `JobRequest`, dedupe of duplicate triggers,
acknowledgement messages in the originating channel.

### 4. Identity and authorization (`identity.md`)

- Okta OIDC (authorization code + PKCE for the UI, device flow or token exchange for the CLI), SCIM
  provisioning of users and groups into the `identity` schema, deprovisioning effects on running jobs
  and pending approvals.
- **Identity linking**: Okta ↔ GitHub (GitHub Enterprise SAML/SCIM linkage or an OAuth link flow) and
  Okta ↔ Slack (Enterprise Grid SCIM or an OAuth link flow); what happens with unlinked identities.
- Authorization model: roles and permissions, group-to-permission mapping per tenant, plus the
  **repository permission check** for the requester and approvers (GitHub API collaborator
  permission or equivalent) to prevent confused-deputy actions. Policy engine choice (for example
  OPA/Cedar or in-code; decide).
- Admin-only memory management (D-25): the permission, its mapping to the admin Okta group, the
  server-side check on every memory-management endpoint, audit of denials, and behavior when SCIM
  removes someone from the group mid-session.
- Enforcement points: every entry (UI API, Slack handlers, GitHub webhooks, CLI, Temporal Update
  validators via activities, Runner API callers, MCP context servers).
- Local stub IdP (for example Dex or Keycloak in the compose stack) with seeded users and groups.

### 5. Approvals (`approvals.md`)

- Channel selection policy per tenant, workflow type, and risk level.
- **Slack**: Block Kit message design, interactive button handling, request signature verification,
  identity resolution, message updates after a decision in any channel, reminders and escalations.
- **GitHub**: requested reviewers, labels, CODEOWNERS, and how a PR review maps to a gate decision.
  Address that the factory's GitHub App cannot approve its own PR and how branch protection
  interacts with factory gates (factory gate before PR creation vs GitHub review after).
- **UI**: approval inbox actions.
- All paths produce the same Temporal Update with the decider's Okta identity; first-wins;
  idempotent duplicates; audit records.
- Reminder/escalation/expiry policy schema, and what the requester sees at each stage.
- Approval fatigue: batching, risk-based routing, delegation, out-of-office handling.

### 6. Notifications (`notifications.md`)

What each persona is notified of, through which channel, with what content; preferences; digest
mode.

## Must answer

- A user is removed from the approver group in Okta while their approval click is in flight. What
  happens?
- The same gate is approved in Slack and rejected in GitHub within one second. What happens in each
  channel?
- What does a requester see in the first 60 seconds after triggering a job?
- How does an operator find and roll back the release that caused a spike in failed jobs?

## Acceptance criteria

- The prototype renders in both themes with realistic data, screenshots exist, contrast numbers are
  reported, and the self-critique shows no banned pattern remaining without written justification.
- The GitHub App manifest is valid, and a developer can go from nothing to an issue-triggered job on
  a personal org by following `github-app.md`.
- Every screen has annotated wireframes including empty, loading, and error states.
- Every enforcement point is listed with its check.
- Approval race conditions and identity edge cases have defined outcomes.
