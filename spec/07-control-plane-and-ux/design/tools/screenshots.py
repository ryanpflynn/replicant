#!/usr/bin/env python3
"""Render the prototype with headless Chromium (Playwright) at 1440 and 1024 px (390 px for the
phone approve flow), light and dark, and fail on clipped horizontal overflow (R2-029).

Run (from spec/07-control-plane-and-ux):
  uv run --with playwright python -m playwright install chromium
  uv run --with playwright python design/tools/screenshots.py [page ...]

Fails if a web font did not load (document.fonts check), so screenshots never silently fall
back to system fonts.
"""
from __future__ import annotations

import pathlib
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROTO = ROOT / "prototype"
OUT = ROOT / "screenshots"

# (file, slug, extra query, full_page, widths)
DESK = {1440: 900, 1024: 768}
PHONE = {390: 844}
PAGES = [
    ("job-detail.html", "job-detail", "", False, DESK),
    ("job-detail.html", "job-detail-palette", "&palette=1", False, DESK),
    ("job-detail.html", "job-detail-failed", "&state=failed", False, DESK),
    ("job-detail.html", "job-detail-capacity", "&state=capacity", False, DESK),
    ("job-detail.html", "job-detail-stale", "&state=stale", False, DESK),
    ("job-list.html", "job-list", "", False, DESK),
    ("approvals.html", "approvals", "", False, DESK),
    ("approvals.html", "approvals-rolledback", "&state=rolledback", False, DESK),
    ("approvals.html", "approvals-stale", "&state=stale", False, DESK),
    ("approvals.html", "approvals-empty", "&state=empty", False, DESK),
    ("approvals.html", "approvals-degraded", "&state=degraded", False, DESK),
    ("approvals.html", "approvals-phone", "", False, PHONE),
    ("channels.html", "channels", "", True, DESK),
    ("releases.html", "releases", "&admin=1", False, DESK),
    ("releases.html", "releases-rollback-dialog", "&admin=1&dialog=rollback", False, DESK),
    ("memory.html", "memory-admin", "&admin=1", False, DESK),
    ("memory-denied.html", "memory-denied-nonadmin", "&admin=0", False, DESK),
    ("tokens.html", "tokens", "", True, DESK),
]
THEMES = ["light", "dark"]

# R2-029: horizontal overflow check. An element whose content is wider than its box and which is
# not an intentional scroller (overflow-x auto/scroll) or an intentional ellipsis is reported.
OVERFLOW_JS = """() => {
  const out = [];
  const de = document.documentElement;
  if (de.scrollWidth > de.clientWidth + 1) out.push('page scrolls horizontally: ' + de.scrollWidth + ' > ' + de.clientWidth);
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    if (el.closest('svg') && el.tagName.toLowerCase() !== 'svg') continue;
    if (cs.textOverflow === 'ellipsis') continue;
    // Scrollers count too: content hidden behind a horizontal scroll at the target widths is the same defect
    // unless the element opts in with data-scroll-ok (it then renders an edge fade, see app.css .scroll-x).
    if (['auto', 'scroll'].includes(cs.overflowX) && el.hasAttribute('data-scroll-ok')) continue;
    if (el.closest('[data-scroll-ok]') && el !== el.closest('[data-scroll-ok]')) continue;
    if (el.scrollWidth > el.clientWidth + 2 && el.clientWidth > 0 && cs.overflowX !== 'visible') {
      out.push(el.tagName.toLowerCase() + (el.className && typeof el.className === 'string' ? '.' + el.className.split(' ').join('.') : '') + ' ' + el.scrollWidth + '>' + el.clientWidth + ' "' + (el.textContent || '').trim().slice(0, 50) + '"');
    }
  }
  // Elements cut by a clipping ancestor (the R2-029 table and button cases): tables, cells, buttons, pills.
  for (const el of document.querySelectorAll('table, th, td, button, .btn, .pill')) {
    const r = el.getBoundingClientRect();
    if (!r.width) continue;
    let a = el.parentElement;
    while (a && a !== document.body) {
      const cs = getComputedStyle(a);
      if (['auto', 'scroll'].includes(cs.overflowX)) break;
      if (['hidden', 'clip'].includes(cs.overflowX)) {
        const ar = a.getBoundingClientRect();
        if (r.right > ar.right + 2 || r.left < ar.left - 2) out.push('clipped by ' + a.tagName.toLowerCase() + '.' + String(a.className).split(' ').join('.') + ': ' + el.tagName.toLowerCase() + ' "' + (el.textContent || '').trim().slice(0, 40) + '"');
        break;
      }
      a = a.parentElement;
    }
  }
  return out;
}"""


def main(only: list[str]) -> int:
    OUT.mkdir(exist_ok=True)
    bad = []
    overflow: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for f, slug, extra, full, widths in PAGES:
            if only and slug not in only:
                continue
            for w, h in widths.items():
                for theme in THEMES:
                    ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1,
                                              color_scheme=theme, reduced_motion="reduce")
                    page = ctx.new_page()
                    page.goto(f"{(PROTO / f).as_uri()}?theme={theme}{extra}")
                    page.evaluate("document.fonts.ready")
                    loaded = page.evaluate("""() => ['400 13px "IBM Plex Sans"','600 13px "IBM Plex Sans"','400 12px "IBM Plex Mono"']
                                              .map(f => document.fonts.check(f))""")
                    used = page.evaluate("""() => [...document.fonts].filter(f => f.status === 'loaded').map(f => f.family + ' ' + f.weight)""")
                    if not all(loaded) or not any("Plex Sans" in u for u in used):
                        bad.append(f"{slug} {w} {theme}: fonts not loaded {loaded} {used}")
                    for o in page.evaluate(OVERFLOW_JS):
                        overflow.append(f"{slug} {w} {theme}: {o}")
                    path = OUT / f"{slug}-{w}-{theme}.png"
                    page.screenshot(path=str(path), full_page=full)
                    ctx.close()
                    print("wrote", path.relative_to(ROOT))
        browser.close()
    for b in bad:
        print("FONT FAILURE:", b)
    for o in overflow:
        print("OVERFLOW:", o)
    return 1 if bad or overflow else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
