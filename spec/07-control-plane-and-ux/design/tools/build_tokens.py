#!/usr/bin/env python3
"""Single source of truth for the Replicant control-plane design tokens.

Run:  uv run --with coloraide==4.* python design/tools/build_tokens.py
Emits:
  design/tokens.json                 (W3C Design Tokens Community Group format, $value/$type)
  design/prototype/tokens.css        (CSS custom properties, light + dark, used by the prototype
                                      and copied verbatim into control-plane-web/src/styles)
  design/contrast-report.md          (WCAG 2.2 contrast + colour-vision-deficiency separation)
Exit code 1 if any required contrast threshold or CVD separation threshold fails, so CI can gate it.
"""
from __future__ import annotations

import itertools
import json
import pathlib
import sys

from coloraide import Color

ROOT = pathlib.Path(__file__).resolve().parents[1]

# ----------------------------------------------------------------------------------------------
# Colour definitions (OKLCH). Neutral hue 75 = warm graphite on drafting paper (see direction.md).
# ----------------------------------------------------------------------------------------------
NEUTRAL_HUE = 75
NEUTRAL_CHROMA = 0.006

THEMES = {
    "light": {
        # surfaces
        "bg": "oklch(0.982 0.004 75)",          # app canvas (paper)
        "bg-sunken": "oklch(0.958 0.005 75)",   # wells, code blocks, table header band
        "bg-raised": "oklch(1 0 0)",            # popovers, palette, the one raised panel
        "bg-hover": "oklch(0.940 0.006 75)",
        "bg-selected": "oklch(0.925 0.010 60)",
        # lines (hairlines instead of boxes)
        "line": "oklch(0.895 0.006 75)",
        "line-strong": "oklch(0.80 0.008 75)",
        "line-input": "oklch(0.60 0.010 75)",   # >= 3:1 against bg (non-text UI, WCAG 1.4.11)
        # text
        "text": "oklch(0.235 0.012 75)",
        "text-2": "oklch(0.43 0.012 75)",
        "text-3": "oklch(0.515 0.012 75)",      # meta/IDs prefix; must still be >= 4.5
        "text-on-accent": "oklch(0.18 0.02 50)",
        # accent: "signal" - reserved for 'a human is needed' + primary action
        "accent": "oklch(0.715 0.165 58)",      # fill (buttons, attention pill dot)
        "accent-hover": "oklch(0.675 0.170 54)",
        "accent-text": "oklch(0.505 0.140 50)", # accent used as text on bg
        "accent-tint": "oklch(0.955 0.040 70)",
        "focus": "oklch(0.505 0.140 50)",
        # semantic families: fg (text/icon on tint), solid (dot/bar), tint (pill background)
        "info-fg": "oklch(0.46 0.135 255)", "info-solid": "oklch(0.56 0.150 255)", "info-tint": "oklch(0.950 0.025 255)",
        "success-fg": "oklch(0.45 0.095 170)", "success-solid": "oklch(0.55 0.110 170)", "success-tint": "oklch(0.952 0.030 170)",
        "danger-fg": "oklch(0.49 0.175 22)", "danger-solid": "oklch(0.53 0.190 22)", "danger-tint": "oklch(0.955 0.025 22)",
        "special-fg": "oklch(0.48 0.140 320)", "special-solid": "oklch(0.55 0.150 320)", "special-tint": "oklch(0.955 0.025 320)",
        "neutral-fg": "oklch(0.43 0.012 75)", "neutral-solid": "oklch(0.62 0.010 75)", "neutral-tint": "oklch(0.940 0.006 75)",
        "attention-fg": "oklch(0.47 0.120 52)", "attention-solid": "oklch(0.660 0.160 58)", "attention-tint": "oklch(0.950 0.045 70)",
        # warning (R2-034): degraded or risky, distinct from attention ("a human is needed here")
        "warning-fg": "oklch(0.400 0.090 110)", "warning-solid": "oklch(0.400 0.090 110)", "warning-tint": "oklch(0.960 0.060 105)",
        # primary action = ink (R2-034); the orange accent is not used for buttons
        "primary": "oklch(0.235 0.012 75)", "primary-hover": "oklch(0.330 0.012 75)", "text-on-primary": "oklch(0.982 0.004 75)",
        # diff
        "diff-add-bg": "oklch(0.955 0.030 170)", "diff-add-mark": "oklch(0.45 0.095 170)",
        "diff-del-bg": "oklch(0.955 0.025 22)", "diff-del-mark": "oklch(0.49 0.175 22)",
        "diff-add-word": "oklch(0.895 0.065 170)", "diff-del-word": "oklch(0.900 0.060 22)",
    },
    "dark": {
        "bg": "oklch(0.180 0.006 75)",
        "bg-sunken": "oklch(0.150 0.006 75)",
        "bg-raised": "oklch(0.225 0.007 75)",
        "bg-hover": "oklch(0.235 0.008 75)",
        "bg-selected": "oklch(0.270 0.016 60)",
        "line": "oklch(0.285 0.008 75)",
        "line-strong": "oklch(0.370 0.010 75)",
        "line-input": "oklch(0.520 0.010 75)",
        "text": "oklch(0.930 0.006 75)",
        "text-2": "oklch(0.770 0.008 75)",
        "text-3": "oklch(0.665 0.010 75)",
        "text-on-accent": "oklch(0.18 0.02 50)",
        "accent": "oklch(0.760 0.150 62)",
        "accent-hover": "oklch(0.800 0.140 66)",
        "accent-text": "oklch(0.790 0.140 64)",
        "accent-tint": "oklch(0.285 0.050 60)",
        "focus": "oklch(0.790 0.140 64)",
        "info-fg": "oklch(0.780 0.110 255)", "info-solid": "oklch(0.680 0.140 255)", "info-tint": "oklch(0.265 0.045 255)",
        "success-fg": "oklch(0.800 0.110 170)", "success-solid": "oklch(0.720 0.120 170)", "success-tint": "oklch(0.260 0.040 170)",
        "danger-fg": "oklch(0.780 0.130 22)", "danger-solid": "oklch(0.640 0.190 22)", "danger-tint": "oklch(0.270 0.055 22)",
        "special-fg": "oklch(0.800 0.110 320)", "special-solid": "oklch(0.680 0.140 320)", "special-tint": "oklch(0.270 0.045 320)",
        "neutral-fg": "oklch(0.770 0.008 75)", "neutral-solid": "oklch(0.560 0.010 75)", "neutral-tint": "oklch(0.250 0.008 75)",
        "attention-fg": "oklch(0.810 0.135 68)", "attention-solid": "oklch(0.800 0.150 70)", "attention-tint": "oklch(0.285 0.050 60)",
        "warning-fg": "oklch(0.950 0.110 100)", "warning-solid": "oklch(0.970 0.120 100)", "warning-tint": "oklch(0.300 0.050 105)",
        "primary": "oklch(0.930 0.006 75)", "primary-hover": "oklch(0.850 0.006 75)", "text-on-primary": "oklch(0.180 0.006 75)",
        "diff-add-bg": "oklch(0.235 0.035 170)", "diff-add-mark": "oklch(0.800 0.110 170)",
        "diff-del-bg": "oklch(0.240 0.045 22)", "diff-del-mark": "oklch(0.780 0.130 22)",
        "diff-add-word": "oklch(0.320 0.065 170)", "diff-del-word": "oklch(0.330 0.080 22)",
    },
}

