// Project settings (v0 CONFIG-01): triggers, approvals & risk, workflow graph, access and history, edited by the team
// under RBAC and versioned. Live settings apply immediately; pinned ones apply to new jobs (a derived release bundle).
import { useMemo, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { TopBar } from "../components/Shell";
import { Banner, Empty, ErrorBanner } from "../components/bits";
import { Dialog } from "../components/Dialog";
import { ApiError, get, post, request } from "../lib/api";
import { useMe, useSession } from "../lib/session";
import { dateTime } from "../lib/format";
import { GraphSvg } from "./JobGraph";
import { Forbidden } from "./Admin";
import { Link } from "../router";
import type { GraphDoc } from "../lib/graph";

// ------------------------------------------------------------------------------------------------ types
type Section = "triggers" | "approvals" | "gates" | "risk" | "workflows";
interface WorkflowCfg { params?: { max_review_rounds?: number }; max_visits?: Record<string, number>; graph_yaml?: string | null }
export interface ProjectConfig {
  triggers?: { enabled?: boolean; label?: string; modes?: string[]; allowed_workflow_types?: string[]; default_priority?: string; min_trigger_role?: string };
  approvals?: { approver_groups?: string[]; slack_channel?: string | null };
  gates?: { plan?: string; publish?: string; requester_may_decide?: boolean };
  risk?: { large_change_files?: number; sensitive_paths?: string[]; forbidden_paths?: string[] };
  workflows?: Record<string, WorkflowCfg>;
}
type Effective = Required<{ [K in keyof ProjectConfig]-?: NonNullable<ProjectConfig[K]> }>;
interface Version { version: number; config: ProjectConfig; is_current: boolean; actor: string; reason: string; rolled_back_from?: number | null; created_at: string }
interface ConfigDoc {
  project_id: string; tenant_id: string; version: number; config: ProjectConfig; effective: Effective; defaults: Effective;
  history: Version[]; can_edit: { live: boolean; pinned: boolean };
  repos: Record<string, { full_name: string; version: number; config: ProjectConfig }>;
}
interface Change { path: string; from: unknown; to: unknown; applies: "live" | "new_jobs" }
interface ValidateDoc { valid: boolean; errors: Array<{ path: string; message: string }>; changes: Change[]; graphs: Record<string, { definition: { ref: string }; graph: NonNullable<GraphDoc["graph"]> }> }
interface ProjectDoc { project_id: string; tenant_id: string; name: string; owners_group?: string | null; repos: Array<{ repo_id: string; full_name: string; host: string }>; permissions: string[] }
interface AccessDoc { version: number; bindings: Array<{ group: string; role: string }>; inherited: Array<{ group: string; role: string; scope: string }>; members: Record<string, Array<{ login: string }>>; roles: string[]; can_edit: boolean }
interface ProjectRow { project_id: string; tenant_id: string; name: string; repo_count: number; permissions: string[] }

const WF_TYPES = ["bugfix", "feature", "api_change", "docs", "hotfix"];
const TABS = [["overview", "Overview"], ["triggers", "Triggers"], ["approvals", "Approvals & risk"], ["workflow", "Workflow"], ["access", "Access"], ["history", "History"]] as const;

export function Applies({ live }: { live: boolean }) {
  return <span className={"applies " + (live ? "live" : "newjobs")} title={live ? "Read when the next event or notification happens" : "Compiled into the release bundle of jobs started after the change; running jobs keep theirs"}>
    {live ? "Applies immediately" : "Applies to new jobs"}</span>;
}

function Field({ label, live, children, hint }: { label: string; live: boolean; children: ReactNode; hint?: ReactNode }) {
  return (
    <div className="cfgrow">
      <div className="cfglabel"><span>{label}</span><Applies live={live} /></div>
      <div className="cfgctl">{children}{hint ? <div className="t3 small">{hint}</div> : null}</div>
    </div>
  );
}

const csv = (v?: string[]) => (v ?? []).join(", ");
const uncsv = (s: string) => s.split(/[,\n]/).map((x) => x.trim()).filter(Boolean);

// ------------------------------------------------------------------------------------------------ list
export function Projects() {
  const { user } = useSession();
  const q = useQuery({ queryKey: ["projects", user], queryFn: () => get<{ items: ProjectRow[] }>("/v1/projects") });
  return (
    <>
      <TopBar><b>Projects</b><span className="t3">· team configuration</span></TopBar>
      <main className="main">
        <div className="section">
          {q.isError ? <ErrorBanner error={q.error} what="projects" retry={() => void q.refetch()} /> : null}
          {q.data && !q.data.items.length ? <Empty title="No projects">You are not a member of any project.</Empty> : null}
          {q.data?.items.length ? (
            <table className="t" aria-label="Projects">
              <thead><tr><th>Project</th><th>Tenant</th><th className="right">Repos</th><th>Your access</th></tr></thead>
              <tbody>
                {q.data.items.map((p) => (
                  <tr key={p.project_id}>
                    <td><Link to={`/projects/${p.project_id}/settings/overview`}><b>{p.name}</b></Link> <span className="t3 mono small">{p.project_id}</span></td>
                    <td>{p.tenant_id}</td><td className="right num">{p.repo_count}</td>
                    <td className="t2 small">{accessWord(p.permissions)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </div>
      </main>
    </>
  );
}

const accessWord = (perms: string[]) =>
  perms.includes("tenant:policy:edit") || perms.includes("tenant:ops:edit") ? "can edit settings" : perms.includes("job:read") ? "read-only" : "none";

// ------------------------------------------------------------------------------------------------ settings
export function ProjectSettings({ projectId, tab }: { projectId: string; tab?: string }) {
  const { user } = useSession();
  const qc = useQueryClient();
  const me = useMe().data;
  const t = (TABS.find(([k]) => k === tab)?.[0] ?? "overview") as (typeof TABS)[number][0];
  const proj = useQuery({ queryKey: ["project", user, projectId], queryFn: () => get<ProjectDoc>(`/v1/projects/${projectId}`) });
  const cfg = useQuery({ queryKey: ["project-config", user, projectId], queryFn: () => get<ConfigDoc>(`/v1/projects/${projectId}/config`) });
  const [draft, setDraft] = useState<ProjectConfig | null>(null);
  const [review, setReview] = useState<ValidateDoc | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const base = cfg.data?.config ?? {};
  const cur: ProjectConfig = draft ?? base;
  const eff = useMemo(() => effectiveOf(cfg.data?.defaults, cur), [cfg.data?.defaults, cur]);
  const dirty = draft !== null && JSON.stringify(draft) !== JSON.stringify(base);
  const canLive = !!cfg.data?.can_edit.live, canPinned = !!cfg.data?.can_edit.pinned;
  const edit = (sec: Section, patch: Record<string, unknown>) => {
    setSaved(null);
    setDraft({ ...cur, [sec]: { ...(cur[sec] as object ?? {}), ...patch } });
  };
  const setWorkflow = (wt: string, w: WorkflowCfg | null) => {
    const wf = { ...(cur.workflows ?? {}) };
    if (w) wf[wt] = w; else delete wf[wt];
    setSaved(null);
    setDraft({ ...cur, workflows: wf });
  };
  if (proj.isError && proj.error instanceof ApiError && proj.error.status === 404) {
    return <><TopBar><b>Projects</b></TopBar><main className="main"><Forbidden what="this project" perm="job:read" error={proj.error} /></main></>;
  }
  const startReview = async () => {
    setBusy(true); setErr(null);
    try {
      setReview((await request<ValidateDoc>(`/v1/projects/${projectId}/config:validate`, { method: "POST", body: JSON.stringify({ config: cur }) })).data);
    } catch (e) { setErr(e); } finally { setBusy(false); }
  };
  const save = async (reason: string) => {
    setBusy(true); setErr(null);
    try {
      const r = await request<{ written_version: number }>(`/v1/projects/${projectId}/config`, {
        method: "PUT", body: JSON.stringify({ config: cur, reason }), headers: { "If-Match": String(cfg.data?.version ?? 0) } });
      setSaved(`Saved as version ${r.data.written_version}.`);
      setDraft(null); setReview(null);
      await qc.invalidateQueries({ queryKey: ["project-config", user, projectId] });
    } catch (e) { setErr(e); } finally { setBusy(false); }
  };
  const readOnly = cfg.data && !canLive && !canPinned;
  return (
    <>
      <TopBar><Link to="/projects">Projects</Link><span className="sep">/</span><b>{proj.data?.name ?? projectId}</b><span className="sep">/</span><span>Settings</span>
        {readOnly ? <span className="t3">· read-only</span> : null}</TopBar>
      <main className="main">
        <nav className="tabs" aria-label="Project settings">
          {TABS.map(([k, label]) => <Link key={k} to={`/projects/${projectId}/settings/${k}`} className={t === k ? "on" : ""} aria-current={t === k ? "page" : undefined}>{label}</Link>)}
        </nav>
        {cfg.isError ? <ErrorBanner error={cfg.error} what="the project configuration" retry={() => void cfg.refetch()} /> : null}
        {readOnly ? <Banner kind="neutral" glyph="plain" lead="Read-only.">You can see this project's settings, but changing them needs <span className="mono">tenant:ops:edit</span> or <span className="mono">tenant:policy:edit</span> on the project ({me?.display_name}).</Banner> : null}
        {saved ? <Banner kind="info" glyph="check" lead={saved}>Live settings apply now; gate, risk and workflow settings apply to jobs started from now on.</Banner> : null}
        {err && !review ? <ErrorBanner error={err} what="the change" /> : null}
        {!cfg.data || !proj.data ? <div className="section"><div className="skel-line w40" /><div className="skel-line w70" /></div> : (
          <div className="section cfg">
            {t === "overview" ? <Overview p={proj.data} c={cfg.data} /> : null}
            {t === "triggers" ? <Triggers eff={eff} can={canLive} edit={edit} /> : null}
            {t === "approvals" ? <Approvals eff={eff} canLive={canLive} canPinned={canPinned} edit={edit} /> : null}
            {t === "workflow" ? <Workflow projectId={projectId} eff={eff} cur={cur} can={canPinned} setWorkflow={setWorkflow} /> : null}
            {t === "access" ? <Access projectId={projectId} /> : null}
            {t === "history" ? <History projectId={projectId} c={cfg.data} /> : null}
            {["triggers", "approvals", "workflow"].includes(t) && (canLive || canPinned) ? (
              <div className="cfgfoot">
                <button className="btn primary" disabled={!dirty || busy} onClick={() => void startReview()}>Review changes…</button>
                <button className="btn ghost" disabled={!dirty || busy} onClick={() => { setDraft(null); setSaved(null); }}>Discard</button>
                <span className="t3 small">{dirty ? "Unsaved changes" : `Version ${cfg.data.version}`}</span>
              </div>
            ) : null}
          </div>
        )}
        {review ? <ReviewDialog v={review} busy={busy} err={err} onClose={() => { setReview(null); setErr(null); }} onSave={(r) => void save(r)} /> : null}
      </main>
    </>
  );
}

function effectiveOf(defaults: Effective | undefined, cfg: ProjectConfig): Effective {
  const d = defaults ?? ({ triggers: {}, approvals: {}, gates: {}, risk: {}, workflows: {} } as Effective);
  return {
    triggers: { ...d.triggers, ...cfg.triggers }, approvals: { ...d.approvals, ...cfg.approvals },
    gates: { ...d.gates, ...cfg.gates }, risk: { ...d.risk, ...cfg.risk }, workflows: { ...d.workflows, ...cfg.workflows },
  };
}

function ReviewDialog({ v, busy, err, onClose, onSave }: { v: ValidateDoc; busy: boolean; err: unknown; onClose: () => void; onSave: (reason: string) => void }) {
  const [reason, setReason] = useState("");
  return (
    <Dialog label="Review configuration change" title="Review and save" sub="Every change is versioned with your name and reason, and can be rolled back." onClose={onClose}
      foot={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!v.valid || !reason.trim() || busy || !v.changes.length} onClick={() => onSave(reason)}>Save version</button></>}>
      {!v.valid ? <Banner kind="danger" glyph="cross" lead="The configuration is not valid.">{v.errors.map((e) => <div key={e.path + e.message} className="small"><span className="mono">{e.path}</span> {e.message}</div>)}</Banner> : null}
      {v.valid && !v.changes.length ? <p className="t2">Nothing changes.</p> : null}
      {v.changes.length ? (
        <table className="t" aria-label="Changes"><thead><tr><th>Setting</th><th>From</th><th>To</th><th>When</th></tr></thead>
          <tbody>{v.changes.map((c) => <tr key={c.path}><td className="mono small">{c.path}</td><td className="mono small t3">{show(c.from)}</td><td className="mono small">{show(c.to)}</td><td><Applies live={c.applies === "live"} /></td></tr>)}</tbody></table>
      ) : null}
      <label className="small t2" style={{ display: "block", marginTop: 12 }}>Reason (required)
        <textarea className="field" rows={2} aria-label="Reason" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why this change?" /></label>
      {err ? <ErrorBanner error={err} what="the change" /> : null}
    </Dialog>
  );
}

const show = (v: unknown) => v === null || v === undefined ? "—" : typeof v === "string" ? (v.length > 40 ? v.slice(0, 40) + "…" : v) : JSON.stringify(v);

// ------------------------------------------------------------------------------------------------ tabs
function Overview({ p, c }: { p: ProjectDoc; c: ConfigDoc }) {
  return (
    <dl className="kv">
      <dt>Project</dt><dd><b>{p.name}</b> <span className="mono t3">{p.project_id}</span></dd>
      <dt>Tenant</dt><dd>{p.tenant_id}</dd>
      <dt>Owners</dt><dd className="mono">{p.owners_group ?? "—"}</dd>
      <dt>Repositories</dt><dd>{p.repos.length ? p.repos.map((r) => <div key={r.repo_id} className="mono">{r.full_name} <span className="t3">({r.host}, id {r.repo_id})</span>
        {p.permissions.includes("tenant:policy:edit") || p.permissions.includes("memory:admin") ? <ScanButton project={p.project_id} repo={r.repo_id} /> : null}</div>) : <span className="t3">none</span>}</dd>
      <dt>Configuration</dt><dd>version {c.version}{c.history[0] ? <span className="t3"> · {dateTime(c.history[0].created_at)} · {c.history[0].reason}</span> : null}</dd>
      <dt>Your access</dt><dd className="small">{p.permissions.join(", ") || "none"}</dd>
    </dl>
  );
}

// MEM: "Learn this repo's conventions" starts a repo scan job; its proposals land in the memory candidates queue.
function ScanButton({ project, repo }: { project: string; repo: string }) {
  const [res, setRes] = useState<{ job_id?: string; error?: string } | null>(null);
  const run = async () => {
    try {
      const r = await post<{ job_id: string }>(`/v1/projects/${project}/repos/${repo}:scan`, {});
      setRes({ job_id: r.data.job_id });
    } catch (e) { setRes({ error: e instanceof ApiError ? `${e.status} ${e.code}` : String(e) }); }
  };
  return (
    <span className="row g1" style={{ display: "inline-flex", marginLeft: 8 }}>
      <button className="btn sm" onClick={() => void run()}>Learn this repo's conventions</button>
      {res?.job_id ? <span className="small">scan <Link to={`/jobs/${res.job_id}`}>{res.job_id.slice(0, 14)}…</Link> · proposals go to <Link to="/admin/memory?tab=candidates">memory candidates</Link></span> : null}
      {res?.error ? <span className="small danger">{res.error}</span> : null}
    </span>
  );
}

function Triggers({ eff, can, edit }: { eff: Effective; can: boolean; edit: (s: Section, p: Record<string, unknown>) => void }) {
  const t = eff.triggers;
  return (
    <div className="cfgform" aria-label="Triggers">
      <Field label="Enabled" live><label className="row g1"><input type="checkbox" disabled={!can} checked={!!t.enabled} onChange={(e) => edit("triggers", { enabled: e.target.checked })} /> Start jobs from issues in this project's repos</label></Field>
      <Field label="Trigger label" live hint="An issue with this label starts a job."><input className="field" aria-label="Trigger label" disabled={!can} value={t.label ?? ""} onChange={(e) => edit("triggers", { label: e.target.value })} /></Field>
      <Field label="Modes" live>{["label", "command"].map((m) => <label key={m} className="row g1"><input type="checkbox" disabled={!can} checked={!!t.modes?.includes(m)} onChange={(e) => edit("triggers", { modes: toggle(t.modes, m, e.target.checked) })} /> {m === "label" ? "Issue label" : "/replicant command comment"}</label>)}</Field>
      <Field label="Workflow types" live hint="auto lets the router pick.">{["auto", ...WF_TYPES.filter((w) => w !== "hotfix")].map((w) => <label key={w} className="row g1"><input type="checkbox" disabled={!can} checked={!!t.allowed_workflow_types?.includes(w)} onChange={(e) => edit("triggers", { allowed_workflow_types: toggle(t.allowed_workflow_types, w, e.target.checked) })} /> {w}</label>)}</Field>
      <Field label="Default priority" live><select className="field" aria-label="Default priority" disabled={!can} value={t.default_priority} onChange={(e) => edit("triggers", { default_priority: e.target.value })}>{["low", "normal", "high"].map((p) => <option key={p}>{p}</option>)}</select></Field>
      <Field label="Who can trigger" live hint="Least role on the project that may start a job."><select className="field" aria-label="Who can trigger" disabled={!can} value={t.min_trigger_role} onChange={(e) => edit("triggers", { min_trigger_role: e.target.value })}>
        <option value="engineer">Engineers and above</option><option value="approver">Approvers and above</option><option value="project_admin">Project admins only</option></select></Field>
    </div>
  );
}

const toggle = (arr: string[] | undefined, v: string, on: boolean) => on ? [...new Set([...(arr ?? []), v])] : (arr ?? []).filter((x) => x !== v);

function Approvals({ eff, canLive, canPinned, edit }: { eff: Effective; canLive: boolean; canPinned: boolean; edit: (s: Section, p: Record<string, unknown>) => void }) {
  const g = eff.gates, a = eff.approvals, r = eff.risk;
  return (
    <div className="cfgform" aria-label="Approvals and risk">
      <h3 className="cfgh">Gates</h3>
      <Field label="Plan approval" live={false}><select className="field" aria-label="Plan approval" disabled={!canPinned} value={g.plan} onChange={(e) => edit("gates", { plan: e.target.value })}>
        <option value="off">Off</option><option value="risk_medium">When risk is medium or higher</option><option value="always">Always</option></select></Field>
      <Field label="Publish approval" live={false} hint="The publish gate cannot be turned off."><select className="field" aria-label="Publish approval" disabled={!canPinned} value={g.publish} onChange={(e) => edit("gates", { publish: e.target.value })}>
        <option value="always">Always</option><option value="risk_medium">When risk is medium or higher</option></select></Field>
      <Field label="Requester may approve" live={false}><label className="row g1"><input type="checkbox" disabled={!canPinned} checked={!!g.requester_may_decide} onChange={(e) => edit("gates", { requester_may_decide: e.target.checked })} /> The person who asked may approve their own job</label></Field>
      <h3 className="cfgh">Approvers</h3>
      <Field label="Approver groups" live hint="Comma-separated. Empty: anyone with approve rights on the project."><input className="field" aria-label="Approver groups" disabled={!canLive} value={csv(a.approver_groups)} onChange={(e) => edit("approvals", { approver_groups: uncsv(e.target.value) })} /></Field>
      <Field label="Slack approval channel" live hint="Channel ID (C…); empty uses the tenant's channel."><input className="field" aria-label="Slack approval channel" disabled={!canLive} value={a.slack_channel ?? ""} onChange={(e) => edit("approvals", { slack_channel: e.target.value.trim() || null })} /></Field>
      <h3 className="cfgh">Risk</h3>
      <Field label="Large change" live={false} hint="More changed files than this is at least medium risk."><input className="field" type="number" min={0} aria-label="Large change threshold" disabled={!canPinned} value={r.large_change_files ?? 20} onChange={(e) => edit("risk", { large_change_files: Math.max(0, Number(e.target.value) || 0) })} /></Field>
      <Field label="Sensitive paths" live={false} hint="Globs; touching one is at least high risk."><textarea className="field" rows={2} aria-label="Sensitive paths" disabled={!canPinned} value={csv(r.sensitive_paths)} onChange={(e) => edit("risk", { sensitive_paths: uncsv(e.target.value) })} /></Field>
      <Field label="Forbidden paths" live={false} hint="Globs; the verifier fails a change touching one."><textarea className="field" rows={2} aria-label="Forbidden paths" disabled={!canPinned} value={csv(r.forbidden_paths)} onChange={(e) => edit("risk", { forbidden_paths: uncsv(e.target.value) })} /></Field>
    </div>
  );
}

function Workflow({ projectId, eff, cur, can, setWorkflow }: { projectId: string; eff: Effective; cur: ProjectConfig; can: boolean; setWorkflow: (wt: string, w: WorkflowCfg | null) => void }) {
  const [wt, setWt] = useState("bugfix");
  const [preview, setPreview] = useState<ValidateDoc | null>(null);
  const [perr, setPerr] = useState<unknown>(null);
  const w: WorkflowCfg = eff.workflows[wt] ?? {};
  const advanced = w.graph_yaml != null;
  const doPreview = async () => {
    setPerr(null);
    try {
      setPreview((await request<ValidateDoc>(`/v1/projects/${projectId}/config:validate`, { method: "POST", body: JSON.stringify({ config: cur }) })).data);
    } catch (e) { setPerr(e); }
  };
  const g = preview?.graphs[wt];
  return (
    <div className="cfgform" aria-label="Workflow">
      <Field label="Workflow" live={false}><select className="field" aria-label="Workflow type" value={wt} onChange={(e) => { setWt(e.target.value); setPreview(null); }}>{WF_TYPES.map((x) => <option key={x}>{x}</option>)}</select></Field>
      <Field label="Review rounds" live={false} hint="Maximum reviewer runs per job."><input className="field" type="number" min={1} max={10} aria-label="Max review rounds" disabled={!can || advanced} value={w.params?.max_review_rounds ?? ""} placeholder="default"
        onChange={(e) => { const n = Number(e.target.value); setWorkflow(wt, n ? { ...w, params: { ...w.params, max_review_rounds: n } } : (w.max_visits ? { ...w, params: undefined } : null)); }} /></Field>
      <Field label="Advanced" live={false} hint="A whole GraphDefinition in YAML; only node types the worker already has.">
        <label className="row g1"><input type="checkbox" disabled={!can} checked={advanced} onChange={(e) => setWorkflow(wt, e.target.checked ? { graph_yaml: SKELETON(wt) } : null)} /> Edit the graph as YAML</label>
        {advanced ? <textarea className="field mono yaml" rows={16} aria-label="Graph YAML" disabled={!can} value={w.graph_yaml ?? ""} onChange={(e) => setWorkflow(wt, { graph_yaml: e.target.value })} /> : null}
      </Field>
      <div className="row g1"><button className="btn" onClick={() => void doPreview()}>{eff.workflows[wt] ? "Validate and preview" : "View effective graph"}</button>
        {!eff.workflows[wt] ? <span className="t3 small">{wt} uses the release bundle's default graph.</span> : null}</div>
      {perr ? <ErrorBanner error={perr} what="the preview" /> : null}
      {preview && !preview.valid ? <Banner kind="danger" glyph="cross" lead="Not valid.">{preview.errors.map((e) => <div key={e.path + e.message} className="small"><span className="mono">{e.path}</span> {e.message}</div>)}</Banner> : null}
      {preview?.valid && !g ? <p className="t3 small">No tuned graph for {wt}: new jobs use the default definition.</p> : null}
      {g ? <div className="cfgpreview"><div className="small t3 mono">{g.definition.ref} · {g.graph.nodes.length} nodes</div><div className="gwrap"><GraphSvg doc={g.graph} onSelect={() => undefined} /></div></div> : null}
    </div>
  );
}

const SKELETON = (wt: string) => `apiVersion: replicant.example.com/v1
kind: GraphDefinition
metadata: {name: ${wt.replace("_", "-")}}
spec:
  workflowType: ${wt}
  fglVersion: 1
  entry: context
  nodes:
    - {key: context, type: context_assembler, config: {briefing: standard}}
  edges:
    - {from: context, to: "end:no_change_needed"}
`;

function Access({ projectId }: { projectId: string }) {
  const { user } = useSession();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["project-access", user, projectId], queryFn: () => get<AccessDoc>(`/v1/projects/${projectId}/access`) });
  const [rows, setRows] = useState<Array<{ group: string; role: string }> | null>(null);
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<unknown>(null);
  if (q.isError) return <ErrorBanner error={q.error} what="access" />;
  if (!q.data) return null;
  const cur = rows ?? q.data.bindings;
  const save = async () => {
    setErr(null);
    try {
      await request(`/v1/projects/${projectId}/access`, { method: "PUT", body: JSON.stringify({ bindings: cur, reason }) });
      setRows(null); setReason("");
      await qc.invalidateQueries({ queryKey: ["project-access", user, projectId] });
    } catch (e) { setErr(e); }
  };
  return (
    <div aria-label="Access">
      <h3 className="cfgh">Project bindings <Applies live /></h3>
      <table className="t" aria-label="Project role bindings"><thead><tr><th>Group</th><th>Role</th><th>Members</th><th /></tr></thead>
        <tbody>
          {cur.map((b, i) => (
            <tr key={i}>
              <td>{q.data.can_edit ? <input className="field" aria-label="Group" value={b.group} onChange={(e) => setRows(cur.map((x, j) => j === i ? { ...x, group: e.target.value } : x))} /> : <span className="mono">{b.group}</span>}</td>
              <td>{q.data.can_edit ? <select className="field" aria-label="Role" value={b.role} onChange={(e) => setRows(cur.map((x, j) => j === i ? { ...x, role: e.target.value } : x))}>{q.data.roles.map((r) => <option key={r}>{r}</option>)}</select> : b.role}</td>
              <td className="small t2">{(q.data.members[b.group] ?? []).map((m) => m.login).join(", ") || "—"}</td>
              <td>{q.data.can_edit ? <button className="btn sm ghost" onClick={() => setRows(cur.filter((_, j) => j !== i))}>Remove</button> : null}</td>
            </tr>
          ))}
          {!cur.length ? <tr><td colSpan={4} className="t3 small">No project bindings.</td></tr> : null}
        </tbody>
      </table>
      {q.data.can_edit ? (
        <div className="cfgfoot">
          <button className="btn sm" onClick={() => setRows([...cur, { group: "", role: "engineer" }])}>Add binding</button>
          <input className="field" style={{ maxWidth: 320 }} aria-label="Access change reason" placeholder="Reason (required)" value={reason} onChange={(e) => setReason(e.target.value)} />
          <button className="btn primary" disabled={rows === null || !reason.trim()} onClick={() => void save()}>Save access</button>
        </div>
      ) : <p className="t3 small">Read-only: changing access needs <span className="mono">identity:admin</span> on the project.</p>}
      {err ? <ErrorBanner error={err} what="the access change" /> : null}
      <h3 className="cfgh">Inherited from the tenant and platform</h3>
      <table className="t" aria-label="Inherited role bindings"><thead><tr><th>Group</th><th>Role</th><th>Scope</th><th>Members</th></tr></thead>
        <tbody>{q.data.inherited.map((b) => <tr key={b.group + b.role + b.scope}><td className="mono">{b.group}</td><td>{b.role}</td><td>{b.scope}</td><td className="small t2">{(q.data.members[b.group] ?? []).map((m) => m.login).join(", ") || "—"}</td></tr>)}</tbody></table>
    </div>
  );
}

function History({ projectId, c }: { projectId: string; c: ConfigDoc }) {
  const { user } = useSession();
  const qc = useQueryClient();
  const [rb, setRb] = useState<Version | null>(null);
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<unknown>(null);
  const can = c.can_edit.live || c.can_edit.pinned;
  const doRollback = async () => {
    if (!rb) return;
    setErr(null);
    try {
      await request(`/v1/projects/${projectId}/config:rollback`, { method: "POST", body: JSON.stringify({ version: rb.version, reason: reason || `rollback to version ${rb.version}` }) });
      setRb(null); setReason("");
      await qc.invalidateQueries({ queryKey: ["project-config", user, projectId] });
    } catch (e) { setErr(e); }
  };
  if (!c.history.length) return <Empty title="No versions yet">The project uses the defaults.</Empty>;
  return (
    <div aria-label="History">
      <table className="t" aria-label="Configuration versions"><thead><tr><th>Version</th><th>When</th><th>Who</th><th>Why</th><th>What changed</th><th /></tr></thead>
        <tbody>{c.history.map((v, i) => {
          const prev = c.history[i + 1];
          const diff = diffPaths(prev?.config ?? {}, v.config);
          return (
            <tr key={v.version}>
              <td className="num">v{v.version}{v.is_current ? <span className="t3 small"> · current</span> : null}</td>
              <td className="small">{dateTime(v.created_at)}</td>
              <td className="mono small">{v.actor.slice(0, 14)}</td>
              <td className="small">{v.reason}{v.rolled_back_from ? <span className="t3"> (copy of v{v.rolled_back_from})</span> : null}</td>
              <td className="small mono t2" style={{ whiteSpace: "normal" }}>{diff.join(", ") || "—"}</td>
              <td>{can && !v.is_current ? <button className="btn sm" onClick={() => setRb(v)}>Roll back to this</button> : null}</td>
            </tr>
          );
        })}</tbody></table>
      {rb ? (
        <Dialog label="Roll back configuration" title={`Roll back to version ${rb.version}`} sub="Writes a new version that copies this one. Running jobs keep their configuration." onClose={() => setRb(null)}
          foot={<><button className="btn" onClick={() => setRb(null)}>Cancel</button><button className="btn primary" onClick={() => void doRollback()}>Roll back</button></>}>
          <label className="small t2">Reason<textarea className="field" rows={2} aria-label="Rollback reason" value={reason} onChange={(e) => setReason(e.target.value)} placeholder={`rollback to version ${rb.version}`} /></label>
          {err ? <ErrorBanner error={err} what="the rollback" /> : null}
        </Dialog>
      ) : null}
    </div>
  );
}

function flat(o: unknown, p = ""): Record<string, string> {
  if (o && typeof o === "object" && !Array.isArray(o)) {
    return Object.entries(o as Record<string, unknown>).reduce((acc, [k, v]) => ({ ...acc, ...flat(v, p ? `${p}.${k}` : k) }), {});
  }
  return { [p]: JSON.stringify(o) };
}

export function diffPaths(a: ProjectConfig, b: ProjectConfig): string[] {
  const fa = flat(a), fb = flat(b);
  return [...new Set([...Object.keys(fa), ...Object.keys(fb)])].filter((k) => fa[k] !== fb[k]).sort();
}
