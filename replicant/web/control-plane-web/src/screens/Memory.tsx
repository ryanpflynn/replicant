// /admin/memory (MEM, curation-ui.md subset): candidates queue, entries, entry detail (provenance, versions, jobs that
// used it), conflicts, quarantine and purge with a dry-run blast radius, export, analytics, admin-authored entries.
// Every call goes through control-plane-api /v1/admin/memory/** (memory:admin = replicant-admins, D-25), which
// memory-service re-checks; mutations send If-Match (state_seq) and an Idempotency-Key.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { TopBar } from "../components/Shell";
import { Dialog } from "../components/Dialog";
import { Empty, ErrorBanner, SkeletonRows } from "../components/bits";
import { ApiError, get, request } from "../lib/api";
import { hasCap, useMe } from "../lib/session";
import { dateTime } from "../lib/format";
import { Link, useRouter } from "../router";
import { Forbidden } from "./Admin";

const API = "/v1/admin/memory";
const TABS = [["candidates", "Candidates"], ["entries", "Entries"], ["conflicts", "Conflicts"], ["quarantine", "Quarantine & purge"],
  ["export", "Export"], ["analytics", "Analytics"]] as const;
const DEFAULT_REPO = "git.replicant.localhost/acme-payments/ledger-api";

export interface Entry {
  entry_id: string; tenant_id: string; memory_type: string; origin: string; state: string; state_seq: number;
  scope_level: string; repo?: string | null; subject_key?: string | null; current_version: number; confidence: number;
  evidence_count: number; injection_count: number; created_at: string; title?: string | null; statement?: string | null;
  rationale?: string | null; path_globs?: string[]; source_kind?: string | null; open_conflicts: number;
}
interface Detail extends Entry {
  versions: Array<{ version: number; statement?: string | null; scope_level: string; path_globs: string[]; change_kind: string; created_at: string; created_by: string; purged_at?: string | null }>;
  provenance: Array<{ provenance_id: number; source_kind: string; source_url?: string | null; source_excerpt?: string | null; author_principal_id?: string | null; author_repo_permission?: string | null; job_id?: string | null; pr_number?: number | null; observed_at: string; extraction_model?: string | null; withdrawn_reason?: string | null }>;
  transitions: Array<{ from_state?: string | null; to_state: string; at: string; actor_principal_id: string; reason_code: string; reason_text?: string | null }>;
  jobs: Array<{ job_id: string; run_id: string; role: string; node_key: string; created_at: string }>;
}
interface Conflict { conflict_id: string; tenant_id: string; entry_a: string; entry_b: string; statement_a: string; statement_b: string; state_a: string; state_b: string; kind: string; detected_at: string }

