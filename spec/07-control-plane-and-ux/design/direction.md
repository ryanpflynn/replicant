# Design direction: "drafting table"

**Owner:** WS-07 · **Constraints:** shared context §4 (visual design bar), D-19, D-25 · **Artifacts:**
[tokens.json](tokens.json), [contrast-report.md](contrast-report.md), [components.md](components.md),
[critique.md](critique.md), [prototype/](prototype/index.html), [screenshots/](screenshots/)

## The idea in one paragraph

Replicant is an industrial system that people supervise. Its control plane should feel like a
drafting table in a well-run plant: warm paper and graphite, hairline rules instead of boxes,
dense tabular information set in one carefully chosen type family, monospace wherever a machine
identifier appears, and exactly **one** signal colour. That colour, a safety-marking orange,
means one thing everywhere: **a human is needed here** (an open gate, the approvals count, the
primary decision button, the keyboard focus ring). Everything else is ink. An engineer who glances
at any screen can answer "is anything waiting on me?" from the colour alone. That rule is what
makes the tool recognisable.

## References and what is taken from each

| Reference | What we take | What we deliberately do not take |
|---|---|---|
| Engineering drawings and ISO 3864 safety marking | Warm paper ground, graphite ink, hairline rules; orange reserved for "attention required" | Hazard stripes, decorative industrial texture |
| Linear (issue tracker) | Keyboard-first operation: single-letter actions shown on the button (`A`, `R`, `X`, `S`, `P`, `T`), ⌘K palette as the universal entry, `J`/`K` list navigation | Purple brand gradient, the glassy dark default |
| Temporal Web UI / Grafana Explore | A timeline that merges heterogeneous events on one time axis, with monospace timestamps right-aligned in a fixed gutter | Card stacks for each event |
| GitHub's diff view and `git log --stat` | Gutter-numbered unified diff, `+n −m` stats in the file header, word-level highlights | Blue-tinted chrome, large rounded file cards |
| Bloomberg Terminal / Unix `top` | Density: 28 px rows, 13 px body, tabular figures, numbers right-aligned, no wasted vertical space | Low-contrast phosphor colours, all-caps everywhere |
| IBM Carbon (type only) | IBM Plex Sans + Plex Mono: a sans and mono designed as one family, so IDs inside sentences sit on the same metrics | Carbon's components, its blue, and its spacing |
| Field Notes / Muji stationery | Restraint. Hierarchy from weight and spacing, not colour or boxes | — |

## What makes it recognisable

1. **The single signal colour.** Orange (`--c-accent`, `#ed8418` light / `#f0a145`-family dark)
   appears only on: open gates (◆ diamond glyph), the Approvals count in the rail, the primary
   decision button, the left edge of the selected inbox item, and the focus ring. Never on
   charts, links, logos of other products, or decoration.
2. **IDs as typography.** Every Replicant ID is rendered by one component: prefix dimmed
   (`job_`), the first 10 body characters in ink, the remainder dimmed. Tabular figures and
   Plex Mono's slashed zero keep `0`/`O` and `1`/`l` apart. Users learn to read IDs at a glance.
3. **The phase track.** A thin segmented line under every job title shows every phase of the
   job's graph, gates marked with ◆. It is the only "progress" graphic in the product.
4. **Hairlines, not boxes.** Sections are separated by 1 px rules. Only three things have a
   visible container: the diff, the decision bar, and popovers (palette, menus). There are no cards.
5. **Shapes for state.** Every status has a glyph whose *shape* carries the meaning (check,
   cross, diamond, ring, half-ring, bars, slash, hourglass, triangle, hand); colour reinforces
   it. The vocabulary is in [tokens.json](tokens.json) `status` and rendered in
   [prototype/tokens.html](prototype/tokens.html).
6. **Warm neutrals.** Neutral hue 75° at chroma 0.004–0.012 in OKLCH: paper rather than the cool
   slate grey of most developer tools. Dark theme is warm charcoal, not blue-black.

## Typography

