# tests/e2e

`e2e01_v0.py`: the E2E-01-v0 driver (plan §1.6 X0-2; test-strategy.md §6 with the v0 gate and repair settings).
Needs the full v0 stack (`replicant dev up --profile v0` then `replicant dev seed`).

```sh
uv run --package replicant-e2e pytest -s tests/e2e/e2e01_v0.py     # or: uv run python tests/e2e/e2e01_v0.py
```

From a clean checkout (podman machine running, uv installed):

```sh
cd replicant
uv sync --all-packages
uv run replicant dev up --profile v0      # first run builds 15 images (~10 min), then ~2.5 min to healthy
uv run replicant dev seed
uv run --package replicant-e2e python tests/e2e/e2e01_v0.py
uv run replicant dev down -v              # or tests/e2e/run_clean.sh for down -v + up + seed + E2E
```

Humans: alice opens the issue (Gitea admin `Sudo`), bob approves (control-plane-api `X-Replicant-Dev-User`, CONTROL-02),
grace reads. The driver also checks that the projection (`GET /v1/jobs/{id}`) equals the `job.snapshot` Query (temporal CLI in the temporal container).

## E2E-02 notifications (`e2e02_notify.py`)

After `replicant dev up --profile v0` and `replicant dev seed`, run `uv run --package replicant-e2e python tests/e2e/e2e02_notify.py`. It asserts the
approver's gate DM, the `PR #N is ready for review` issue comment and the requester's DM, `GET /v1/me/waiting` for the
requester and the reviewer, the reminder comment and the consolidated reminder DMs after the threshold, and the
`PR #N merged · job done` comment and DM. Slack messages are read from slack-mock (`GET /_mock/messages`). It sets
alice's and bob's quiet hours to none first (the default 20:00-08:00 + weekends would hold the reminder). It needs a
sample repo that still has the bug (fresh `dev down -v`, or revert the merged fix on `main`).

The 120 s PR idle threshold is a test-only in-memory override (`PUT /v1/admin/dev/notify-overrides`), re-asserted
while waiting and restored at the end; nothing is written to `dev/compose/.env`.

## E2E-03 Slack phase 2 (`e2e03_slack.py`)

Three real jobs: Approve from the gate DM button (DM and the channel gate message re-rendered, PR notice threaded
under the job's parent message in #payments-approvals), a late Slack click on a gate already approved in the UI
("Not recorded: Already approved in the UI"), Reject through the modal (job rejected, thread terminal notice); then
alice's daily digest (2 PRs > threshold 1, no per-item reminder DM) while bob (1 item) still gets the reminder DM; then
App Home via `app_home_opened`. It closes both PRs at the end, so the sample repo keeps its bug. Needs seed step 08.
