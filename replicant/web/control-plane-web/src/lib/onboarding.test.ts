import { afterEach, describe, expect, it, vi } from "vitest";
import {
  classBadge, decideFleetItem, decisionWord, deltaPhrase, fleetItemsPath, getReadiness, hookOutcome, impactPhrases, listFleetItems,
  mcpSkipWord, reachPhrase, readinessBadge, recheckRepo, repoConfigHeadline, requestSupport, rescanFleet, runRepoConfig, sourceWord,
  tierFor, times, withWorkflowTier,
} from "./onboarding";
import { effectiveOf } from "../screens/ProjectSettings";

describe("readiness status", () => {
  it("maps ready / warnings / blocked and treats anything else as not checked", () => {
    expect(readinessBadge("ready")).toMatchObject({ family: "success", glyph: "check", label: "Ready" });
    expect(readinessBadge("warnings")).toMatchObject({ family: "attention", label: "Warnings" });
    expect(readinessBadge("blocked")).toMatchObject({ family: "danger", glyph: "cross", label: "Blocked" });
    expect(readinessBadge(null).label).toBe("Not checked yet");
    expect(readinessBadge("whatever").family).toBe("neutral");
  });
});

describe("classes and decisions in words", () => {
  it("classes", () => {
    expect(classBadge("auto_allow")).toEqual({ label: "Allowed automatically", family: "success" });
    expect(classBadge("needs_credentials").family).toBe("attention");
    expect(classBadge("missing_tool")).toEqual({ label: "A tool is missing", family: "danger" });
    expect(classBadge("brand_new_class")).toEqual({ label: "brand new class", family: "neutral" });
  });
  it("decisions", () => {
    expect(decisionWord(null)).toBe("No decision yet");
    expect(decisionWord("approve")).toBe("Approved");
    expect(decisionWord("deny")).toBe("Denied");
    expect(decisionWord("map:jira-gateway")).toBe("Mapped to jira-gateway");
    expect(decisionWord("map", "schema-svc")).toBe("Mapped to schema-svc");
  });
});

describe("impact wording", () => {
  it("formats ratios", () => {
    expect(times(2)).toBe("2×");
    expect(times(1.54)).toBe("1.5×");
    expect(times(12.4)).toBe("12×");
    expect(times(Infinity)).toBe("many times");
  });
  it("says more / fewer and whether it is bad", () => {
    expect(deltaPhrase("rejected", 2)).toEqual({ text: "Jobs with this had 2× more rejections", bad: true });
    expect(deltaPhrase("failed", 0.5)).toEqual({ text: "Jobs with this had 2× fewer failures", bad: false });
    expect(deltaPhrase("qa_first_pass", 0.5)).toEqual({ text: "Jobs with this had 2× fewer first-time QA passes", bad: true });
    expect(deltaPhrase("repair_cycles", 1.1)).toBeNull();
    expect(deltaPhrase("rejected", null)).toBeNull();
    expect(deltaPhrase("rejected", 0)?.text).toBe("Jobs with this had many times fewer rejections");
  });
  it("lists bad news first", () => {
    const p = impactPhrases({ qa_first_pass: 2, rejected: 3, repair_cycles: 1, failed: null });
    expect(p.map((x) => x.bad)).toEqual([true, false]);
    expect(p[0].text).toContain("3× more rejections");
    expect(impactPhrases(undefined)).toEqual([]);
  });
  it("reach", () => {
    expect(reachPhrase(1, 1)).toBe("Seen in 1 job across 1 repo");
    expect(reachPhrase(12, 3)).toBe("Seen in 12 jobs across 3 repos");
  });
});

describe("command sources", () => {
  it("names CI jobs, files and defaults", () => {
    expect(sourceWord(".github/workflows/ci.yml#jobs.test")).toBe("CI: .github/workflows/ci.yml (job test)");
    expect(sourceWord(".gitlab-ci.yml")).toBe("CI: .gitlab-ci.yml");
    expect(sourceWord("default:uv")).toBe("default for uv");
    expect(sourceWord(".python-version")).toBe("from .python-version");
    expect(sourceWord(null)).toBe("not found");
  });
});

describe("run repo configuration (§6)", () => {
  const rc = {
    mode: "use", settings_hash: "sha256:abc", claude_md: ["CLAUDE.md", "src/billing/CLAUDE.md"], rules: ["python-style.md"], skills: [],
    hooks: [{ event: "PostToolUse", command_hash: "sha256:1", runs: 4, failures: 0, cause: null }],
    mcp_skipped: [{ name: "jira", identity: "url:https://jira.acme.internal", reason: "not_allowed" }], mcp_allowed: ["schema"],
    hook_events_available: true,
  };
  it("reads the section only when present", () => {
    expect(runRepoConfig(undefined)).toBeNull();
    expect(runRepoConfig({ exit_code: 0 })).toBeNull();
    expect(runRepoConfig({ repo_config: rc })?.mode).toBe("use");
  });
  it("headline", () => {
    expect(repoConfigHeadline(rc)).toBe("Used the repo's Claude Code setup: 2 CLAUDE.md files, 1 rule, 1 hook; 1 MCP server skipped.");
    expect(repoConfigHeadline({ mode: "ignore" })).toMatch(/^Repo configuration not used/);
    expect(repoConfigHeadline({ mode: "use" })).toBe("Used the repo's Claude Code setup (nothing to load).");
  });
  it("hook outcomes", () => {
    expect(hookOutcome({ runs: 4, failures: 0 })).toEqual({ text: "ran 4 times, no failures", bad: false });
    expect(hookOutcome({ runs: 1, failures: 1, cause: "missing_tool" })).toEqual({ text: "ran 1 time, 1 failed (missing tool)", bad: true });
    expect(hookOutcome({ runs: 3 }, false).text).toBe("configured (run counts not recorded)");
    expect(hookOutcome({ runs: 0 }).text).toBe("didn't run");
  });
  it("MCP skip reasons", () => {
    expect(mcpSkipWord("not_allowed")).toBe("not approved for Replicant yet");
    expect(mcpSkipWord("odd_reason")).toBe("odd reason");
  });
});

