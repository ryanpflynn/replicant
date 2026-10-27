# Self-critique log

Screenshots are in [screenshots/](screenshots/) (`<screen>-<1440|1024|390>-<light|dark>.png`, 70 files:
17 screens or states at 1440 and 1024 px in both themes, plus the phone approve flow at 390 px in both
themes; R1-189 corrected the count), rendered by [tools/screenshots.py](tools/screenshots.py) with
headless Chromium (Playwright, `chromium-headless-shell` 156). The script fails if IBM Plex Sans/Mono did
not load (`document.fonts.check`) and, since the wave-1 fix round, if any element clips horizontal
overflow without being an intentional scroller or ellipsis (R2-029). The last full render reported fonts
loaded and zero overflow findings.

Review method for each pass: open every screenshot at both widths and both themes; check against
(1) the banned list in shared context §4, (2) [direction.md](direction.md) (single signal colour,
hairlines not boxes, IDs as typography, shapes for state), (3) layout defects (overflow, wraps,
truncation that loses meaning), (4) whether each screen answers its job's main question in one
glance.

## Pass 0: token build (before any screen)

- First palette had green (hue 155) and red (hue 10) status colours. The CVD check in
  `build_tokens.py` failed: success/danger ΔE00 **1.4** under deuteranopia (light), 2.1 (dark).
  Fix: success → bluish green (hue 170), danger → vermilion-crimson (hue 22), special → hue 320.
  Result 16.7 / 16.9 (protan/deutan). Also failed: attention glyph contrast 2.51:1 on paper;
  darkened `attention-solid` to L 0.66 (3.10:1) while the button accent keeps L 0.715 for its
  dark label (7.12:1).

## Pass 1: job detail, first render

Observed (job-detail-1440-light, job-detail-1024-dark):
- **Defect:** the dimmed tail of the breadcrumb ID was drawn inside a bordered box. Cause: the
  ID component's `.tail` class collided with the transcript-tail block style. Renamed to `.rest`.