| Role | Face | Why |
|---|---|---|
| UI text | **IBM Plex Sans** 400 / 500 / 600 (OFL 1.1, vendored `woff2`, latin subset, ~70 KB total) | Large x-height that stays legible at 13 px in dense tables; tabular figures (`tnum`); distinct `I`/`l`/`1`; a grotesque with some character (the angled terminals) without being quirky. It is not Inter, and it is not the default of any component library we use |
| IDs, hashes, diffs, logs, timestamps, CLI | **IBM Plex Mono** 400 / 500 / 600 | Designed alongside Plex Sans: shared vertical metrics, so an inline `rb_ds5p3wmcj` does not jump the baseline. Distinguishes `0`/`O` and `1`/`l`/`I`, which matters because our IDs are Crockford base32 |
| Fallbacks | `ui-sans-serif, system-ui` / `ui-monospace, SF Mono, Menlo` | `font-display: block` with a 3 s budget; the screenshot pipeline fails if Plex did not load |

Scale (px, size/line-height): micro 11/14, small 12/16, **body 13/18**, body-lg 14/20, title-sm
15/20, title 18/24, display 22/28. Two weights do almost all the work: 400 and 600; 500 for
selected navigation and emphasised table cells. Micro-caps (11 px, 500, +0.06 em) label sections
and columns; they are the only uppercase text.

## Colour

**One meaning per hue (wave-1 fix, R2-034).** Orange (`accent`/`attention`) means only "a human is
needed here" and the focus ring. Primary buttons are ink (`primary`, near-black in light and near-white in
dark) so that a page full of buttons does not read as a page full of requests. Conditions that need
awareness but not a decision (escalated, kill switch engaged, stale data, canary paused) use the separate
`warning` family, olive on pale yellow at hue 100–110, chosen by search so it stays ≥ 17 CIEDE2000 from
attention and ≥ 11 from danger under every simulated deficiency. Waiting for capacity is neutral.

Defined in OKLCH in [tools/build_tokens.py](tools/build_tokens.py) and emitted as hex.
WCAG 2.2 results (full table in [contrast-report.md](contrast-report.md)):

| Pair | Light | Dark | Requirement |
|---|---|---|---|
| `text` on `bg` | 15.84 | 15.30 | 4.5 (we hold 7) |
| `text-2` on `bg` | 7.71 | 9.07 | 4.5 |
| `text-3` (meta, ID tails) on `bg` / `bg-selected` (worst) | 5.35 / 4.51 | 6.16 / 4.95 | 4.5 |
| `accent-text` on `bg` | 5.88 | 9.45 | 4.5 |
| Primary button label (`text-on-primary`) on `primary` (ink) | 15.84 | 15.30 | 7 |
| Warning label (`warning-fg`) on `warning-tint` | 8.15 | 11.81 | 4.5 |
| Warning glyph (`warning-solid`) on `bg` | 8.63 | 17.34 | 3.0 (1.4.11) |
| Status pill label (`*-fg` on `*-tint`), worst family | 5.95 (danger) | 7.26 (danger) | 4.5 |
| Status glyph (`*-solid` on `bg`), worst family | 3.10 (attention) | 4.04 (neutral) | 3.0 (1.4.11) |
| Input border (`line-input` on `bg`) | 3.75 | 3.41 | 3.0 (1.4.11) |
| Focus ring on `bg` | 5.88 | 9.45 | 3.0 (2.4.13 / 1.4.11) |
| Text on diff word highlight | 12.50 | 9.99 | 4.5 |

Colour-vision separation of status glyph colours (CIEDE2000, Machado 2009 simulation at full
severity for protan/deutan, Brettel for tritan). Every pair that would mislead an operator if
confused stays ≥ 16 under every simulated deficiency:

| Critical pair | Normal | Protan | Deutan | Tritan |
|---|---|---|---|---|
| success / danger | 63.5 | 16.7 | 16.9 | 56.2 |
| danger / attention | 30.3 | 25.1 | 16.7 | 17.1 |
| info / attention | 51.5 | 56.2 | 60.5 | 58.4 |
| success / attention | 48.2 | 19.5 | 27.6 | 51.9 |
| info / danger | 44.3 | 42.2 | 50.5 | 63.0 |
| attention / warning (light; dark min 17.7) | 37.5 | 22.2 | 31.6 | 33.8 |
| danger / warning (light; dark min 26.4) | 45.0 | 11.7 | 14.2 | 25.9 |

