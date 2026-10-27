# Component inventory

**Owner:** WS-07 · Implementation target: `control-plane-web` (TypeScript, React 19.3, Vite 8.3) ·
Tokens: [tokens.json](tokens.json) → generated `src/styles/tokens.css` (same file as
[prototype/tokens.css](prototype/tokens.css)).

## 1. Foundation and restyling policy

| Layer | Library (version checked on npm, 2026-10-24) | How it is used |
|---|---|---|
| Behaviour primitives | `radix-ui` 1.8.1 (unified package) | Dialog, Popover, DropdownMenu, Tooltip, Tabs, Checkbox, Toggle, ScrollArea, VisuallyHidden. Radix ships **unstyled**; every part receives our classes. No Radix Themes |
| Command palette | `cmdk` 1.1.1 | Behaviour only (filtering, keyboard). Markup is our `Palette` component; styling from `app.css` §Command palette |
| Tables, virtualisation | `@tanstack/react-virtual` 3.14 | Lists over 200 rows (job list, transcripts, logs) |
| Data fetching | `@tanstack/react-query` 5.104, `openapi-fetch` 0.17 with types from `openapi-typescript` 7.13 | Typed client generated from [../api/control-plane-api.openapi.yaml](../api/control-plane-api.openapi.yaml) |
| Routing | `@tanstack/react-router` 1.170 | Typed routes; route `beforeLoad` hides admin routes from navigation (UX only; the API enforces D-25) |
| Syntax highlighting | `shiki` 4.5 with a **custom theme generated from tokens** (`tok-k`, `tok-s`, `tok-c`, `tok-f`) | Diff and code views; runs in a Web Worker |
| Diff parsing | `parse-diff` 0.12 | Unified diff → hunks; word diff via `diff` 9.0 (`diffWordsWithSpace`) |
| Live updates | `@microsoft/fetch-event-source` 2.0.1 | SSE with credentials and `Last-Event-ID` (see [../architecture.md](../architecture.md) §5) |
| Accessibility tests | `axe-core` 4.14 in Playwright component tests | Every component story runs axe in both themes |

**The stock look never ships.** Rules enforced in CI (`stylelint` + a custom ESLint rule):

1. No component library theme packages (`@radix-ui/themes`, `shadcn` generated `components/ui/*`
   with their default Tailwind classes, MUI, Chakra) in `package.json` (dependency allowlist check).
2. Colours, radii, shadows, font sizes and spacing must be `var(--…)` tokens; raw hex, `px` font
   sizes and `box-shadow` literals fail `stylelint` (`declaration-strict-value`).
3. `gradient`, `backdrop-filter`, `text-shadow`, and `filter: drop-shadow` are disallowed values.
4. Only one `box-shadow` token exists (`--shadow-popover`), allowed only in `Popover`, `Menu`,
   `Palette`, `Toast`.
5. `border-radius` above `--r-md` (5 px) is disallowed except avatars.
6. Animation durations must be `--duration-fast` or `--duration-state`; `@keyframes` are allowed
   only in `StatusGlyph` (running) and `Skeleton` (see §9).

## 2. Primitives

