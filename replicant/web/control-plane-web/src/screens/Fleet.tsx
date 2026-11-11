// /admin/fleet (design §8c, onboarding-interfaces.md §10): what repos across the fleet contain (MCP servers, hooks,
// toolchains) and what makes readiness checks fail, ranked by impact on job outcomes, with admin decisions
// (approve, deny, map to a platform service) and the support requests project admins sent. fleet:admin =
// replicant-admins, the same rule as memory:admin; control-plane-api re-checks on every call.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { TopBar } from "../components/Shell";
import { Empty, ErrorBanner, SkeletonRows } from "../components/bits";
import { hasCap, useMe } from "../lib/session";
import { dateTime } from "../lib/format";
import { Link, useRouter } from "../router";
import { Forbidden } from "./Admin";
import {
  classBadge, decideFleetItem, decisionWord, getFleetItem, impactPhrases, KINDS, kindWord, listFleetItems, reachPhrase, rescanFleet,
  type FleetItem,
} from "../lib/onboarding";

const SORTS = [["impact", "Impact"], ["repos", "Most repos"], ["recent", "Most recent"]] as const;

export function Fleet() {
  const me = useMe().data;
  const { loc, navigate } = useRouter();
  const tab = KINDS.find(([k]) => k === loc.search.get("tab"))?.[0] ?? "mcp_server";
  const sel = loc.search.get("item");
  const requested = loc.search.get("requests") === "1";
  if (me && !hasCap(me, "fleet:admin")) {
    return (
      <><TopBar><b>Fleet</b></TopBar><main className="main">
        <Forbidden what="fleet administration" perm="fleet:admin">
          This page decides which repo tools (MCP servers, hooks and toolchains) Replicant allows across every team. Only members of the Okta group <span className="mono">replicant-admins</span> can open it, and that is checked on every request. Your account, {me.display_name}, is not in that group.
        </Forbidden>
      </main></>
    );
  }
  const go = (t: string, o: { item?: string | null; requests?: boolean } = {}) => {
    const sp = new URLSearchParams({ tab: t });
    if (o.requests ?? requested) sp.set("requests", "1");
    if (o.item) sp.set("item", o.item);
    navigate(`/admin/fleet?${sp}`);
  };
  return (
    <>
      <TopBar><b>Fleet</b><span className="sep">/</span><span>{KINDS.find(([k]) => k === tab)?.[1]}</span><span className="adminbadge" style={{ marginLeft: 6 }}>replicant-admins</span></TopBar>
      <main className="main">
        <nav className="tabs" aria-label="Fleet sections">
          {KINDS.map(([k, t]) => <a key={k} href={`/admin/fleet?tab=${k}`} className={tab === k ? "on" : ""} onClick={(e) => { e.preventDefault(); go(k, { item: null }); }}>{t}</a>)}
        </nav>
        <div className="mem fleet">
          <div><ItemList kind={tab} requested={requested} sel={sel} onOpen={(id) => go(tab, { item: id })} onRequested={(v) => go(tab, { item: sel, requests: v })} /></div>
          <aside className="side">{sel ? <ItemDetail id={sel} onClose={() => go(tab, { item: null })} /> : <FleetHelp />}</aside>
        </div>
      </main>
    </>
  );
}