const idem = () => `ui-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

async function mpost<T>(path: string, body: unknown, ifMatch?: number): Promise<T> {
  const headers: Record<string, string> = { "Idempotency-Key": idem() };
  if (ifMatch !== undefined) headers["If-Match"] = String(ifMatch);
  return (await request<T>(`${API}/${path}`, { method: "POST", body: JSON.stringify(body), headers })).data;
}

const scopeLabel = (e: { scope_level: string; repo?: string | null; path_globs?: string[] }) =>
  e.scope_level === "path" ? `${(e.repo ?? "").split("/").slice(1).join("/")}:${(e.path_globs ?? []).join(", ")}`
    : e.scope_level === "repo" ? (e.repo ?? "").split("/").slice(1).join("/") : e.scope_level;

const SOURCE_WORDS: Record<string, string> = { gate_comment: "approver comment", pr_review_comment: "PR review comment",
  pr_review_body: "PR review", pr_issue_comment: "PR comment", review_finding: "reviewer finding (recurring)",
  qa_failure: "QA failure (recurring)", import: "repo scan", curator: "admin", agent_proposal: "agent proposal" };

export function Memory() {
  const me = useMe().data;
  const { loc, navigate } = useRouter();
  const tab = loc.search.get("tab") ?? "candidates";
  const sel = loc.search.get("entry");
  if (me && !hasCap(me, "memory:admin")) {
    return (
      <><TopBar><b>Memory</b></TopBar><main className="main">
        <Forbidden what="memory administration" perm="memory:admin">
          This page manages Replicant's long-term memory. Only members of the Okta group <span className="mono">replicant-admins</span> can open it, and that is checked on every request. Your account, {me.display_name}, is not in that group.
        </Forbidden>
      </main></>
    );
  }
  const go = (t: string, entry?: string | null) => navigate(`/admin/memory?tab=${t}${entry ? `&entry=${entry}` : ""}`);
  return (
    <>
      <TopBar><b>Memory</b><span className="sep">/</span><span>{TABS.find(([k]) => k === tab)?.[1]}</span><span className="adminbadge" style={{ marginLeft: 6 }}>replicant-admins</span></TopBar>
      <main className="main">
        <nav className="tabs" aria-label="Memory sections">
          {TABS.map(([k, t]) => <a key={k} href={`/admin/memory?tab=${k}`} className={tab === k ? "on" : ""} onClick={(e) => { e.preventDefault(); go(k); }}>{t}</a>)}
        </nav>
        <div className="mem">
          <div>
            {tab === "candidates" ? <EntryList state="candidate" onOpen={(id) => go(tab, id)} sel={sel} /> : null}
            {tab === "entries" ? <EntryList onOpen={(id) => go(tab, id)} sel={sel} /> : null}
            {tab === "conflicts" ? <Conflicts /> : null}
            {tab === "quarantine" ? <QuarantinePurge /> : null}
            {tab === "export" ? <Export /> : null}
            {tab === "analytics" ? <Analytics /> : null}
          </div>
          <aside className="side">{sel ? <EntryDetail id={sel} onClose={() => go(tab)} /> : <NewEntry />}</aside>
        </div>
      </main>
    </>
  );
}

function EntryList({ state, onOpen, sel }: { state?: string; onOpen: (id: string) => void; sel: string | null }) {
  const [f, setF] = useState({ state: state ?? "", memory_type: "", repo: "", q: "" });
  const qs = new URLSearchParams(Object.entries({ ...f, state: state ?? f.state }).filter(([, v]) => v) as [string, string][]);
  const q = useQuery({ queryKey: ["memory", state ?? "entries", qs.toString()], queryFn: () =>
    get<{ items: Entry[] }>(`${API}/${state ? "candidates" : "entries"}?${qs}`) });
  return (
    <div className="section">
      <div className="section-h"><h3>{state ? "Candidates waiting for review" : "Entries"}</h3>
        <span className="t3 small">{state ? "every learned entry waits for an admin; nothing activates on its own" : "filter by repo, type and state"}</span></div>
      <div className="memfilters">
        {!state ? <select className="field" aria-label="State" value={f.state} onChange={(e) => setF({ ...f, state: e.target.value })}>
          <option value="">any state</option>{["active", "candidate", "disabled", "deprecated", "quarantined", "rejected", "purged"].map((s) => <option key={s}>{s}</option>)}</select> : null}
        <select className="field" aria-label="Type" value={f.memory_type} onChange={(e) => setF({ ...f, memory_type: e.target.value })}>
          <option value="">any type</option>{["rule", "negative", "procedure", "preference"].map((s) => <option key={s}>{s}</option>)}</select>
        <input className="field" aria-label="Repo" placeholder="repo host/owner/name" value={f.repo} onChange={(e) => setF({ ...f, repo: e.target.value })} />
        <input className="field" aria-label="Search" placeholder="search text" value={f.q} onChange={(e) => setF({ ...f, q: e.target.value })} />
      </div>
      {q.isLoading ? <SkeletonRows cols={[30, 10, 20, 10]} rows={5} /> : q.error ? <ErrorBanner error={q.error} what="memory entries" retry={() => void q.refetch()} /> :
        !q.data?.items.length ? <Empty title={state ? "No candidates" : "No entries"}>{state ? "Feedback from gates, PR reviews, recurring reviewer and QA findings and repo scans lands here." : "Nothing matches the filter."}</Empty> :
        <table className="t" aria-label={state ? "Candidates" : "Entries"}>
          <thead><tr><th>Statement</th><th>Type</th><th>Scope</th><th>Source</th><th className="right">Evidence</th><th>State</th></tr></thead>
          <tbody>{q.data.items.map((e) => (
            <tr key={e.entry_id} className={"click" + (sel === e.entry_id ? " sel" : "")} onClick={() => onOpen(e.entry_id)}>
              <td><div>{e.title ? <b>{e.title}</b> : null}</div><div className="small">{e.statement ?? <i className="t3">purged</i>}</div>
                {e.open_conflicts ? <span className="pill warn small">conflict</span> : null}</td>
              <td>{e.memory_type}</td><td className="mono small">{scopeLabel(e)}</td>
              <td className="small">{SOURCE_WORDS[e.source_kind ?? ""] ?? e.source_kind ?? "—"}</td>
              <td className="right num">{e.evidence_count}</td><td>{e.state}</td>
            </tr>))}</tbody>
        </table>}
    </div>
  );
}

function EntryDetail({ id, onClose }: { id: string; onClose: () => void }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["memory", "entry", id], queryFn: () => get<Detail>(`${API}/entries/${id}`) });
  const [err, setErr] = useState<unknown>(null);
  const [edit, setEdit] = useState(false);
  const [reason, setReason] = useState("");
  const e = q.data;
  const act = async (verb: string, body: Record<string, unknown> = {}) => {
    if (!e) return;
    setErr(null);
    try {
      await mpost(`entries/${e.entry_id}:${verb}`, { reason: reason || undefined, ...body }, e.state_seq);
      await qc.invalidateQueries({ queryKey: ["memory"] });
      setEdit(false);
    } catch (x) { setErr(x); }
  };
  if (q.isLoading) return <div className="section"><SkeletonRows cols={[60]} rows={6} /></div>;
  if (q.error || !e) return <div className="section"><ErrorBanner error={q.error} what="the entry" /></div>;
  const verbs: Array<[string, string, string]> = ({
    candidate: [["approve", "Approve", "primary"], ["reject", "Reject", ""]],
    active: [["disable", "Disable", ""], ["deprecate", "Deprecate", ""], ["quarantine", "Quarantine", ""]],
    disabled: [["restore", "Restore", "primary"], ["deprecate", "Deprecate", ""]],
    deprecated: [["restore", "Restore", "primary"]], quarantined: [["restore", "Restore", "primary"]],
  } as Record<string, Array<[string, string, string]>>)[e.state] ?? [];
  return (
    <div className="section" aria-label="Entry detail">
      <div className="section-h"><h3>{e.title ?? e.memory_type}</h3><button className="btn sm ghost" onClick={onClose}>Close</button></div>
      <p>{e.statement ?? <i className="t3">purged (tombstone)</i>}</p>
      {e.rationale ? <p className="small t2">{e.rationale}</p> : null}
      <dl className="kv">
        <dt>State</dt><dd>{e.state} <span className="t3 small">state_seq {e.state_seq} · v{e.current_version}</span></dd>
        <dt>Scope</dt><dd className="mono small">{e.scope_level} · {scopeLabel(e)}</dd>
        <dt>Subject</dt><dd className="mono small">{e.subject_key ?? "—"}</dd>
        <dt>Origin</dt><dd>{e.origin} <span className="t3 small">tenant {e.tenant_id}</span></dd>
        <dt>Evidence</dt><dd className="num">{e.evidence_count} · confidence {e.confidence.toFixed(2)} · injected {e.injection_count}×</dd>
      </dl>
      {err ? <ErrorBanner error={err} what="the change" /> : null}
      {e.open_conflicts ? <p className="small warn">An open conflict blocks activation; resolve it under Conflicts.</p> : null}
      {verbs.length ? <>
        <input className="field" aria-label="Reason" placeholder="reason (recorded in the audit trail)" value={reason} onChange={(x) => setReason(x.target.value)} style={{ width: "100%", marginTop: 8 }} />
        <div className="row g1" style={{ marginTop: 8, flexWrap: "wrap" }}>
          {verbs.map(([v, label, cls]) => <button key={v} className={"btn sm " + cls} onClick={() => void act(v)}>{label}</button>)}
          {e.state === "candidate" || e.state === "active" ? <button className="btn sm" onClick={() => setEdit(true)}>{e.state === "candidate" ? "Edit and approve…" : "Edit…"}</button> : null}
        </div></> : null}
      {edit ? <EditDialog e={e} onClose={() => setEdit(false)} onSave={(b) => void act("edit", b)} /> : null}
      <h4 className="cfgh">Provenance</h4>
      {e.provenance.map((p) => (
        <div key={p.provenance_id} className="small" style={{ marginBottom: 8 }}>
          <div><b>{SOURCE_WORDS[p.source_kind] ?? p.source_kind}</b>{p.pr_number ? <> · PR #{p.pr_number}</> : null}
            {p.job_id ? <> · <Link to={`/jobs/${p.job_id}`} className="mono">{p.job_id.slice(0, 14)}…</Link></> : null}
            {p.source_url ? <> · <a href={p.source_url} target="_blank" rel="noreferrer noopener">source ↗</a></> : null}</div>
          {p.source_excerpt ? <blockquote className="t2">{p.source_excerpt}</blockquote> : null}
          <div className="t3">{p.author_principal_id ? <>author <span className="mono">{p.author_principal_id.slice(0, 12)}…</span> ({p.author_repo_permission ?? "?"}) · </> : null}
            {dateTime(p.observed_at)}{p.extraction_model ? <> · extracted by {p.extraction_model}</> : null}{p.withdrawn_reason ? <> · withdrawn ({p.withdrawn_reason})</> : null}</div>
        </div>))}
      <h4 className="cfgh">Versions</h4>
      {e.versions.map((v) => <div key={v.version} className="small"><b>v{v.version}</b> {v.change_kind} · {dateTime(v.created_at)} · <span className="t2">{v.statement ?? "purged"}</span></div>)}
      <h4 className="cfgh">History</h4>
      {e.transitions.map((t, i) => <div key={i} className="small t2">{dateTime(t.at)} · {t.from_state ?? "new"} → {t.to_state} · {t.reason_code}{t.reason_text ? ` (${t.reason_text})` : ""}</div>)}
      <h4 className="cfgh">Jobs that used it</h4>
      {e.jobs.length ? e.jobs.map((j) => <div key={j.run_id} className="small"><Link to={`/jobs/${j.job_id}`} className="mono">{j.job_id.slice(0, 16)}…</Link> <span className="t3">{j.node_key} ({j.role}) · {dateTime(j.created_at)}</span></div>)
        : <p className="small t3">Not injected into any run yet.</p>}
    </div>
  );
}

function EditDialog({ e, onClose, onSave }: { e: Detail; onClose: () => void; onSave: (b: Record<string, unknown>) => void }) {
  const [b, setB] = useState({ statement: e.statement ?? "", title: e.title ?? "", subject_key: e.subject_key ?? "",
    scope_level: e.scope_level, repo: e.repo ?? "", path_globs: (e.path_globs ?? []).join(", ") });
  const body = () => ({ statement: b.statement, title: b.title || null, subject_key: b.subject_key || null, scope_level: b.scope_level,
    repo: b.repo || null, path_globs: b.path_globs.split(",").map((x) => x.trim()).filter(Boolean), approve: e.state === "candidate" });
  return (
    <Dialog label="Edit entry" title={e.state === "candidate" ? "Edit and approve" : "Edit entry"} sub="Editing writes a new immutable version; jobs that pinned the old one keep it." onClose={onClose}
      foot={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={b.statement.trim().length < 10} onClick={() => onSave(body())}>{e.state === "candidate" ? "Save and approve" : "Save version"}</button></>}>
      <div className="cfgform">
        <Lbl t="Statement"><textarea className="field" aria-label="Statement" rows={3} value={b.statement} onChange={(x) => setB({ ...b, statement: x.target.value })} /></Lbl>
        <Lbl t="Title"><input className="field" aria-label="Title" value={b.title} onChange={(x) => setB({ ...b, title: x.target.value })} /></Lbl>
        <Lbl t="Subject key"><input className="field mono" aria-label="Subject key" value={b.subject_key} onChange={(x) => setB({ ...b, subject_key: x.target.value })} /></Lbl>
        <Lbl t="Scope"><select className="field" aria-label="Scope" value={b.scope_level} onChange={(x) => setB({ ...b, scope_level: x.target.value })}>{["tenant", "repo", "path"].map((s) => <option key={s}>{s}</option>)}</select></Lbl>
        {b.scope_level !== "tenant" ? <Lbl t="Repo"><input className="field mono" aria-label="Repo" value={b.repo} onChange={(x) => setB({ ...b, repo: x.target.value })} /></Lbl> : null}
        {b.scope_level === "path" ? <Lbl t="Path globs"><input className="field mono" aria-label="Path globs" value={b.path_globs} onChange={(x) => setB({ ...b, path_globs: x.target.value })} /></Lbl> : null}
      </div>
    </Dialog>
  );
}

const Lbl = ({ t, children }: { t: string; children: ReactNode }) => <label className="fld"><span className="t2 small">{t}</span>{children}</label>;

function NewEntry() {
  const qc = useQueryClient();
  const [b, setB] = useState({ tenant_id: "payments", memory_type: "rule", scope_level: "repo", repo: DEFAULT_REPO, path_globs: "", statement: "", title: "" });
  const [msg, setMsg] = useState<ReactNode>(null);
  const save = async () => {
    try {
      const e = await mpost<Entry>("entries", { ...b, path_globs: b.path_globs.split(",").map((x) => x.trim()).filter(Boolean), title: b.title || undefined });
      setMsg(<span className="ok">Created {e.entry_id.slice(0, 14)}… (active)</span>);
      setB({ ...b, statement: "", title: "" });
      await qc.invalidateQueries({ queryKey: ["memory"] });
    } catch (x) { setMsg(<ErrorBanner error={x} what="the entry" />); }
  };
  return (
    <div className="section" aria-label="New entry">
      <div className="section-h"><h3>Author an entry</h3><span className="t3 small">active at once, audited</span></div>
      <div className="cfgform">
        <Lbl t="Tenant"><input className="field" aria-label="Tenant" value={b.tenant_id} onChange={(x) => setB({ ...b, tenant_id: x.target.value })} /></Lbl>
        <Lbl t="Type"><select className="field" aria-label="New entry type" value={b.memory_type} onChange={(x) => setB({ ...b, memory_type: x.target.value })}>{["rule", "negative", "procedure", "preference"].map((s) => <option key={s}>{s}</option>)}</select></Lbl>
        <Lbl t="Scope"><select className="field" aria-label="New entry scope" value={b.scope_level} onChange={(x) => setB({ ...b, scope_level: x.target.value })}>{["tenant", "repo", "path"].map((s) => <option key={s}>{s}</option>)}</select></Lbl>
        {b.scope_level !== "tenant" ? <Lbl t="Repo"><input className="field mono" aria-label="New entry repo" value={b.repo} onChange={(x) => setB({ ...b, repo: x.target.value })} /></Lbl> : null}
        {b.scope_level === "path" ? <Lbl t="Path globs"><input className="field mono" aria-label="New entry globs" value={b.path_globs} onChange={(x) => setB({ ...b, path_globs: x.target.value })} /></Lbl> : null}
        <Lbl t="Title"><input className="field" aria-label="New entry title" value={b.title} onChange={(x) => setB({ ...b, title: x.target.value })} /></Lbl>
        <Lbl t="Statement"><textarea className="field" aria-label="New entry statement" rows={3} value={b.statement} onChange={(x) => setB({ ...b, statement: x.target.value })} /></Lbl>
      </div>
      <button className="btn primary" style={{ marginTop: 8 }} disabled={b.statement.trim().length < 10} onClick={() => void save()}>Create entry</button>
      {msg ? <div style={{ marginTop: 8 }}>{msg}</div> : null}
    </div>
  );
}

function Conflicts() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["memory", "conflicts"], queryFn: () => get<{ items: Conflict[] }>(`${API}/conflicts`) });
  const [err, setErr] = useState<unknown>(null);
  const resolve = async (c: Conflict, resolution: string) => {
    try { await mpost(`conflicts/${c.conflict_id}:resolve`, { resolution }); await qc.invalidateQueries({ queryKey: ["memory"] }); } catch (x) { setErr(x); }
  };
  return (
    <div className="section">
      <div className="section-h"><h3>Open conflicts</h3><span className="t3 small">same subject, different statements; both are held until you decide</span></div>
      {err ? <ErrorBanner error={err} what="the resolution" /> : null}
      {q.isLoading ? <SkeletonRows cols={[40, 40]} rows={3} /> : !q.data?.items.length ? <Empty title="No open conflicts">Conflicts are detected when two entries share a subject key.</Empty> :
        q.data.items.map((c) => (
          <div key={c.conflict_id} className="conflict" style={{ borderBottom: "1px solid var(--line, #ddd)", padding: "8px 0" }}>
            <div className="small"><b>A</b> ({c.state_a}) {c.statement_a}</div>
            <div className="small"><b>B</b> ({c.state_b}) {c.statement_b}</div>
            <div className="row g1" style={{ marginTop: 6 }}>
              {[["keep_a", "Keep A"], ["keep_b", "Keep B"], ["both_valid", "Both valid"], ["neither", "Neither"]].map(([r, t]) =>
                <button key={r} className="btn sm" onClick={() => void resolve(c, r)}>{t}</button>)}
            </div>
          </div>))}
    </div>
  );
}

interface Blast { dry_run: boolean; entries: string[]; count?: number; jobs: string[]; snapshots: string[]; quarantine_ids?: string[]; purge_ids?: string[] }

function QuarantinePurge() {
  const qc = useQueryClient();
  const [sel, setSel] = useState({ tenant_id: "payments", author_principal_id: "", source_kind: "", job_id: "" });
  const [ids, setIds] = useState("");
  const [qr, setQr] = useState<Blast | null>(null);
  const [pr, setPr] = useState<Blast | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [reason, setReason] = useState("");
  const selector = () => Object.fromEntries(Object.entries(sel).filter(([, v]) => v));
  const quarantine = async (dry: boolean) => {
    setErr(null);
    try { setQr(await mpost<Blast>("quarantines", { selector: selector(), dry_run: dry, reason: reason || "admin quarantine" })); if (!dry) await qc.invalidateQueries({ queryKey: ["memory"] }); } catch (x) { setErr(x); }
  };
  const purge = async (dry: boolean) => {
    setErr(null);
    try { setPr(await mpost<Blast>("purges", { entry_ids: ids.split(/[\s,]+/).filter(Boolean), dry_run: dry, reason_code: "curator", reason_text: reason || "admin purge" })); if (!dry) await qc.invalidateQueries({ queryKey: ["memory"] }); } catch (x) { setErr(x); }
  };
  return (
    <>
      {err ? <ErrorBanner error={err} what="the request" /> : null}
      <div className="section" aria-label="Quarantine">
        <div className="section-h"><h3>Quarantine by author or source</h3><span className="t3 small">bulk disable; restorable</span></div>
        <div className="cfgform">
          <Lbl t="Author principal"><input className="field mono" aria-label="Author principal" value={sel.author_principal_id} onChange={(x) => setSel({ ...sel, author_principal_id: x.target.value })} /></Lbl>
          <Lbl t="Source kind"><select className="field" aria-label="Source kind" value={sel.source_kind} onChange={(x) => setSel({ ...sel, source_kind: x.target.value })}><option value="">any</option>{Object.keys(SOURCE_WORDS).map((k) => <option key={k} value={k}>{SOURCE_WORDS[k]}</option>)}</select></Lbl>
          <Lbl t="Job"><input className="field mono" aria-label="Source job" value={sel.job_id} onChange={(x) => setSel({ ...sel, job_id: x.target.value })} /></Lbl>
          <Lbl t="Reason"><input className="field" aria-label="Quarantine reason" value={reason} onChange={(x) => setReason(x.target.value)} /></Lbl>
        </div>
        <div className="row g1" style={{ marginTop: 8 }}><button className="btn" onClick={() => void quarantine(true)}>Dry run</button>
          <button className="btn primary" disabled={!qr?.dry_run || !qr.entries.length} onClick={() => void quarantine(false)}>Quarantine {qr?.dry_run ? qr.entries.length : ""}</button></div>
        {qr ? <BlastView b={qr} /> : null}
      </div>
      <div className="section" aria-label="Purge">
        <div className="section-h"><h3>Purge</h3><span className="t3 small">erases content everywhere; a tombstone and the audit trail stay</span></div>
        <Lbl t="Entry IDs"><input className="field mono" aria-label="Entry IDs" value={ids} onChange={(x) => setIds(x.target.value)} placeholder="mem_… mem_…" style={{ width: "100%" }} /></Lbl>
        <div className="row g1" style={{ marginTop: 8 }}><button className="btn" onClick={() => void purge(true)}>Dry run</button>
          <button className="btn danger" disabled={!pr?.dry_run} onClick={() => void purge(false)}>Purge</button></div>
        {pr ? <BlastView b={pr} /> : null}
      </div>
    </>
  );
}

function BlastView({ b }: { b: Blast }) {
  return (
    <div className="codeblock" style={{ marginTop: 8 }} aria-label="Blast radius">{`${b.dry_run ? "dry run" : "done"}  entries ${b.entries.length}  jobs ${b.jobs.length}  snapshots ${b.snapshots.length}