Three pairs fall below 8 under one deficiency (info/success under tritanopia 3.2; info/special
under deuteranopia 6.7; success/neutral under protanopia 7.7). They are never disambiguated by
colour: running is a spinning ring, succeeded a filled check; taken-over is a filled hand-arrow;
neutral states are hollow or slashed. To get success/danger separated under deuteranopia we moved
success to a bluish green (hue 170, Okabe-Ito lineage) and danger to a vermilion-crimson (hue 22),
then darkened danger relative to attention. The first draft (green 155 / red 10) scored 1.4 under
deuteranopia and failed the build script.

## Banned patterns: how each is avoided, per screen

| Banned pattern | Job list | Job detail | Approvals | Releases | Memory | Notes |
|---|---|---|---|---|---|---|
| Purple/blue/pink gradients, gradient text, glows, aurora/mesh | none | none | none | none | none | No gradient token exists. `build_tokens.py` emits no gradient values, and a stylelint rule (`declaration-property-value-disallowed-list` for `/gradient/`) fails CI in `control-plane-web` |
| Glassmorphism / frosted surfaces | none | none | none | none | none | No `backdrop-filter` anywhere. The palette scrim is a flat 45% mix of `bg`, used only behind the modal palette |
| Sparkle / magic-wand icons, "AI-powered" badges, emoji in headings/nav | none | Agent work is described by role and CLI ("implementer · claude-code 2.1.240"), never "AI" | none | none | none | The icon set is 9 stroked 14 px glyphs (see `app.js`), reviewed in the component inventory |
| Grids of identical rounded cards with icon + title + line | Table | Timeline list | List + packet | Table + chart | Table + detail | QA screenshot thumbnails are the only grid: real artifacts, not feature cards |
| Vanity stat tiles | none | Cost has a cap and a bar because it answers "how close to the budget?" | none | The chart answers "which release raised the failure rate?" and carries ramp markers | "If approved" states a concrete consequence | Dashboards in the product must name the question each figure answers (components.md §Charts) |
| Everything centred, hero sections, marketing layouts | Left-aligned table | Left-aligned | Two-pane | Two-pane | Two-pane | The 403 page is left-aligned with a 620 px measure, not a centred hero |
| Stock component-library look, default Inter, default shadows and radii | Plex, 3 px radii, one shadow token used only by popovers | same | same | same | same | Radix primitives are unstyled by design; we ship no shadcn theme. components.md lists every primitive's restyle |
| Decorative motion | none | The running glyph rotates (1.2 s linear) only while a run is live: motion explains state. `prefers-reduced-motion` stops it | Decision bar slides 160 ms when the outcome arrives | none | none | Motion tokens: 90 ms (hover), 160 ms (state change) |
| Lorem ipsum / vague data | Realistic jobs, repos, costs | Real-looking Temporal events, run IDs from C-IDS test vectors | Real-looking diff in Go, rule IDs, packet digest | Bundle IDs, build IDs, eval suite | Realistic candidate memories, including a poisoned one | Data is consistent across screens (the same job, bundle, approver) |

### Justified exceptions

- **The orange escalation banner** on the approval packet is a large tinted area. It is
  justified because the escalation and base-drift facts change what the approver should do;
  that is precisely the meaning of the signal colour.
- **The `prod` environment tag** is the only red element in the shell. Operating on production
  should never be ambiguous.

## Light and dark were designed together

Both themes come from one table. Dark is not an inversion: surfaces step *up* in lightness for
elevation (`bg` 0.18 → `bg-raised` 0.225 L), tints are low-chroma (0.04–0.055) so pills do not
glow, the accent is lighter and yellower (L 0.76, hue 62) to keep 8.5:1 with dark button text, and
status solids are re-tuned for 3:1 on the dark ground. Both themes were checked in every
screenshot.