function ItemList({ kind, requested, sel, onOpen, onRequested }: { kind: string; requested: boolean; sel: string | null; onOpen: (id: string) => void; onRequested: (v: boolean) => void }) {
  const [sort, setSort] = useState<"impact" | "repos" | "recent">("impact");
  const [cls, setCls] = useState("");
  const q = useQuery({ queryKey: ["fleet", kind, sort, cls], queryFn: () => listFleetItems({ kind, sort, class: cls || undefined, limit: 100 }) });
  const items = (q.data?.items ?? []).filter((it) => !requested || (it.requests ?? 0) > 0);
  return (
    <div className="section">
      <div className="section-h"><h3>{kindWord(kind)}s across all repos</h3>
        <span className="t3 small">ranked by how much they change job outcomes</span>
        <span className="aside"><RescanButton /></span></div>
      <div className="memfilters">
        <select className="field" aria-label="Sort" value={sort} onChange={(e) => setSort(e.target.value as typeof sort)}>{SORTS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select>
        <input className="field" aria-label="Class" placeholder="class (e.g. needs_credentials)" value={cls} onChange={(e) => setCls(e.target.value.trim())} />
        <label className="row g1 small"><input type="checkbox" aria-label="Requests" checked={requested} onChange={(e) => onRequested(e.target.checked)} /> Only items teams asked about</label>
      </div>
      {q.isLoading ? <table className="t"><tbody><SkeletonRows cols={[30, 10, 20, 10]} rows={5} /></tbody></table>
        : q.error ? <ErrorBanner error={q.error} what="fleet items" retry={() => void q.refetch()} />
        : !items.length ? <Empty title="Nothing here">{requested ? "No team has asked about one of these." : "Nothing of this kind has been seen in any repo yet. Repos are scanned when registered and on every push to the default branch."}</Empty>
        : <table className="t" aria-label="Fleet items">
          <thead><tr><th>Item</th><th>Class</th><th>Decision</th><th className="right">Repos</th><th className="right">Jobs</th><th className="right">Impact</th></tr></thead>
          <tbody>{items.map((it) => <Row key={it.id} it={it} sel={sel === it.id} onOpen={() => onOpen(it.id)} />)}</tbody>
        </table>}
      {q.data?.next ? <p className="small t3">Showing the first {q.data.items.length}; narrow by class to see more.</p> : null}
    </div>
  );
}

function Row({ it, sel, onOpen }: { it: FleetItem; sel: boolean; onOpen: () => void }) {
  const cb = classBadge(it.class);
  const phrases = impactPhrases(it.impact?.delta);
  return (
    <tr className={"click" + (sel ? " sel" : "")} onClick={onOpen}>
      <td><div className="mono small">{it.identity}</div>
        <div className="small" aria-label="Impact explained">{phrases.length ? phrases.slice(0, 2).map((p) => <div key={p.text} className={p.bad ? "attn-t" : "t2"}>{p.text}</div>) : <span className="t3">No clear effect on outcomes yet</span>}</div>
        {it.requests ? <span className="pill small info">{it.requests} request{it.requests === 1 ? "" : "s"}</span> : null}</td>
      <td><span className={`pill small ${cb.family}`}>{cb.label}</span></td>
      <td className="small">{decisionWord(it.decision)}{it.effective ? <div className="t3">{it.effective.replace(/_/g, " ")}</div> : null}</td>
      <td className="right num">{it.repos}</td><td className="right num">{it.jobs}</td>
      <td className="right num">{it.impact ? it.impact.score.toFixed(1) : "—"}</td>
    </tr>
  );
}

function RescanButton() {
  const [res, setRes] = useState<{ ok?: boolean; error?: unknown } | null>(null);
  const run = async () => {
    setRes(null);
    try { await rescanFleet(); setRes({ ok: true }); } catch (e) { setRes({ error: e }); }
  };
  return (
    <span className="row g1">
      <button className="btn sm" onClick={() => void run()}>Rescan all</button>
      {res?.ok ? <span className="small t2">Rescan started; results appear as each repo finishes.</span> : null}
      {res?.error ? <ErrorBanner error={res.error} what="the rescan" /> : null}
    </span>
  );
}

function FleetHelp() {
  return (
    <div className="section">
      <div className="section-h"><h3>How this works</h3></div>
      <p className="small t2">Replicant scans every registered repo for its Claude Code setup and toolchains, sorts what it finds into classes and gives each a safe default. MCP servers are skipped unless allowed; hooks that need the network or missing tools are flagged.</p>
      <p className="small t2">Impact compares jobs that had an item with similar jobs (same repos and workflow types) that didn't. A high score means fixing or allowing it is likely to help many jobs.</p>
      <p className="small t3">Select an item to see its repos, recent jobs, decisions and requests.</p>
    </div>
  );
}

function ItemDetail({ id, onClose }: { id: string; onClose: () => void }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["fleet", "item", id], queryFn: () => getFleetItem(id) });
  const [reason, setReason] = useState("");
  const [mapTo, setMapTo] = useState("");
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const d = q.data;
  const decide = async (decision: "approve" | "deny" | "map") => {
    setBusy(true); setErr(null);
    try {
      await decideFleetItem(id, decision, reason.trim(), decision === "map" ? mapTo.trim() : undefined);
      setReason(""); setMapTo("");
      await qc.invalidateQueries({ queryKey: ["fleet"] });
    } catch (e) { setErr(e); } finally { setBusy(false); }
  };
  if (q.isLoading) return <div className="section"><table className="t"><tbody><SkeletonRows cols={[60]} rows={6} /></tbody></table></div>;
  if (q.error || !d) return <div className="section"><ErrorBanner error={q.error} what="the item" /></div>;
  const cb = classBadge(d.class);
  const phrases = impactPhrases(d.impact?.delta);
  return (
    <div className="section" aria-label="Item detail">
      <div className="section-h"><h3>{kindWord(d.kind)}</h3><button className="btn sm ghost" onClick={onClose}>Close</button></div>
      <p className="mono small" style={{ overflowWrap: "anywhere" }}>{d.identity}</p>
      <dl className="kv">
        <dt>Class</dt><dd><span className={`pill small ${cb.family}`}>{cb.label}</span> <span className="t3 small mono">{d.class}</span></dd>
        <dt>Decision</dt><dd aria-label="Decision">{decisionWord(d.decision)}{d.effective ? <span className="t3 small"> · in effect: {d.effective.replace(/_/g, " ")}</span> : null}</dd>
        <dt>Reach</dt><dd>{reachPhrase(d.jobs, d.repos.length)}</dd>
        <dt>Impact</dt><dd aria-label="Impact">{d.impact ? <>score {d.impact.score.toFixed(1)}
          {d.impact.jobs_with != null ? <span className="t3 small"> · {d.impact.jobs_with} jobs with it, {d.impact.jobs_without ?? 0} similar jobs without</span> : null}
          {phrases.length ? <ul className="plain small">{phrases.map((p) => <li key={p.text} className={p.bad ? "attn-t" : "t2"}>{p.text}</li>)}</ul> : <div className="t3 small">No clear effect on outcomes yet.</div>}</> : <span className="t3">not enough jobs yet</span>}</dd>
        <dt>Seen</dt><dd className="small">{d.first_seen ? dateTime(d.first_seen) : "—"} → {d.last_seen ? dateTime(d.last_seen) : "—"}</dd>
      </dl>
      <h4 className="cfgh">Decide</h4>
      <input className="field" aria-label="Decision reason" placeholder="reason (required, recorded in the audit trail)" value={reason} onChange={(e) => setReason(e.target.value)} style={{ width: "100%" }} />
      <div className="row g1" style={{ marginTop: 8, flexWrap: "wrap" }}>
        <button className="btn sm primary" disabled={busy || !reason.trim()} onClick={() => void decide("approve")}>Approve</button>
        <button className="btn sm" disabled={busy || !reason.trim()} onClick={() => void decide("deny")}>Deny</button>
        <input className="field" aria-label="Map to platform service" placeholder="platform service" value={mapTo} onChange={(e) => setMapTo(e.target.value)} style={{ width: 160 }} />
        <button className="btn sm" disabled={busy || !reason.trim() || !mapTo.trim()} onClick={() => void decide("map")}>Map</button>
      </div>
      {err ? <ErrorBanner error={err} what="the decision" /> : null}
      <h4 className="cfgh">Repos</h4>
      {d.repos.length ? <ul className="plain small">{d.repos.map((r) => <li key={r.repo_id}><span className="mono">{r.full_name}</span>{r.project_id ? <> · <Link to={`/projects/${r.project_id}/settings/readiness`}>readiness</Link></> : null}</li>)}</ul> : <p className="small t3">None.</p>}
      <h4 className="cfgh">Recent jobs</h4>
      {d.recent_jobs?.length ? d.recent_jobs.map((j) => <div key={j.job_id} className="small"><Link to={`/jobs/${j.job_id}`} className="mono">{j.job_id.slice(0, 16)}…</Link> <span className="t3">{j.status}{j.outcome ? ` · ${j.outcome}` : ""} · {dateTime(j.at)}</span></div>)
        : <p className="small t3">No jobs yet.</p>}
      <h4 className="cfgh">Decisions</h4>
      {d.decisions?.length ? d.decisions.map((x, i) => <div key={i} className="small t2">{x.at ? dateTime(x.at) + " · " : ""}{decisionWord(x.decision, x.map_to)}{x.actor ? ` · ${x.actor}` : ""}{x.reason ? ` (${x.reason})` : ""}</div>)
        : <p className="small t3">No decisions yet; the class default applies.</p>}
      <h4 className="cfgh">Requests</h4>
      {d.requests?.length ? d.requests.map((x, i) => <div key={i} className="small"><span className="t2">{x.reason}</span> <span className="t3">{x.project_id ? `· ${x.project_id} ` : ""}{x.actor ? `· ${x.actor} ` : ""}{x.at ? `· ${dateTime(x.at)}` : ""}</span></div>)
        : <p className="small t3">No team has asked about this.</p>}
    </div>
  );
}