- At 1024 px the search placeholder wrapped onto two lines and side-panel headings ("Memory
  used") wrapped. Added `nowrap` + ellipsis to the search label and to section headings.
- Banned-pattern check: no gradients, no cards, no glows. The phase track is the only
  progress graphic, and the orange appears only on the Approvals count and the open-gate glyph.
  Pass.
- Kept: the live transcript tail directly under the live node strip answers "what is it doing
  right now?" without a click. That was the screen's main question.

## Pass 1: approvals, first render

- Filter chips wrapped mid-label ("My / teams"); the bulk bar wrapped into three lines; the
  side panel key column (112 px) squeezed QA values into two lines; the diff rendered tabs at
  width 8. Fixed: `white-space: nowrap` on chips, pills, and buttons; `.side .kv` 92 px; diff
  `tab-size: 2`; bulk-bar copy shortened to one sentence.
- A bug in mock data: a deletion line contained an `<ins>` highlight. Corrected to `<del>`.
- Considered and rejected: making the escalation banner neutral. It stays orange because
  escalation and base drift change what the approver should do (justified in direction.md).

## Pass 2: releases, memory, list, token sheet

- **Defect (releases, both widths):** the bundle table overflowed under the side panel (grid
  child without `min-width: 0`), and the chart header crammed title, subtitle, and legend into
  one wrapping row. Fixed: removed the worker-build column from the bundle table (the worker
  deployment table below shows builds), `min-width: 0; overflow-x: auto` on main columns,
  legend moved to its own line.
- **Defect (memory, job list):** `.filters` was scoped to the inbox only, so chips touched each
  other on other screens. Made `.filters` a shared pattern.
- Memory actions wrapped with a lone "Reject" on a second line. Moved scope promotion,
  quarantine, and merge into a "More ▾" menu, which leaves the three common actions on one line.
- Chart check against the dataviz anti-patterns: one axis, two series, emphasis pattern, direct
  labels, legend, table-view link, ramp markers; question in the title. The validator passes CVD
  and contrast; its chroma-floor failure on the grey context series is intended for the emphasis
  pattern. In dark mode we use a darker blue so it stays inside the lightness band.
- Worker deployment table inside a padded section doubled the left padding. Fixed: tables
  inside sections drop their outer cell padding.

## Pass 3: 1024 px and polish

- Job list at 1440: the "Updated" column was clipped. Title column max width 340 → 300 px.
- Inbox items at 1024 wrapped requester names to two lines. Requester name hidden below 1200
  px (shown in the packet header), sub-row forced to one line.
- Decision bar stamp wrapped to four lines at 1024. Shortened at narrow widths to "Packet r3 ·
  sha256:… · first decision wins"; the cross-channel note shows at ≥ 1200 px.
- The palette mock listed a job titled "Steering: …", which read as an action. Replaced with
  the job's real title.

## Pass 4: releases after WS-06's release pipeline landed

- The first releases design showed a free-form ramp control (0/5/10/25/50/100) and an 18% failure spike.
  WS-06's pipeline drives the ramp through fixed stages with guardrails, so a manual ramp control was the
  wrong primary affordance and a +14 pt spike would already have triggered an automatic rollback (R3
  rollback tier). Replaced with: a stage track (ring-early → 10% → 50% sign-off → full), a guardrail banner
  stating the trigger, delta, posterior, n, and the auto-rollback deadline, and realistic numbers that sit
  in the pause tier (9.7% vs 4.3%, P(worse) 0.993).
- Added the rollback dialog as a rendered state (`releases-rollback-dialog-*`): the consequences for
  running jobs are the decision, so they are radio options with plain-language effects, and the reason
  field is required. Danger styling is reserved for revoke; rollback itself uses the primary button
  because it is the action the page exists to support.
- Worker table now shows the candidate build as "ramping 0%" (the start target WS-06 uses for canary
  pins), so the Temporal routing line no longer contradicts the release design.
- Checked both themes at both widths: the dialog's scrim keeps the page legible behind it without blur
  (no glassmorphism); the radio options use ink, not accent.

## Pass 5: wave-1 review fixes (R2-020…R2-050)

Review input was the R2 red-team UX findings rather than my own pass. What changed and what the
re-render showed:

| Finding | Change | Verified in |
|---|---|---|
| R2-034 one accent, five meanings | Primary buttons are ink (`primary` tokens); orange is only "a human is needed" and focus. New `warning` family (olive/pale yellow, hue 100–110; the suggested hue 85 failed the attention/warning CVD gate) for escalations, kill switches, stale data and paused canaries. Memory `candidate` moved to info | all screens; `tokens-*` |
| R2-029 overflow and clipping | Column priority (`p2` hidden ≤ 1100, `p3` ≤ 1200, memory `p3w` ≤ 1500) with a second line under the title; filter bars wrap; hunk headers wrap; memory source column wraps; releases Version drops first so Failed 24h stays. `screenshots.py` now fails on any element whose content is wider than its box, scrollers included unless marked `data-scroll-ok` | overflow check: 0 findings over 70 renders |
| R2-035 status semantics in the list | Open = non-terminal; terminal jobs under "Recent"; sort caret on the sorted column; footer "148 open jobs · loads as you scroll" | `job-list-*` |
| R2-036 missing states | Job detail failed, waiting for capacity (no ETA), stale data; approvals rolled-back bundle, changed packet (r3 → r4), empty inbox, degraded backend (Temporal not answering); Slack/GitHub renderings including the stale click, unlinked user and leak-purge "1 of 2"; phone approve flow at 390 px | `job-detail-failed/capacity/stale-*`, `approvals-rolledback/stale/empty/degraded-*`, `channels-*`, `approvals-phone-390-*` |
| R2-020 stuck job | Capacity state names the quota and the wait time, shows no ETA and no queue position (the Runner has no queue, XD-W2-02-04); actions "View running jobs" and "Ask payments-leads" | `job-detail-capacity-*` |
| R2-021 (WS-06 lead; WS-07 dialog and banner) | Packet banner keyed on the bundle state; the rollback dialog during a failed canary shows the blast radius first and preselects "Migrate them now" (`required_by_operator`, from WS-06's `suggested_migrate_running`) | `approvals-rolledback-*`, `releases-rollback-dialog-*` |
| R2-047 memory bulk approve | Bulk approve says "Approve (2 now · 1 as 1 of 2)"; the tenant-scope row shows "1 of 2" | `memory-admin-*` |
| R2-050 copy | Repair budget renders "0 of 5 cycles · repair_limits" from policy | `job-detail-*` |
| Banned-list near miss (R2 §banned) | QA thumbnails are drawn screens (refund detail, FX rates, ledger entries) instead of grey wireframes | `approvals-*` |

Defects found on the re-render and fixed before the final run: hunk header wrapped into the
line-number column (made the hunk a block); the rollback legend ran past the dialog border at 1024 px
(legend wraps); the phone decision bar had an implicit second grid column from `grid-column: 1 / span 2`
(set `1 / -1`); the job-detail side panel still said "Running" in the failed and capacity states; the
capacity timeline row was out of time order; memory candidate columns ran under the side panel at 1024 px
(Kind, Source, Evidence now drop by priority); the rollback dialog and the command palette rendered under their own scrim, which made the dialog look disabled (z-index fixed).

## Remaining known compromises (accepted, with reasons)

| Item | Why it stays |
|---|---|
| Memory candidate actions wrap to two lines at 1024 px | The side panel is 380 px there; four actions plus key hints do not fit, and dropping the hints would hurt keyboard discoverability. The primary action is still first |
| Tritan-simulated info/success glyph colours are close (ΔE 3.2) | Shapes differ (spinning ring vs filled check) and the label is always present; changing the blue would break info/attention separation |
| QA screenshot thumbnails in the prototype are drawn SVG screens | They stand in for real Playwright PNGs from the `screenshot` artifact type; in the product they are presigned images |
| Memory list shows only Candidate and Scope at 1024 px | Kind, source and evidence are in the side panel for the selected row; the list's job at that width is selection |

## Banned-list verdict (final screenshots)

| Pattern | Present? |
|---|---|
| Gradients, gradient text, glows, aurora/mesh | No |
| Glassmorphism / frosted cards | No |
| Sparkle/magic icons, "AI-powered" badges, emoji in headings/nav | No |
| Grids of identical icon-title-line cards | No (QA thumbnails are artifacts; the channel page shows four different message renderings, not cards) |
| Vanity stat tiles | No |
| Centred hero / marketing layout | No |
| Unmodified component-library styling, default Inter, default shadows/radii | No |
| Decorative motion | No (only the running-run glyph rotates; disabled under reduced motion) |
| Lorem ipsum / vague data | No |