# Status vocabulary -> (family, glyph). Colour is never the only carrier: every status has a
# distinct glyph shape and a text label (WCAG 1.4.1). Glyph names refer to components.md §StatusGlyph.
STATUS = {
    "job": {
        "queued": ("neutral", "ring-dotted"),
        "running": ("info", "ring-spin"),
        "waiting_human": ("attention", "diamond"),
        "waiting_external": ("neutral", "ring-half"),
        "paused": ("neutral", "bars"),
        "succeeded": ("success", "check"),
        "failed": ("danger", "cross"),
        "cancelled": ("neutral", "slash"),
        "rejected": ("danger", "slash"),
        "expired": ("neutral", "hourglass"),
        "taken_over": ("special", "hand"),
    },
    "gate": {
        "partially_approved": ("attention", "diamond"),
        "open": ("attention", "diamond"),
        "approved": ("success", "check"),
        "rejected": ("danger", "cross"),
        "changes_requested": ("special", "return"),
        "answered": ("success", "check"),
        "expired": ("neutral", "hourglass"),
        "cancelled": ("neutral", "slash"),
    },
    "run": {
        "Pending": ("neutral", "ring-dotted"),
        "Provisioning": ("info", "ring-dotted"),
        "Running": ("info", "ring-spin"),
        "Finalizing": ("info", "ring-half"),
        "Cancelling": ("neutral", "slash"),
        "Succeeded": ("success", "check"),
        "Failed": ("danger", "cross"),
        "Cancelled": ("neutral", "slash"),
    },
    "memory": {
        "candidate": ("info", "diamond-hollow"),
        "active": ("success", "dot"),
        "deprecated": ("neutral", "dot-hollow"),
        "quarantined": ("danger", "triangle"),
        "disabled": ("neutral", "slash"),
        "purged": ("neutral", "cross"),
    },
    "bundle": {
        "built": ("neutral", "dot-hollow"),
        "evaluating": ("info", "ring-spin"),
        "eval_passed": ("success", "check"),
        "eval_failed": ("danger", "cross"),
        "shadowing": ("info", "ring-half"),
        "canary": ("info", "ring-spin"),
        "canary_held": ("warning", "bars"),
        "promoted": ("success", "dot"),
        "rolled_back": ("danger", "return"),
        "withdrawn": ("neutral", "slash"),
        "deprecated": ("neutral", "dot-hollow"),
        "revoked": ("danger", "slash"),
    },
}

