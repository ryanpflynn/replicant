# control-plane-web (WS-07)

Replicant's management UI: a TypeScript SPA (React 19, Vite, TanStack Query; pnpm, Vitest, Playwright), served by
nginx on **http://127.0.0.1:8400** with `/v1` proxied to control-plane-api (`REPLICANT_API_UPSTREAM`). The visual design
is the reviewed prototype in `spec/07-control-plane-and-ux/design/prototype/`: `src/styles/tokens.css` and `app.css` are
copied from it unchanged, and `web.css` adds only what a working app needs (form controls, menus, skeletons).

Screens: Jobs (Open / Needs a human / Running / In PR review / Recent / Failed 24h / My jobs, query filter, J/K),
Job detail (header, phase track, timeline, runs, gates, plan/diff/QA from the gate packet, cost, links to Grafana,
Temporal UI and Gitea; Steer, Pause/Resume, Cancel), Approvals (inbox, packet with rules, plan, diff, QA, review;
Approve / Request changes / Reject through `POST /v1/gates/{id}/decisions`), Releases (read-only), Admin (tenants,
kill switches, migrations, infrastructure links), Memory (admin empty state; 403 page for everyone else). ⌘K palette,
⇧T theme, `G J/A/R/M` navigation. Deviations: `docs/v0/DEVIATIONS.md` WEB-*.

## Local identity

No Dex/BFF in v0. The rail's bottom menu switches the local dev user (sent as `X-Replicant-Dev-User`):
grace (platform admin, approver everywhere), alice (payments engineer; requester of the sample issues), bob (payments
approver), carol (replicant-admins, operator: Memory), dave (payments viewer), erin (payments tenant admin: can cancel).

## Run and test

```sh
# image + container on the running v0 stack (from replicant/)
podman build -f web/control-plane-web/Dockerfile -t localhost/replicant/control-plane-web:dev .
cd dev/compose && podman-compose --project-name replicant -f compose.yaml -f compose.v0.yaml -f compose.v0.deps.yaml \
  --env-file .env --profile v0 up -d --no-deps control-plane-web

# typecheck + unit tests (host Node is 16, so in a container)
podman run --rm -v $PWD:/app -w /app docker.io/library/node:22 sh -c 'npm i -g pnpm@12.15.0 && pnpm install && pnpm build && pnpm test'

# dev server with HMR against the stack's API: pnpm dev (http://127.0.0.1:5173)

# live E2E + screenshots against :8400 (creates a real publish gate, approves it from the UI, merges the PR)
cd replicant && uv run --package replicant-e2e python web/control-plane-web/e2e/live_gate.py make   # prints job_id, gate_id
cd web/control-plane-web && podman run --rm --network host -v $PWD:/app -w /app \
  -e JOB_ID=job_… -e GATE_ID=gate_… mcr.microsoft.com/playwright:v1.64.1-noble npx playwright test
cd replicant && uv run --package replicant-e2e python web/control-plane-web/e2e/live_gate.py finish job_…
# cancel check: live_gate.py start → CANCEL_JOB_ID=job_… npx playwright test e2e/04-cancel.spec.ts
```

Screenshots land in `screenshots/` (`<screen>-<1440|1024>-<light|dark>.png`); the helper also reports elements that
overflow their box.
