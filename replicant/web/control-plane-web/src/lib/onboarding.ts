// Repo onboarding (design §8c, docs/design/onboarding-interfaces.md): the shapes the web reads from control-plane-api
// (§10 admin fleet queue and project readiness, §6 run report `repo_config`, §8 project config additions), the API
// calls, and the pure helpers that turn classes, impact deltas and command sources into plain words.
import { get, post, type ApiResult } from "./api";
import type { Family } from "./status";

// ------------------------------------------------------------------------------------------------ shapes
export type EnvCommand = "install" | "build" | "test" | "lint";
export const ENV_COMMANDS: EnvCommand[] = ["install", "build", "test", "lint"];

/** §2 environment inference. */
export interface InferredEnvironment {
  environment: string; version?: string | null;
  commands: Partial<Record<EnvCommand, string | null>>;
  source?: Partial<Record<EnvCommand, string | null>>;
  supported?: boolean; unsupported_reason?: string | null;
}

/** §10 readiness `repo_config`: the profile's `claude` summary (§1). */
export interface RepoConfigSummary {
  settings_present?: boolean;
  hooks?: Array<{ event: string; matcher?: string | null; command?: string; command_hash?: string }>;
  rules?: string[]; skills?: string[]; commands?: string[]; agents?: string[]; claude_md?: string[];
  permission_rules?: number;
}

export interface ReadinessWarning { kind: string; identity: string; class: string; message: string; item_id?: string | null }
export interface ProbeCause { stage?: string; cause: string; detail?: string | null }
export interface ReadinessRepo {
  repo_id: string; full_name: string; status: string; profile_id?: string | null; sha?: string | null;
  environment?: InferredEnvironment | null; repo_config?: RepoConfigSummary | null;
  warnings?: ReadinessWarning[];
  last_probe?: { job_id: string; status: string; at: string; causes?: ProbeCause[] } | null;
}
export interface ReadinessDoc { repos: ReadinessRepo[] }

export type FleetKind = "mcp_server" | "hook" | "toolchain" | "probe_failure";
export interface ImpactDelta { qa_first_pass?: number | null; rejected?: number | null; repair_cycles?: number | null; failed?: number | null }
export interface FleetItem {
  id: string; kind: FleetKind | string; identity: string; class: string; decision?: string | null; effective?: string | null;
  repos: number; jobs: number;
  impact?: { score: number; jobs_with?: number; jobs_without?: number; delta?: ImpactDelta } | null;
  first_seen?: string; last_seen?: string; requests?: number;
}
export interface FleetDecision { decision: string; map_to?: string | null; reason?: string | null; actor?: string | null; at?: string | null }
export interface FleetRequest { project_id?: string | null; reason?: string | null; actor?: string | null; at?: string | null }
/** The detail endpoint returns `repos` and `requests` as lists (§10), shadowing the counts on the list item. */
export type FleetDetailDoc = Omit<FleetItem, "repos" | "requests"> & {
  repos: Array<{ repo_id: string; full_name: string; project_id?: string | null }>;
  requests: FleetRequest[];
  recent_jobs?: Array<{ job_id: string; status: string; outcome?: string | null; at: string }>;
  decisions?: FleetDecision[];
};

/** §6 run report `repo_config` in the role result's `cli` section. */
export interface RunRepoConfig {
  mode: "use" | "ignore" | string; config_base_sha?: string | null; settings_hash?: string | null;
  hooks?: Array<{ event: string; command_hash?: string; runs?: number | null; failures?: number | null; cause?: string | null }>;
  rules?: string[]; skills?: string[]; claude_md?: string[];
  mcp_skipped?: Array<{ name: string; identity?: string; reason: string }>;
  mcp_allowed?: string[]; permission_rules_ignored?: number; hook_events_available?: boolean;
}

/** §8 pinned `models` section. */
export interface ModelsCfg { default_tier?: string; workflows?: Record<string, string> }
export const TIERS = ["standard", "thorough"] as const;