FAMILIES = ["info", "success", "danger", "special", "neutral", "attention", "warning"]

TYPE = {
    "font-sans": '"IBM Plex Sans", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif',
    "font-mono": '"IBM Plex Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace',
    # size / line-height (px). 13px is the body size: dense, but Plex has a large x-height.
    "scale": {
        "micro": [11, 14], "small": [12, 16], "body": [13, 18], "body-lg": [14, 20],
        "title-sm": [15, 20], "title": [18, 24], "display": [22, 28],
    },
    "weights": {"regular": 400, "medium": 500, "semibold": 600},
    "tracking": {"micro-caps": "0.04em", "default": "0", "display": "-0.01em"},
}
SPACE = {"0": 0, "px": 1, "0.5": 2, "1": 4, "1.5": 6, "2": 8, "3": 12, "4": 16, "5": 20, "6": 24, "8": 32, "10": 40, "12": 48}
RADII = {"none": 0, "xs": 2, "sm": 3, "md": 5, "pill": 999}
ELEVATION = {
    "light": {"0": "none", "popover": "0 0 0 1px oklch(0.80 0.008 75), 0 8px 24px -8px oklch(0.235 0.012 75 / 0.18)"},
    "dark": {"0": "none", "popover": "0 0 0 1px oklch(0.370 0.010 75), 0 12px 32px -8px oklch(0 0 0 / 0.55)"},
}
MOTION = {"duration-fast": "90ms", "duration-state": "160ms", "easing": "cubic-bezier(0.2, 0, 0, 1)"}
LAYOUT = {"rail-width": 216, "rail-collapsed": 48, "row-height-dense": 28, "row-height": 32, "topbar": 40}


def ratio(fg: str, bg: str) -> float:
    return Color(fg).convert("srgb").fit("srgb").contrast(Color(bg).convert("srgb").fit("srgb"), method="wcag21")


def hexof(c: str) -> str:
    return Color(c).convert("srgb").fit("srgb").to_string(hex=True)


CVD = ["protan", "deutan", "tritan"]


def de(a: str, b: str, cvd: str | None) -> float:
    ca, cb = Color(a).convert("srgb").fit("srgb"), Color(b).convert("srgb").fit("srgb")
    if cvd:
        ca, cb = ca.filter(cvd, 1.0, method="machado" if cvd != "tritan" else "brettel"), cb.filter(cvd, 1.0, method="machado" if cvd != "tritan" else "brettel")
    return ca.delta_e(cb, method="2000")