${b.entries.slice(0, 20).join("\n")}${b.jobs.length ? `\njobs that used them:\n${b.jobs.slice(0, 20).join("\n")}` : ""}${b.purge_ids?.length ? `\npurge ${b.purge_ids.join(", ")}` : ""}${b.quarantine_ids?.length ? `\nquarantine ${b.quarantine_ids.join(", ")}` : ""}`}</div>
  );
}

function Export() {
  const [t, setT] = useState("payments");
  const [out, setOut] = useState<{ export_id: string; object_key: string; entries: Entry[] } | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const run = async () => {
    setErr(null);
    try {
      const d = await mpost<{ export_id: string; object_key: string; entries: Entry[] }>("exports", { tenant_id: t });
      setOut(d);
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([JSON.stringify(d, null, 2)], { type: "application/json" }));
      a.download = `${d.export_id}.json`;
      a.click();
    } catch (x) { setErr(x); }
  };
  return (
    <div className="section" aria-label="Export">
      <div className="section-h"><h3>Export</h3><span className="t3 small">JSON, purged entries excluded; a copy is kept in the memory bucket</span></div>
      <div className="row g1"><input className="field" aria-label="Export tenant" value={t} onChange={(x) => setT(x.target.value)} /><button className="btn primary" onClick={() => void run()}>Export JSON</button></div>
      {err ? <ErrorBanner error={err} what="the export" /> : null}
      {out ? <p className="small t2" style={{ marginTop: 8 }}>{out.export_id}: {out.entries.length} entries · <span className="mono">{out.object_key}</span></p> : null}
    </div>
  );
}

function Analytics() {
  const sum = useQuery({ queryKey: ["memory", "an", "summary"], queryFn: () => get<{ states: Record<string, number>; injections: { runs: number; jobs: number; avg_entries?: number }; outcomes: { jobs: number; avg_review_rounds?: number; avg_changes_requested?: number; first_pass_qa_rate?: number } }>(`${API}/analytics/summary`) });
  const ent = useQuery({ queryKey: ["memory", "an", "entries"], queryFn: () => get<{ rows: Array<Record<string, number | string | null>> }>(`${API}/analytics/entries`) });
  const fb = useQuery({ queryKey: ["memory", "an", "feedback"], queryFn: () => get<{ rows: Array<Record<string, number | string | null>> }>(`${API}/analytics/feedback`) });
  const n = (v: unknown, d = 2) => (typeof v === "number" ? v.toFixed(d) : "—");
  if (sum.error instanceof ApiError) return <ErrorBanner error={sum.error} what="analytics" />;
  return (
    <>
      <div className="section" aria-label="Summary">
        <div className="section-h"><h3>Summary</h3></div>
        <dl className="kv">
          <dt>Entries</dt><dd>{Object.entries(sum.data?.states ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "—"}</dd>
          <dt>Injections</dt><dd>{sum.data ? `${sum.data.injections.runs} runs in ${sum.data.injections.jobs} jobs, ${n(sum.data.injections.avg_entries, 1)} entries per run` : "—"}</dd>
          <dt>Outcomes</dt><dd>{sum.data ? `${sum.data.outcomes.jobs} jobs · review rounds ${n(sum.data.outcomes.avg_review_rounds)} · gate request-changes ${n(sum.data.outcomes.avg_changes_requested)} · first-pass QA ${n(sum.data.outcomes.first_pass_qa_rate)}` : "—"}</dd>
        </dl>
      </div>
      <div className="section" aria-label="Per entry">
        <div className="section-h"><h3>Jobs with and without each entry</h3><span className="t3 small">same repo; no holdouts in v0, so this is observational</span></div>
        {!ent.data?.rows.length ? <Empty title="No active entries">Approve entries to compare outcomes.</Empty> :
          <table className="t"><thead><tr><th>Entry</th><th className="right">Jobs with / without</th><th className="right">Request-changes with / without</th><th className="right">Review rounds with / without</th></tr></thead>
            <tbody>{ent.data.rows.map((r) => <tr key={String(r.entry_id)}><td className="small">{String(r.statement ?? "").slice(0, 90)}</td>
              <td className="right num">{r.jobs_with} / {r.jobs_without}</td><td className="right num">{n(r.changes_requested_with)} / {n(r.changes_requested_without)}</td>
              <td className="right num">{n(r.review_rounds_with)} / {n(r.review_rounds_without)}</td></tr>)}</tbody></table>}
      </div>
      <div className="section" aria-label="Repeated feedback">
        <div className="section-h"><h3>Repeated feedback</h3><span className="t3 small">feedback items that matched an existing entry</span></div>
        {!fb.data?.rows.length ? <Empty title="No feedback yet">Gate comments, PR reviews and findings are counted here.</Empty> :
          <table className="t"><thead><tr><th>Week</th><th>Source</th><th className="right">Items</th><th className="right">Repeated</th><th className="right">Rate</th><th className="right">New candidates</th><th className="right">Blocked</th></tr></thead>
            <tbody>{fb.data.rows.map((r, i) => <tr key={i}><td className="small">{String(r.week).slice(0, 10)}</td><td>{String(r.source)}</td><td className="right num">{r.items}</td>
              <td className="right num">{r.repeated}</td><td className="right num">{n(r.repeated_rate)}</td><td className="right num">{r.new_candidates}</td><td className="right num">{r.blocked}</td></tr>)}</tbody></table>}
      </div>
    </>
  );
}