describe("model tiers (§8)", () => {
  it("resolves and edits per-workflow tiers", () => {
    expect(tierFor(undefined, "bugfix")).toBe("standard");
    expect(tierFor({ default_tier: "thorough" }, "bugfix")).toBe("thorough");
    expect(tierFor({ default_tier: "standard", workflows: { bugfix: "thorough" } }, "bugfix")).toBe("thorough");
    const m = withWorkflowTier({ default_tier: "standard", workflows: { docs: "standard" } }, "bugfix", "thorough");
    expect(m).toEqual({ default_tier: "standard", workflows: { docs: "standard", bugfix: "thorough" } });
    expect(withWorkflowTier(m, "docs", null)).toEqual({ default_tier: "standard", workflows: { bugfix: "thorough" } });
  });
  it("effective config fills the onboarding defaults for an older API", () => {
    const e = effectiveOf(undefined, {});
    expect(e.repo_config).toBe("use");
    expect(e.models).toEqual({ default_tier: "standard", workflows: {} });
    expect(e.triggers.readiness_override).toBe(false);
    const e2 = effectiveOf({ models: { default_tier: "standard", workflows: { docs: "standard" } } },
      { repo_config: "ignore", models: { workflows: { bugfix: "thorough" } }, triggers: { readiness_override: true } });
    expect(e2.repo_config).toBe("ignore");
    expect(e2.models).toEqual({ default_tier: "standard", workflows: { docs: "standard", bugfix: "thorough" } });
    expect(e2.triggers.readiness_override).toBe(true);
  });
});

describe("API calls (mocked fetch)", () => {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const mock = (status: number, body: unknown) => {
    calls.length = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      return new Response(body === undefined ? "" : JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    }));
  };
  const hdr = (i: number, k: string) => new Headers(calls[i].init.headers).get(k);
  afterEach(() => { vi.unstubAllGlobals(); });

  it("builds the fleet list query", () => {
    expect(fleetItemsPath({ kind: "hook" })).toBe("/v1/admin/fleet/items?kind=hook&sort=impact");
    expect(fleetItemsPath({ kind: "mcp_server", class: "review", sort: "repos", limit: 50, cursor: "c1" }))
      .toBe("/v1/admin/fleet/items?kind=mcp_server&class=review&sort=repos&limit=50&cursor=c1");
  });
  it("GET readiness and the fleet list", async () => {
    mock(200, { repos: [{ repo_id: "R_1", full_name: "acme/billing-py", status: "blocked" }] });
    const d = await getReadiness("prj_ledger");
    expect(calls[0].url).toBe("/v1/projects/prj_ledger/readiness");
    expect(d.repos[0].status).toBe("blocked");
    expect(hdr(0, "X-Replicant-Dev-User")).toBe("grace");
    mock(200, { items: [], next: null });
    await listFleetItems({ kind: "toolchain" });
    expect(calls[0].url).toBe("/v1/admin/fleet/items?kind=toolchain&sort=impact");
  });
  it("POST recheck, request, decide and rescan", async () => {
    mock(202, { job_id: "job_x" });
    expect((await recheckRepo("prj_ledger", "R_1")).data.job_id).toBe("job_x");
    expect(calls[0].url).toBe("/v1/projects/prj_ledger/repos/R_1:recheck");
    expect(calls[0].init.method).toBe("POST");
    expect(hdr(0, "Idempotency-Key")).toMatch(/^ui-/);

    mock(201, {});
    await requestSupport("prj_ledger", "fi_1", "we need jira");
    expect(calls[0].url).toBe("/v1/projects/prj_ledger/fleet-items/fi_1:request");
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ reason: "we need jira" });

    mock(200, { id: "fi_1" });
    await decideFleetItem("fi_1", "map", "use the gateway", "jira-gateway");
    expect(calls[0].url).toBe("/v1/admin/fleet/items/fi_1:decide");
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ decision: "map", reason: "use the gateway", map_to: "jira-gateway" });
    await decideFleetItem("fi_1", "deny", "no");
    expect(JSON.parse(String(calls[1].init.body))).toEqual({ decision: "deny", reason: "no" });

    mock(202, undefined);
    await rescanFleet();
    expect(calls[0].url).toBe("/v1/admin/fleet:rescan");
    expect(JSON.parse(String(calls[0].init.body))).toEqual({});
  });
  it("surfaces a 403 as ApiError", async () => {
    mock(403, { code: "missing_permission", title: "Forbidden" });
    await expect(listFleetItems({ kind: "hook" })).rejects.toMatchObject({ status: 403, code: "missing_permission" });
  });
});