| Component | Anatomy | States | Keyboard | A11y |
|---|---|---|---|---|
| `Id` | prefix (text-3) · first 10 body chars (text) · rest (text-3); click copies full ID | default, hover (underline), copied (inline "copied" 1.2 s) | `C` copies the focused row's primary ID | `aria-label` is the full ID; title shows it |
| `StatusGlyph` | 12 px SVG from the shape set (check, cross, diamond, diamond-hollow, ring-spin, ring-dotted, ring-half, bars, slash, hourglass, hand, return, triangle, dot, dot-hollow) | per `tokens.json#status` | — | `aria-hidden`; the adjacent label carries meaning |
| `StatusPill` | glyph + label on `*-tint`, 20 px, `r-sm` | one per status per domain | — | label text always present; colour never alone |
| `Banner` | full-width row under the header: glyph, bold lead sentence, detail, optional right-aligned actions | `warning` (escalated, kill switch, stale, base moved, canary paused), `danger` (bundle rolled back, failed), `neutral` (waiting for capacity, no ETA), `attention` only when the banner itself asks the viewer to decide | — | `role=status`; `role=alert` only for danger |
| `ProvenanceLabel` | dashed hairline mono caps eyebrow: `AGENT-WRITTEN · UNTRUSTED`, `AGENT-WRITTEN CHANGE`, `USER TEXT`, `APPROVER COMMENT`, `EXTERNAL` | — | — | text label, read before the content (T-PI-04) |
| `FactChip` | warning-tint chip stating a deterministic fact about the content (`1 line over 160 characters cache.go:43`, `bidi control U+202E at …`) | — | click scrolls to the line | text |
| `LiveIndicator` | 8 px dot + "Live" in the top bar; hollow warning ring + "Reconnecting · data as of 10:41:12" | live, reconnecting, stale | — | `aria-live=polite` on change |
| `RiskTag` | mono caps `HIGH` | low (text-2), medium (attention-fg), high/critical (danger-fg) | — | text |
| `Button` | 28 px; variants primary (ink fill, `text-on-primary` label; the accent is never a button fill, R2-034), default (raised, hairline), ghost, danger-ghost; optional `Kbd` hint | hover, active, focus-visible (2 px focus ring, 2 px offset), disabled, pending (label replaced by "Recording…" with a 12 px ring-spin) | Hint letter triggers when focus is in the owning region | min target 28×28 (WCAG 2.5.8 AA is 24×24) |
| `Kbd` | 16 px mono key cap with 2 px bottom border | — | — | `aria-hidden` when decorative; shortcuts listed in `?` overlay |
| `Chip` (filter) | 22 px, count in mono | off, on (ink border) | `1`–`9` toggle chips in the filter bar | `aria-pressed` |
| `KV` | 2-column grid; label 12 px text-3 | — | — | `<dl>` |
| `Bar` (budget) | 4 px track, ink fill; warn fill (attention) at ≥ 80% of cap | normal, warn, over (danger, label "over cap") | — | `role=meter` with `aria-valuenow` |
| `Avatar` | initials only (no external image fetch) | — | — | name rendered beside it |
| `Tabs` | text tabs, 2 px ink underline on active, mono count | — | `←`/`→` | Radix Tabs |

## 3. Shell

| Component | Spec |
|---|---|
| `Rail` | 216 px (collapses to 48 px icons below 1200 px). Groups: Work (Jobs, My jobs, Approvals), Platform (Releases, Registries, Cost & quotas), **Replicant admin** (Memory, Admin) rendered only when `GET /v1/me` returns `memory:admin` or `admin:*`, Tenant (Tenant settings) for tenant admins, Operators (Temporal UI ↗) for `replicant-operators`. Approvals count uses the accent; other counts are mono text-3 |
| `TopBar` | 40 px. Breadcrumb with `Id` components, ⌘K search field, theme toggle (`⇧T`), environment tag in the rail (`prod` red outline; `staging` neutral; `local` neutral) |
| `Banner` | Full-width, hairline bottom, used for notices that change what the user should do (bundle revoked, escalated gate, projection lag > 60 s, read-only mode during maintenance) |

## 4. Tables

`DataTable` = TanStack-free plain `<table>` with sticky micro-caps header, 28 px rows (32 px
"comfortable" preference), hairline row rules, hover `bg-hover`, selection `bg-selected`, numbers
right-aligned with tabular figures, IDs in `Id`. Column sets per screen are in the wireframes.
Behaviour: `J`/`K` move, `↵` opens, `X` toggles selection, `⇧X` selects range, `/` focuses the
filter, URL holds filter state (shareable). Empty, loading, error: §9.

## 5. Timeline (job detail)