// ------------------------------------------------------------------------------------------------ API
const idem = () => `ui-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
const enc = encodeURIComponent;

export const getReadiness = (projectId: string) => get<ReadinessDoc>(`/v1/projects/${enc(projectId)}/readiness`);
export const recheckRepo = (projectId: string, repoId: string): Promise<ApiResult<{ job_id: string }>> =>
  post(`/v1/projects/${enc(projectId)}/repos/${enc(repoId)}:recheck`, {}, idem());
export const requestSupport = (projectId: string, itemId: string, reason: string): Promise<ApiResult<unknown>> =>
  post(`/v1/projects/${enc(projectId)}/fleet-items/${enc(itemId)}:request`, { reason }, idem());

export interface FleetQuery { kind?: string; class?: string; sort?: "impact" | "repos" | "recent"; limit?: number; cursor?: string }
export function fleetItemsPath(q: FleetQuery): string {
  const sp = new URLSearchParams();
  if (q.kind) sp.set("kind", q.kind);
  if (q.class) sp.set("class", q.class);
  sp.set("sort", q.sort ?? "impact");
  if (q.limit) sp.set("limit", String(q.limit));
  if (q.cursor) sp.set("cursor", q.cursor);
  return `/v1/admin/fleet/items?${sp}`;
}
export const listFleetItems = (q: FleetQuery) => get<{ items: FleetItem[]; next?: string | null }>(fleetItemsPath(q));
export const getFleetItem = (id: string) => get<FleetDetailDoc>(`/v1/admin/fleet/items/${enc(id)}`);
export function decideFleetItem(id: string, decision: "approve" | "deny" | "map", reason: string, mapTo?: string): Promise<ApiResult<FleetItem>> {
  const body: Record<string, string> = { decision, reason };
  if (decision === "map") body.map_to = mapTo ?? "";
  return post(`/v1/admin/fleet/items/${enc(id)}:decide`, body, idem());
}
export const rescanFleet = (repoIds?: string[]): Promise<ApiResult<unknown>> =>
  post("/v1/admin/fleet:rescan", repoIds?.length ? { repo_ids: repoIds } : {}, idem());

// ------------------------------------------------------------------------------------------------ words
export interface Badge { family: Family; glyph: string; label: string; plain: string }

const READINESS: Record<string, Badge> = {
  ready: { family: "success", glyph: "check", label: "Ready", plain: "Jobs can run on this repo." },
  warnings: { family: "attention", glyph: "triangle", label: "Warnings", plain: "Jobs can run, but some of the repo's setup won't be used or may not work." },
  blocked: { family: "danger", glyph: "cross", label: "Blocked", plain: "Jobs can't run on this repo until the problem below is fixed." },
  pending: { family: "info", glyph: "ring-spin", label: "Checking", plain: "A readiness check is running." },
};
/** Readiness status → badge. Anything else (no profile yet, never probed) reads as "Not checked yet". */
export function readinessBadge(status?: string | null): Badge {
  return READINESS[status ?? ""] ?? { family: "neutral", glyph: "ring-dotted", label: "Not checked yet", plain: "This repo hasn't been checked yet." };
}

export const KINDS: Array<[FleetKind, string]> = [["mcp_server", "MCP servers"], ["hook", "Hooks"], ["toolchain", "Toolchains"], ["probe_failure", "Probe failures"]];
export const kindWord = (k: string) => ({ mcp_server: "MCP server", hook: "Hook", toolchain: "Toolchain", probe_failure: "Probe failure" } as Record<string, string>)[k] ?? k;

const CLASS_WORDS: Record<string, [string, Family]> = {
  auto_allow: ["Allowed automatically", "success"],
  map: ["Platform has an equivalent", "info"],
  needs_egress: ["Needs network access", "attention"],
  needs_credentials: ["Needs credentials", "attention"],
  review: ["Needs review", "attention"],
  ok: ["Works in the sandbox", "success"],
  at_risk: ["May fail in the sandbox", "attention"],
  supported: ["Supported", "success"],
  unsupported: ["Not supported", "danger"],
  missing_tool: ["A tool is missing", "danger"],
  network: ["Needed the network", "danger"],
  timeout: ["Timed out", "danger"],
  test_failure: ["Tests failed", "danger"],
  build_failure: ["Build failed", "danger"],
  install_failure: ["Install failed", "danger"],
};
export function classBadge(cls?: string | null): { label: string; family: Family } {
  const w = CLASS_WORDS[cls ?? ""];
  return w ? { label: w[0], family: w[1] } : { label: (cls ?? "unknown").replace(/_/g, " "), family: "neutral" };
}

/** Admin decision → words; `map:<service>` or decision "map" with `map_to`. */
export function decisionWord(decision?: string | null, mapTo?: string | null): string {
  if (!decision) return "No decision yet";
  if (decision === "approve" || decision === "allowed") return "Approved";
  if (decision === "deny" || decision === "denied") return "Denied";
  if (decision.startsWith("map")) {
    const to = mapTo ?? (decision.includes(":") ? decision.slice(decision.indexOf(":") + 1) : "");
    return to ? `Mapped to ${to}` : "Mapped to a platform service";
  }
  return decision.replace(/_/g, " ");
}

/** "2×", "1.5×": one decimal below 10, none above. */
export function times(r: number): string {
  if (!Number.isFinite(r)) return "many times";
  const v = r >= 10 ? Math.round(r) : Math.round(r * 10) / 10;
  return `${v}×`;
}

const DELTA_WORDS: Record<keyof ImpactDelta, { more: string; fewer: string; same: string; higherIsBad: boolean }> = {
  rejected: { more: "more rejections", fewer: "fewer rejections", same: "about as many rejections", higherIsBad: true },
  failed: { more: "more failures", fewer: "fewer failures", same: "about as many failures", higherIsBad: true },
  repair_cycles: { more: "more repair rounds", fewer: "fewer repair rounds", same: "about as many repair rounds", higherIsBad: true },
  qa_first_pass: { more: "more first-time QA passes", fewer: "fewer first-time QA passes", same: "about as many first-time QA passes", higherIsBad: false },
};

/** One delta → a plain sentence. ASSUMPTION (contract §10 gives the field, not its unit): each delta is the ratio of
 * the outcome rate in jobs that had the item to jobs that didn't (1 = no difference; null = not enough data).
 * Returns the sentence and whether it is bad news, or null when there is no meaningful difference. */
export function deltaPhrase(key: keyof ImpactDelta, ratio: number | null | undefined): { text: string; bad: boolean } | null {
  if (ratio == null || Number.isNaN(ratio) || ratio < 0) return null;
  const w = DELTA_WORDS[key];
  if (ratio >= 1.25) return { text: `Jobs with this had ${times(ratio)} ${w.more}`, bad: w.higherIsBad };
  if (ratio <= 0.8) {
    const inv = ratio === 0 ? Infinity : 1 / ratio;
    return { text: `Jobs with this had ${times(inv)} ${w.fewer}`, bad: !w.higherIsBad };
  }
  return null;
}

/** Every meaningful delta, worst first. */
export function impactPhrases(delta?: ImpactDelta | null): Array<{ text: string; bad: boolean }> {
  if (!delta) return [];
  const keys: Array<keyof ImpactDelta> = ["failed", "rejected", "repair_cycles", "qa_first_pass"];
  const out = keys.map((k) => deltaPhrase(k, delta[k])).filter((x): x is { text: string; bad: boolean } => !!x);
  return [...out.filter((x) => x.bad), ...out.filter((x) => !x.bad)];
}

/** "Seen in 12 jobs across 3 repos" */
export function reachPhrase(jobs: number, repos: number): string {
  const p = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;
  return `Seen in ${p(jobs, "job")} across ${p(repos, "repo")}`;
}

/** Where a command came from (§2 `source`): CI file and job, a version/manifest file, or a default. */
export function sourceWord(src?: string | null): string {
  if (!src) return "not found";
  if (src.startsWith("default:")) return `default for ${src.slice(8)}`;
  if (src === "default") return "default";
  if (src.startsWith("binding")) return "set by the codebase binding";
  const [file, frag] = src.split("#");
  const job = frag?.match(/^jobs\.([^.]+)/)?.[1];
  if (/^\.github\/workflows\/|^\.gitlab-ci\.yml$/.test(file)) return job ? `CI: ${file} (job ${job})` : `CI: ${file}`;
  return `from ${file}${frag ? ` (${frag})` : ""}`;
}

const MCP_SKIP: Record<string, string> = {
  not_allowed: "not approved for Replicant yet",
  denied: "blocked by an admin",
  needs_credentials: "needs credentials the sandbox doesn't have",
  needs_egress: "needs network access the sandbox doesn't have",
  mapped: "replaced by a platform service",
  ignored: "repo configuration is turned off",
};
export const mcpSkipWord = (reason?: string | null) => MCP_SKIP[reason ?? ""] ?? (reason ?? "skipped").replace(/_/g, " ");

/** §6 hook outcome in words. Without hook events from the CLI only "configured" is known. */
export function hookOutcome(h: { runs?: number | null; failures?: number | null; cause?: string | null }, eventsAvailable = true): { text: string; bad: boolean } {
  if (!eventsAvailable || h.runs == null) return { text: "configured (run counts not recorded)", bad: false };
  if (h.runs === 0) return { text: "didn't run", bad: false };
  const ran = `ran ${h.runs} time${h.runs === 1 ? "" : "s"}`;
  const f = h.failures ?? 0;
  if (!f) return { text: `${ran}, no failures`, bad: false };
  return { text: `${ran}, ${f} failed${h.cause ? ` (${h.cause.replace(/_/g, " ")})` : ""}`, bad: true };
}

/** One plain sentence for the run's repo configuration. */
export function repoConfigHeadline(rc: RunRepoConfig): string {
  if (rc.mode === "ignore") return "Repo configuration not used: this project runs with Replicant's own settings only.";
  const parts: string[] = [];
  const n = (k: number, w: string) => `${k} ${w}${k === 1 ? "" : "s"}`;
  if (rc.claude_md?.length) parts.push(n(rc.claude_md.length, "CLAUDE.md file"));
  if (rc.rules?.length) parts.push(n(rc.rules.length, "rule"));
  if (rc.skills?.length) parts.push(n(rc.skills.length, "skill"));
  if (rc.hooks?.length) parts.push(n(rc.hooks.length, "hook"));
  const skipped = rc.mcp_skipped?.length ? `; ${n(rc.mcp_skipped.length, "MCP server")} skipped` : "";
  return parts.length ? `Used the repo's Claude Code setup: ${parts.join(", ")}${skipped}.` : `Used the repo's Claude Code setup (nothing to load)${skipped}.`;
}

/** The `repo_config` section of a role result's `cli` block, or null for old runs. */
export function runRepoConfig(cli: Record<string, unknown> | undefined | null): RunRepoConfig | null {
  const rc = cli?.repo_config;
  return rc && typeof rc === "object" && typeof (rc as RunRepoConfig).mode === "string" ? (rc as RunRepoConfig) : null;
}

// ------------------------------------------------------------------------------------------------ models
export const tierFor = (m: ModelsCfg | undefined, wt: string): string => m?.workflows?.[wt] ?? m?.default_tier ?? "standard";

/** Set (or clear, with null) one workflow type's tier; returns a new section. */
export function withWorkflowTier(m: ModelsCfg | undefined, wt: string, tier: string | null): ModelsCfg {
  const wf = { ...(m?.workflows ?? {}) };
  if (tier) wf[wt] = tier; else delete wf[wt];
  return { default_tier: m?.default_tier ?? "standard", workflows: wf };
}