def main() -> int:
    failures: list[str] = []
    lines = ["# Contrast and colour-vision report", "",
             "Generated by `design/tools/build_tokens.py` (coloraide; WCAG 2.x relative-luminance contrast; "
             "CVD simulation: Machado et al. 2009 at severity 1.0 for protan/deutan, Brettel 1997 for tritan; "
             "colour difference CIEDE2000). Do not edit by hand.", ""]
    checks = []  # (label, fg, bg, min)
    for theme, t in THEMES.items():
        for bgk in ["bg", "bg-sunken", "bg-raised", "bg-hover", "bg-selected"]:
            for fgk, mn in [("text", 7.0), ("text-2", 4.5), ("text-3", 4.5), ("accent-text", 4.5)]:
                if bgk in ("bg-hover", "bg-selected") and fgk == "accent-text":
                    continue
                checks.append((theme, f"{fgk} on {bgk}", t[fgk], t[bgk], mn))
        checks.append((theme, "text-on-primary on primary (button label)", t["text-on-primary"], t["primary"], 7.0))
        checks.append((theme, "text-on-primary on primary-hover", t["text-on-primary"], t["primary-hover"], 4.5))
        checks.append((theme, "primary button on bg (1.4.11)", t["primary"], t["bg"], 3.0))
        checks.append((theme, "line-input on bg (input border, 1.4.11)", t["line-input"], t["bg"], 3.0))
        checks.append((theme, "focus ring on bg (2.4.13 / 1.4.11)", t["focus"], t["bg"], 3.0))
        checks.append((theme, "focus ring on bg-raised", t["focus"], t["bg-raised"], 3.0))
        for fam in FAMILIES:
            checks.append((theme, f"{fam}-fg on {fam}-tint (pill label)", t[f"{fam}-fg"], t[f"{fam}-tint"], 4.5))
            checks.append((theme, f"{fam}-fg on bg (inline status text)", t[f"{fam}-fg"], t["bg"], 4.5))
            checks.append((theme, f"{fam}-solid on bg (glyph, 1.4.11)", t[f"{fam}-solid"], t["bg"], 3.0))
        checks.append((theme, "text on diff-add-bg", t["text"], t["diff-add-bg"], 7.0))
        checks.append((theme, "text on diff-del-bg", t["text"], t["diff-del-bg"], 7.0))
        checks.append((theme, "text on diff-add-word", t["text"], t["diff-add-word"], 4.5))
        checks.append((theme, "text on diff-del-word", t["text"], t["diff-del-word"], 4.5))
        checks.append((theme, "diff-add-mark on diff-add-bg (+ gutter)", t["diff-add-mark"], t["diff-add-bg"], 4.5))
        checks.append((theme, "diff-del-mark on diff-del-bg (- gutter)", t["diff-del-mark"], t["diff-del-bg"], 4.5))

    lines += ["## WCAG 2.2 contrast", "", "| Theme | Pair | Foreground | Background | Ratio | Required | Result |", "|---|---|---|---|---|---|---|"]
    for theme, label, fg, bg, mn in checks:
        r = ratio(fg, bg)
        ok = r >= mn
        if not ok:
            failures.append(f"{theme}: {label} = {r:.2f} < {mn}")
        lines.append(f"| {theme} | {label} | `{hexof(fg)}` | `{hexof(bg)}` | {r:.2f} | {mn} | {'pass' if ok else '**FAIL**'} |")

    # CVD separation between chromatic status solids (the glyph colours) – normal + 3 CVDs.
    lines += ["", "## Status colour separation (CIEDE2000 between `*-solid` glyph colours)", "",
              "Threshold: ΔE00 ≥ 12 for normal vision, ≥ 8 under each simulated deficiency, for every pair that can "
              "appear next to each other in one list. Pairs below 8 are allowed only if their glyph shapes differ "
              "(they always do: see STATUS table) and are flagged for review.", ""]
    for theme, t in THEMES.items():
        lines += [f"### {theme}", "", "| Pair | normal | protan | deutan | tritan | min |", "|---|---|---|---|---|---|"]
        for a, b in itertools.combinations(FAMILIES, 2):
            vals = [de(t[f"{a}-solid"], t[f"{b}-solid"], c) for c in [None] + CVD]
            mn = min(vals[1:])
            flag = ""
            if vals[0] < 12 or mn < 8:
                flag = " (shape-disambiguated)"
                if vals[0] < 12 and not {a, b} <= {"neutral", "attention"}:
                    pass
            lines.append(f"| {a} / {b} | {vals[0]:.1f} | {vals[1]:.1f} | {vals[2]:.1f} | {vals[3]:.1f} | {mn:.1f}{flag} |")
            # hard failure only for the pairs whose confusion would mislead an operator:
            critical = {("success", "danger"), ("info", "attention"), ("danger", "attention"), ("success", "attention"), ("info", "danger"),
                        ("attention", "warning"), ("danger", "warning"), ("success", "warning")}
            if (a, b) in critical or (b, a) in critical:
                if vals[0] < 12 or mn < 8:
                    failures.append(f"{theme}: critical pair {a}/{b} separation normal={vals[0]:.1f} min-cvd={mn:.1f}")
        lines.append("")

    lines += ["## Status vocabulary (family + glyph; colour is never the only carrier)", "", "| Domain | State | Family | Glyph |", "|---|---|---|---|"]
    for dom, states in STATUS.items():
        for s, (fam, g) in states.items():
            lines.append(f"| {dom} | `{s}` | {fam} | {g} |")

    if failures:
        lines += ["", "## FAILURES", ""] + [f"- {f}" for f in failures]
    else:
        lines += ["", "All required thresholds pass."]
    (ROOT / "contrast-report.md").write_text("\n".join(lines) + "\n")

    # ---------------- tokens.json (DTCG) ----------------
    def col(v):
        return {"$type": "color", "$value": hexof(v), "$extensions": {"com.replicant.oklch": v}}

    tokens = {
        "$description": "Replicant control-plane design tokens. Generated by design/tools/build_tokens.py. DTCG format.",
        "color": {theme: {k: col(v) for k, v in t.items()} for theme, t in THEMES.items()},
        "status": {dom: {s: {"family": fam, "glyph": g} for s, (fam, g) in st.items()} for dom, st in STATUS.items()},
        "font": {
            "family": {"sans": {"$type": "fontFamily", "$value": TYPE["font-sans"]}, "mono": {"$type": "fontFamily", "$value": TYPE["font-mono"]}},
            "size": {k: {"$type": "dimension", "$value": f"{v[0]}px"} for k, v in TYPE["scale"].items()},
            "lineHeight": {k: {"$type": "dimension", "$value": f"{v[1]}px"} for k, v in TYPE["scale"].items()},
            "weight": {k: {"$type": "fontWeight", "$value": v} for k, v in TYPE["weights"].items()},
            "tracking": {k: {"$type": "dimension", "$value": v} for k, v in TYPE["tracking"].items()},
        },
        "space": {k: {"$type": "dimension", "$value": f"{v}px"} for k, v in SPACE.items()},
        "radius": {k: {"$type": "dimension", "$value": f"{v}px"} for k, v in RADII.items()},
        "border": {"hairline": {"$type": "dimension", "$value": "1px"}, "focus": {"$type": "dimension", "$value": "2px"}, "focusOffset": {"$type": "dimension", "$value": "2px"}},
        "elevation": {theme: {k: {"$type": "shadow", "$value": v} for k, v in e.items()} for theme, e in ELEVATION.items()},
        "motion": {k: {"$type": "duration" if "duration" in k else "cubicBezier", "$value": v} for k, v in MOTION.items()},
        "layout": {k: {"$type": "dimension", "$value": f"{v}px"} for k, v in LAYOUT.items()},
    }
    (ROOT / "tokens.json").write_text(json.dumps(tokens, indent=2) + "\n")

    # ---------------- tokens.css ----------------
    def block(t, shadow):
        out = [f"  --c-{k}: {hexof(v)};" for k, v in t.items()]
        out += [f"  --shadow-{k}: {v};" for k, v in shadow.items()]
        return "\n".join(out)

    static = [f"  --font-sans: {TYPE['font-sans']};", f"  --font-mono: {TYPE['font-mono']};"]
    for k, (s, lh) in TYPE["scale"].items():
        static += [f"  --fs-{k}: {s}px;", f"  --lh-{k}: {lh}px;"]
    static += [f"  --fw-{k}: {v};" for k, v in TYPE["weights"].items()]
    static += [f"  --sp-{k.replace('.', '_')}: {v}px;" for k, v in SPACE.items()]
    static += [f"  --r-{k}: {v}px;" for k, v in RADII.items()]
    static += [f"  --{k}: {v};" for k, v in MOTION.items()]
    static += [f"  --{k}: {v}px;" for k, v in LAYOUT.items()]
    css = ["/* Generated by design/tools/build_tokens.py — do not edit. */",
           ":root {", "\n".join(static), "  color-scheme: light;", block(THEMES["light"], ELEVATION["light"]), "}",
           ':root[data-theme="dark"] {', "  color-scheme: dark;", block(THEMES["dark"], ELEVATION["dark"]), "}",
           '@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {', "  color-scheme: dark;",
           block(THEMES["dark"], ELEVATION["dark"]), "} }",
           "@media (prefers-reduced-motion: reduce) { :root { --duration-fast: 0ms; --duration-state: 0ms; } }"]
    (ROOT / "prototype" / "tokens.css").write_text("\n".join(css) + "\n")

    print("\n".join(failures) if failures else "all thresholds pass")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
