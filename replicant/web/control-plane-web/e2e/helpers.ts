import { expect, type Page, type APIRequestContext } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";

export const SHOTS = process.env.SHOTS_DIR ?? path.resolve("screenshots");
export const env = (k: string) => {
  const v = process.env[k];
  if (!v) throw new Error(`set ${k}`);
  return v;
};

/** Dev identity and theme are per-browser preferences (localStorage), set before the app boots. */
export async function as(page: Page, user: string, theme: "light" | "dark" = "light") {
  await page.addInitScript(([u, t]) => {
    localStorage.setItem("replicant.devUser", u);
    localStorage.setItem("replicant.theme", t);
  }, [user, theme] as const);
}

export async function settle(page: Page) {
  await page.waitForLoadState("networkidle").catch(() => undefined);
  await page.evaluate(() => document.fonts.ready);
  const ok = await page.evaluate(() => document.fonts.check('13px "IBM Plex Sans"') && document.fonts.check('12px "IBM Plex Mono"'));
  expect(ok, "IBM Plex fonts loaded").toBe(true);
  await page.waitForTimeout(300);
}

/** Elements whose content is wider than their box without being an intended scroller or ellipsis (critique.md R2-029). */
export async function overflowFindings(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const out: string[] = [];
    for (const el of Array.from(document.querySelectorAll<HTMLElement>("body *"))) {
      const cs = getComputedStyle(el);
      if (cs.display === "none" || el.closest("svg")) continue;
      if (el.scrollWidth <= el.clientWidth + 1) continue;
      if (["auto", "scroll"].includes(cs.overflowX) || cs.textOverflow === "ellipsis") continue;
      if (cs.overflowX === "hidden" && el.closest(".track, .filters, .gitem, .sub, td, .tail, .search, .diff")) continue;
      if (cs.overflowX === "visible" && el.clientWidth === 0) continue;
      const r = el.getBoundingClientRect();
      if (r.width === 0) continue;
      out.push(`${el.tagName.toLowerCase()}.${String(el.className).split(" ").join(".")} ${el.scrollWidth}>${el.clientWidth}`);
    }
    return out.slice(0, 20);
  });
}

export async function shot(page: Page, name: string) {
  fs.mkdirSync(SHOTS, { recursive: true });
  await settle(page);
  const f = await overflowFindings(page);
  if (f.length) console.log(`overflow ${name}: ${f.join(" | ")}`);
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: false });
}

export async function apiGet<T>(request: APIRequestContext, p: string, user = "grace"): Promise<T> {
  const r = await request.get(p, { headers: { "X-Replicant-Dev-User": user } });
  expect(r.ok(), `${p} -> ${r.status()}`).toBe(true);
  return (await r.json()) as T;
}