Vertical list: 64 px right-aligned mono time gutter · 18 px glyph spine (1 px rule) · body · right
meta. Event types are the `TimelineEvent.type` values in C-MSG; summaries come from projected,
workflow-generated text only. **Untrusted text** (steering text, approver comments, PR comments)
is rendered only inside a `Quote` block labelled `user text`/`approver comment`, plain text,
never Markdown-rendered, never linkified except for `https://` URLs on an allowlist of the
tenant's GitHub host (D-21, C-MSG §3.4). Grouping: consecutive `run.status_changed` events of
one run collapse into one row with the final phase and duration ("Provisioning 47 s").
Live: new events insert at top with a 160 ms background fade from `bg-selected` to transparent
(the only list animation; explains "this just arrived").

## 6. Diff viewer

Unified by default, split at ≥ 1440 px on request (`⇧D`). Lines are never truncated: long lines
soft-wrap with a `↪` continuation marker, hunk headers wrap, and lines over 160 characters, bidi
control characters, and invisible Unicode get a `FactChip` above the file and an outline in place
(R2-007, R2-020). File header: path (mono), `+n −m`,
"viewed" checkbox, collapse. Gutter: old/new line numbers (text-3), marker column (`+`/`−`
coloured, so the change is not colour-only). Word-level highlights via `diffWordsWithSpace`.
Hunks collapse beyond 400 lines; files beyond 2,000 changed lines render a "large file" summary
with download. Generated files (`*.pb.go`, lockfiles) are collapsed by default per
`codebase_bindings` hints. Source of truth: the `diff_bundle` artifact via presigned GET, parsed
in a Web Worker. Tab size 2 for display. Keyboard: `N`/`⇧N` next/previous file, `]`/`[` next hunk.

## 7. Log and transcript viewer

Virtualised mono list, 18 px lines, timestamp gutter, tool-call lines tagged (`Bash`, `Edit`,
`Read`, MCP tool name) in `info-fg`, `ok`/`FAIL` tokens coloured with their words kept.
Sources: chunks of `transcript` (S3 presigned, zstd decoded in a Worker with `fzstd`) for
completed runs; live tail via `GET /v1/jobs/{job_id}/runs/{run_id}/live` (SSE proxied from Loki
by control-plane-api, C-STATE §2 exception 2). Search (`/`) highlights matches; `G` jumps to end;
follow mode pins to bottom until the user scrolls. Redaction markers (`[REDACTED:aws_key]`) render
as `tag` chips.

## 8. Approval panel (`GatePacket` + `DecisionBar`)

Sections in fixed order: header (kind, gate ID, title, repo, PR/issue, job, requester, opened,
expiry with hourglass) → banners (escalation, base moved, stale view) → **Why this needs a human**
(fired rules with rule ID, matched evidence, level) → Plan → Diff summary and first hunk → QA
(build, tests, health, Playwright, screenshots) → cost, reviewer notes, bundle → Memory used
(read-only). `DecisionBar` is sticky: comment field (required for reject and request changes),
packet revision and digest stamp, buttons Reject (`X`), Request changes (`R`), Approve (`A`,
primary), and **Re-run QA** (`Q`) only when the packet's `qa.actions` contains `rerun_qa` (QA verdict
`inconclusive`, fewer than 2 re-runs used; C-MSG 2.0.0 V5, R2-027). A QA-inconclusive packet shows a warning banner
"QA could not complete: <banner text>. Re-run QA (n of 2 used), or decide without it" above the decision bar.
States:

| State | UI |
|---|---|
| idle | as prototype |
| submitting | buttons disabled, chosen button shows "Recording…" (outbox, C-MSG §3) |
| accepted | bar replaced by "Approved by you in the UI · 10:42:03"; inbox item leaves the list with a 160 ms collapse |
| superseded | bar replaced by "Already approved by Priya Raman in Slack at 10:42:01"; nothing else changes |
| denied | inline error with the denial reason in words ("You are no longer in payments-approvers (Okta)"), gate stays open |
| stale view (`StaleApprovalView`) | banner "The packet changed (r3 → r4): QA re-ran after a rebase." with a diff of packet sections; buttons re-enable after the user scrolls the changed sections |
| expired/cancelled | bar replaced by the terminal state; buttons gone |
| partially approved (`min_approvers` > 1, e.g. `leak_purge`) | pill "partially approved" (attention, hollow diamond) with "1 of 2 approvals", approvers so far, who is still eligible, and the initiator marked "cannot approve"; the bar stays open for other approvers and shows "You approved at 11:09" to those who already did (duplicate_approver) |
| bundle rolled back | danger banner; Approve disabled with the reason; Request changes and migrate stay available |
| phone (≤ 480 px) | rail and inbox list hidden, packet single column, decision bar stacks with Approve full width |

Bulk approve (`BatchDecision`): only for gates whose kind and risk the tenant's `gate_rules`
mark `batchable: true` (default: `publish_approval` at `low` risk on `docs` workflow). Each gate
in the batch still sends its own `gate.decide` Update with its own `decision_id` and
`presented_digest`; the batch view requires each packet to have been rendered (digests fetched).

## 9. Empty, loading, error states (shared)

| State | Pattern |
|---|---|
| Loading (first paint) | Skeleton rows: hairline rows with 8 px `bg-sunken` bars at real column widths, static (no shimmer) |
| Loading (refresh) | Data stays; a 2 px ink progress line under the top bar after 300 ms |
| Empty | One left-aligned sentence saying what would appear and how to make it appear, plus the relevant command: "No jobs match `status:open repo:ledger-*`. Clear filters (`Esc`) or start a job (`N`)." No illustrations |
| Error (API) | Inline banner in the region that failed with the problem `title`, `code`, and the request ID (`X-Request-Id`), "Retry" button; rest of the page keeps working |
| Stale (projection lag) | Banner "Showing data from 2 min ago. Replicant is catching up." when `X-Replicant-Projection-Lag-Seconds` > 60 |
| Forbidden (403) | Page like [prototype/memory-denied.html](prototype/memory-denied.html): what the page is, why you cannot see it, what you can see instead, the audit ID |
| Not found / no access | Same copy for both ("No job `job_…` that you can see"), so existence is not leaked across tenants |
| Offline / SSE disconnected | Top-bar dot turns hollow with "Live updates paused, reconnecting"; data stays |

## 10. Command palette

Opens with ⌘K / Ctrl-K from anywhere. Sections: **This page** (contextual actions with their key),
**Jobs**, **PRs** (`#1431`), **Bundles** (`rb_`), **Runs** (`run_`), **People**, **Navigation**,
**Commands** (`>` prefix: "Pause at next gate", "Steer…", "Roll back ramp…"). Search is
server-backed (`GET /v1/search?q=`) for IDs and titles, debounced 120 ms, with local
navigation commands. Destructive commands open their normal confirmation.

## 11. Charts

Only charts that answer a stated question are allowed; each declares its question in its title.
Lines 2 px, emphasis pattern (highlighted series in `info-solid`, context series in
`neutral-solid`), direct labels at the last point, a legend, a "Table view" link, nearest-x
crosshair tooltip, ramp/deploy markers as dashed verticals. Palette validated with the dataviz
validator (CVD ΔE 14.8 light, 18.6 dark; the neutral context series is intentionally
achromatic). One axis, never two.

## 12. Keyboard map (global)

`⌘K` palette · `G J` jobs · `G A` approvals · `G R` releases · `G M` memory (admins) · `?` shortcuts
· `J/K` move · `↵` open · `Esc` back/close · `/` filter · `⇧T` theme. Job detail: `S` steer, `P`
pause at next node, `⇧P` pause at next gate, `T` take over, `⇧C` cancel (confirm). Approvals:
`A` approve, `R` request changes, `X` reject, `D` full diff, `N`/`⇧N` next/previous gate.
Single-key shortcuts are disabled while focus is in a text field.
