import { MemoryUsed } from "../components/MemoryUsed";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { TopBar } from "../components/Shell";
import { Glyph } from "../components/Glyph";
import { Avatar, Banner, Empty, ErrorBanner, Ext, Id, Kbd, Pill, Quote, Risk, StaleBanner } from "../components/bits";
import { DiffSection, HandoffSection, PlanSection, QASection, ReviewSection, RulesSection } from "../components/Packet";
import { CancelDialog, SteerDialog, useControl } from "./JobActions";
import { activityHref, GraphTab } from "./JobGraph";
import { ApiError, get } from "../lib/api";
import { personName, hasCap, useDirectory, useJobView, useMe, useSession } from "../lib/session";
import { age, clock, dateTime, duration, humanize, parseTs, timeLeft, usd } from "../lib/format";
import { gateStatus, jobStatus, OUTCOME_WORDS, runStatus, TERMINAL, type Family } from "../lib/status";
import { browserGitUrl, grafanaContainersUrl, grafanaJobUrl, repoName, repoUrl, temporalWorkflowUrl } from "../lib/config";
import { Link } from "../router";
import { stageStates } from "../lib/simple";
import { useJobDetail, useTimeline } from "../lib/jobs";
import { JobSimple, ViewToggle } from "./JobSimple";
import type { GateDetail, GateView, JobDetail as JobDetailT, JobSnapshot, Me, RunView, TimelineEvent } from "../lib/types";

interface Row { key: string; at: string; glyph: string; family: Family; body: ReactNode; right?: ReactNode; cls?: string }

function timelineRows(tl: TimelineEvent[], s: JobSnapshot, runs: RunView[], dir: Map<string, string>): Row[] {
  const rows: Row[] = [];
  const byRun = new Map<string, TimelineEvent[]>();
  const completed = new Set(tl.filter((e) => e.type === "node.completed").map((e) => e.refs?.node_key));
  const gates = new Map((s.gates ?? []).map((g) => [g.gate_id, g]));
  for (const e of tl) {
    if (e.type === "run.status_changed" && e.refs?.run_id) {
      const l = byRun.get(e.refs.run_id) ?? [];
      l.push(e);
      byRun.set(e.refs.run_id, l);
    }
  }
  const runRowDone = new Set<string>();
  for (const e of tl) {
    const k = `e${e.seq}`;
    const n = e.refs?.node_key;
    if (e.type === "run.status_changed" && e.refs?.run_id) {
      const id = e.refs.run_id;
      const evs = byRun.get(id)!;
      if (e !== evs[evs.length - 1] || runRowDone.has(id)) continue;
      runRowDone.add(id);
      const r = runs.find((x) => x.run_id === id);
      const last = evs[evs.length - 1].summary.split(" ").pop() ?? "";
      const st = runStatus(last);
      const phases: string[] = [];
      for (let i = 0; i < evs.length - 1; i++) {
        const name = evs[i].summary.split(" ").pop() ?? "";
        if (name === "created") continue;
        const ms = (parseTs(evs[i + 1].at) ?? 0) - (parseTs(evs[i].at) ?? 0);
        phases.push(`${name} ${duration(ms)}`);
      }
      const total = (parseTs(evs[evs.length - 1].at) ?? 0) - (parseTs(evs[0].at) ?? 0);
      rows.push({ key: k, at: e.at, glyph: st.glyph, family: st.family,
        body: <>{r?.role ?? "run"} {last === "Running" ? "running" : last.toLowerCase()} <span className="t3">· {duration(total)}{phases.length ? ` · ${phases.join(" · ")}` : ""}</span>
          <div className="runrow"><Id id={id} full /><span>{r ? `${r.node_key}.c${r.cycle ?? 0}.n${r.ordinal ?? 1}` : ""}</span>{r?.isolation ? <span>{r.isolation}</span> : null}</div></> });
      continue;
    }
    if (e.type === "node.started") {
      if (completed.has(n)) continue;
      rows.push({ key: k, at: e.at, glyph: s.status === "waiting_human" && /gate/.test(n ?? "") ? "diamond" : "ring-spin",
        family: s.status === "waiting_human" && /gate/.test(n ?? "") ? "attention" : TERMINAL.has(s.status) ? "neutral" : "info",
        body: <><b>{n}</b> started</>, right: TERMINAL.has(s.status) ? "" : "now" });
      continue;
    }
    if (e.type === "node.completed") {
      const res = e.summary.split(": ").slice(1).join(": ");
      if (n?.startsWith("risk_")) {
        rows.push({ key: k, at: e.at, glyph: "triangle", family: res === "high" || res === "critical" ? "danger" : "neutral",
          body: <>Risk classified <span className={`risk ${res}`}>{res}</span> <span className="t3">· {n === "risk_plan" ? "on the plan" : "on the diff"}</span>
            {n === "risk_diff" && s.risk?.rules_fired?.length ? <div className="runrow">{s.risk.rules_fired.map((r) => <span className="mono" key={r}>{r}</span>)}</div> : null}</> });
        continue;
      }
      const bad = /fail|error|blocked/.test(res);
      rows.push({ key: k, at: e.at, glyph: bad ? "cross" : res === "not_required" ? "dot-hollow" : "check", family: bad ? "danger" : "neutral",
        body: <><b>{n}</b> <span className="t3">· {res.replace(/_/g, " ")}</span></> });
      continue;
    }
    if (e.type === "gate.opened") {
      const g = gates.get(e.refs?.gate_id ?? "");
      rows.push({ key: k, at: e.at, glyph: "diamond", family: "attention",
        body: <>{humanize(g?.kind ?? "gate")} gate opened <span className="t3">· eligible {g?.eligible?.okta_groups?.join(", ") ?? "approvers"}{g?.expires_at ? ` · expires ${dateTime(g.expires_at)}` : ""}</span>
          {g ? <> · <Link to={`/approvals/${g.gate_id}`}>open approval</Link></> : null}</>,
        right: <span className="chan">{(g?.channels ?? []).map((c) => <i key={c}>{c.toUpperCase()}</i>)}</span> });
      continue;
    }
    if (e.type === "gate.decided") {
      const g = gates.get(e.refs?.gate_id ?? "");
      const d = g?.decision;
      const word = d?.decision === "approve" ? "approved" : d?.decision === "reject" ? "rejected" : d?.decision === "request_changes" ? "sent back with changes requested" : (d?.decision ?? "decided");
      rows.push({ key: k, at: e.at, glyph: d?.decision === "approve" ? "check" : d?.decision === "reject" ? "cross" : "return",
        family: d?.decision === "approve" ? "success" : d?.decision === "reject" ? "danger" : "special",
        body: <>{humanize(g?.kind ?? "gate")} {word} by <b>{personName(dir, d?.principal_id)}</b> in the {d?.channel === "ui" ? "UI" : (d?.channel ?? "").toUpperCase()}
          {g?.opened_at && d?.decided_at ? <span className="t3"> · {duration((parseTs(d.decided_at) ?? 0) - (parseTs(g.opened_at) ?? 0))} after opening</span> : null}
          {d?.comment ? <Quote label="approver comment">{d.comment}</Quote> : null}</>,
        right: d ? <span className="mono small">{d.decision_id.slice(0, 11)}…</span> : undefined });
      continue;
    }
    if (e.type === "pr.opened") {
      const pr = s.pull_request;
      rows.push({ key: k, at: e.at, glyph: "ring-half", family: "neutral",
        body: <>{e.summary}{pr?.url ? <> · <Ext href={browserGitUrl(pr.url)} className="mono">{repoName(pr.repo)}#{pr.number} ↗</Ext></> : null}</> });
      continue;
    }
    if (e.type === "job.terminal") {
      const st = jobStatus(s.status);
      rows.push({ key: k, at: e.at, glyph: st.glyph, family: st.family, cls: "phase",
        body: <>Job {st.label.toLowerCase()}{s.terminal?.outcome ? <span className="t3"> · {OUTCOME_WORDS[s.terminal.outcome] ?? s.terminal.outcome.replace(/_/g, " ")}</span> : null}</> });
      continue;
    }
    if (e.type === "job.routed") {
      rows.push({ key: k, at: e.at, glyph: "dot-hollow", family: "neutral",
        body: <>Job created and {e.summary} <span className="t3">· bundle <span className="mono">{s.release_bundle_id?.slice(0, 13)}</span></span></> });
      continue;
    }
    rows.push({ key: k, at: e.at, glyph: e.type.startsWith("pr.") ? "ring-half" : "plain", family: "neutral",
      body: <span className="t2">{e.summary}</span>, right: <span className="mono small t3">{e.type}</span> });
  }
  return rows.reverse();
}

function canControl(me: Me | undefined, s: JobSnapshot): boolean {
  if (!me) return false;
  const roles = me.tenants.find((t) => t.tenant_id === s.tenant_id)?.roles ?? [];
  if (roles.includes("tenant_admin")) return true;
  return me.principal_id === s.requester && roles.some((r) => ["engineer", "approver", "tenant_admin"].includes(r));
}

function useGate(gateId?: string) {
  const { user } = useSession();
  return useQuery({ queryKey: ["gate", user, gateId], enabled: !!gateId, queryFn: () => get<GateDetail>(`/v1/gates/${gateId}`) });
}

function PacketTab({ gate, which }: { gate?: GateView; which: "plan" | "diff" | "qa" }) {
  const q = useGate(gate?.gate_id);
  if (!gate) return <Empty>The plan, diff and QA report appear here once the job reaches its first approval gate (they are read from the gate's packet).</Empty>;
  if (q.isError) return <ErrorBanner error={q.error} what="the packet" retry={() => void q.refetch()} />;
  if (!q.data) return <div className="section"><div className="skel-line w40" /><div className="skel-line w70" /></div>;
  const p = q.data.packet;
  const note = <p className="small t3 packnote">From <Link to={`/approvals/${gate.gate_id}`} className="mono">{gate.gate_id}</Link> packet r{p.packet_revision}{gate.state !== "open" ? <> · approved digest <span className="mono">{p.presented_digest.slice(0, 15)}…</span></> : null}</p>;
  if (which === "plan") return <>{note}<PlanSection packet={p} /><HandoffSection packet={p} /></>;
  if (which === "diff") return <>{note}<DiffSection packet={p} gateId={gate.gate_id} /></>;
  return <>{note}<QASection packet={p} /><ReviewSection packet={p} /><RulesSection packet={p} /></>;
}

function RunsTab({ runs, jobId }: { runs: RunView[]; jobId: string }) {
  if (!runs.length) return <Empty>No runs yet.</Empty>;
  return (
    <table className="t" aria-label="Runs">
      <thead><tr><th>Run</th><th>Node</th><th>Role</th><th>Phase</th><th className="p3">Outcome</th><th className="p2">Isolation</th><th>Started</th><th className="right">Duration</th><th className="right p3">Cost</th></tr></thead>
      <tbody>
        {runs.map((r) => {
          const st = runStatus(r.phase);
          const end = parseTs(r.finished_at) ?? (st.family === "info" ? Date.now() : null);
          return (
            <tr key={r.run_id}>
              <td><Id id={r.run_id} to={activityHref(jobId, r.node_key, r.run_id)} /></td>
              <td className="mono small">{r.node_key}.c{r.cycle ?? 0}.n{r.ordinal ?? 1}</td>
              <td>{r.role}</td>
              <td><Pill s={st} /></td>
              <td className="p3 t2">{r.result_outcome ?? r.qa_verdict ?? (r.phase === "Succeeded" ? "completed" : "—")}{r.reason ? <span className="t3 mono small"> {r.reason}</span> : null}</td>
              <td className="p2 mono small t2">{r.isolation ?? "—"}</td>
              <td className="t2 mono small">{clock(r.started_at)}</td>
              <td className="right num">{r.started_at && end ? duration(end - (parseTs(r.started_at) ?? end)) : "—"}</td>
              <td className="right num p3 t2">{usd(r.cost_usd ?? 0)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function GatesTab({ gates, dir }: { gates: GateView[]; dir: Map<string, string> }) {
  if (!gates.length) return <Empty>No gate has opened for this job yet.</Empty>;
  return (
    <table className="t" aria-label="Gates">
      <thead><tr><th>Gate</th><th>Kind</th><th>State</th><th>Opened</th><th>Decided by</th><th className="p2">Channel</th></tr></thead>
      <tbody>
        {gates.map((g) => (
          <tr key={g.gate_id}>
            <td><Id id={g.gate_id} to={`/approvals/${g.gate_id}`} /></td>
            <td>{humanize(g.kind)}</td>
            <td><Pill s={gateStatus(g.state)} /></td>
            <td className="t2">{dateTime(g.opened_at)}</td>
            <td>{g.decision ? <>{personName(dir, g.decision.principal_id)} <span className="t3">· {dateTime(g.decision.decided_at)}</span></> : g.state === "open" ? <span className="t3">{timeLeft(g.expires_at)}</span> : "—"}</td>
            <td className="p2 mono small">{g.decision?.channel ?? (g.channels ?? []).join(", ")}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const TABS = ["timeline", "graph", "runs", "gates", "plan", "diff", "qa"] as const;
type Tab = (typeof TABS)[number];

/** WEB-UIPREF: /jobs/:id follows the user's job view (Simple unless they chose Detailed); a sub-page link
 * (/jobs/:id/graph, /runs, ...) always opens the Detailed view it names. */
export function JobDetail({ jobId, tab }: { jobId: string; tab?: string }) {
  const { view } = useJobView();
  if (!tab && view === "simple") return <JobSimple jobId={jobId} />;
  return <JobDetailed jobId={jobId} tab={tab} />;
}

function JobDetailed({ jobId, tab: tabParam }: { jobId: string; tab?: string }) {
  const { user } = useSession();
  const me = useMe().data;
  const dir = useDirectory();
  const tab: Tab = (TABS as readonly string[]).includes(tabParam ?? "") ? (tabParam as Tab) : "timeline";
  const job = useJobDetail(jobId);
  const tl = useTimeline(jobId, job.isSuccess);
  const runsQ = useQuery({ queryKey: ["runs", user, jobId], refetchInterval: 4000, enabled: job.isSuccess,
    queryFn: () => get<{ items: RunView[] }>(`/v1/jobs/${jobId}/runs`) });
  const [dialog, setDialog] = useState<null | "cancel" | "steer" | "pause">(null);
  const ctl = useControl(jobId);
  const s = job.data?.snapshot;
  const runs = runsQ.data?.items ?? s?.runs ?? [];
  const control = s ? canControl(me, s) && !TERMINAL.has(s.status) : false;

  useEffect(() => {
    if (!control) return;
    const on = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t.closest("input, textarea") || e.metaKey || e.ctrlKey || e.altKey || dialog) return;
      if (e.key === "s") { e.preventDefault(); setDialog("steer"); }
      else if (e.key === "p") { e.preventDefault(); setDialog("pause"); }
      else if (e.key === "C") { e.preventDefault(); setDialog("cancel"); }
    };
    addEventListener("keydown", on);
    return () => removeEventListener("keydown", on);
  }, [control, dialog]);

  const rows = useMemo(() => (s && tl.data ? timelineRows(tl.data, s, runs, dir) : []), [s, tl.data, runs, dir]);
  const track = useMemo(() => (s && tl.data ? stageStates(tl.data, s) : []), [s, tl.data]);

  if (job.isError) {
    const nf = job.error instanceof ApiError && job.error.status === 404;
    return (
      <>
        <TopBar><Link to="/jobs">Jobs</Link><span className="sep">/</span><b className="id mono">{jobId}</b></TopBar>
        <main className="main">{nf ? <Empty title={`No job ${jobId} that you can see`}>It may not exist, or it belongs to a tenant or repository you can't read. <Link to="/jobs">Back to jobs</Link></Empty>
          : <ErrorBanner error={job.error} what="this job" retry={() => void job.refetch()} />}</main>
      </>
    );
  }

  const crumbs = (
    <>
      <Link to="/jobs">Jobs</Link><span className="sep">/</span><span>{s?.tenant_id ?? "…"}</span><span className="sep">/</span>
      <b><Id id={jobId} full /></b>
    </>
  );
  if (!s || !job.data) {
    return (<><TopBar>{crumbs}</TopBar><main className="main" aria-busy="true"><section className="jobhead"><div className="skel-line w70 tall" /><div className="skel-line w40" /></section></main></>);
  }
  const d = job.data;
  const st = jobStatus(s.status);
  const repo = s.targets?.[0];
  const pr = s.pull_request;
  const issueUrl = browserGitUrl(d.source?.url);
  const issueNo = issueUrl?.match(/\/issues\/(\d+)/)?.[1];
  const created = tl.data?.[0]?.at;
  const end = s.terminal?.at ?? null;
  const elapsed = created ? (parseTs(end) ?? Date.now()) - (parseTs(created) ?? 0) : null;
  const openGate = (s.gates ?? []).find((g) => g.state === "open");
  const lastGate = (s.gates ?? [])[(s.gates ?? []).length - 1];
  const live = TERMINAL.has(s.status) ? undefined : d.live_run;
  const liveRun = live ? runs.find((r) => r.run_id === live.run_id) : undefined;

  const tabLink = (t: Tab, label: string, n?: number | string) => (
    <Link key={t} to={`/jobs/${jobId}${t === "timeline" ? "" : "/" + t}`} className={tab === t ? "on" : ""} aria-current={tab === t ? "page" : undefined}>
      {label}{n != null ? <span className="n">{n}</span> : null}
    </Link>
  );

  return (
    <>
      <TopBar>{crumbs}</TopBar>
      <main className="main">
        <section className="jobhead">
          <div className="row" style={{ alignItems: "flex-start" }}>
            <div className="grow">
              <h1>{s.title}</h1>
              <div className="meta">
                <Pill s={st} />
                <span>{s.phase} <span className="t3">· cycle {s.cycle}</span></span>
                <span className="sep" />
                {repo ? <Ext href={repoUrl(repo.repo)} className="mono">{repoName(repo.repo)}</Ext> : null}
                {issueNo ? <Ext href={issueUrl} className="mono">#{issueNo}</Ext> : null}
                <span className="sep" />
                <span className="row g1"><Avatar name={personName(dir, s.requester)} sm />{personName(dir, s.requester)}</span>
                <span className="t3">via {d.source?.channel === "github" ? "Gitea issue label" : d.source?.channel ?? "API"} <span className="tag mono">replicant</span></span>
                <span className="sep" />
                <span className="t3">{s.workflow_type}{created ? ` · started ${dateTime(created)}` : ""}{elapsed != null ? ` · ${duration(elapsed)}` : ""}</span>
              </div>
            </div>
            <div className="acts">
              <ViewToggle jobId={jobId} />
            </div>
            {control ? (
              <div className="acts">
                <button className="btn" onClick={() => setDialog("steer")}><Glyph g="plain" />Steer<Kbd>S</Kbd></button>
                {s.paused || s.status === "paused"
                  ? <button className="btn" disabled={!!ctl.busy} onClick={() => void ctl.run("resume")}>{ctl.busy === "resume" ? "Recording…" : <>Resume<Kbd>P</Kbd></>}</button>
                  : <button className="btn" disabled={!!ctl.busy} onClick={() => setDialog("pause")}>{ctl.busy === "pause" ? "Recording…" : <>Pause<Kbd>P</Kbd></>}</button>}
                <button className="btn ghost danger" onClick={() => setDialog("cancel")} data-testid="cancel-job">Cancel</button>
              </div>
            ) : null}
          </div>
          {ctl.result ? <p className={"small actres " + (ctl.result.tone === "err" ? "err-t" : "t2")} role="status">{ctl.result.text} <button className="linkbtn" onClick={() => ctl.setResult(null)}>dismiss</button></p> : null}
        </section>
        <div className="track" aria-label="Phase progress">
          {track.map((t) => <div key={t.label} className={`st ${t.st}${t.gate ? " gate" : ""}`} title={t.title}><i /><span>{t.label}</span></div>)}
        </div>
        <StaleBanner lag={(d as JobDetailT & { lag?: number }).lag ?? 0} active={s.status === "running" || s.status === "queued"} />
        {openGate && s.status === "waiting_human" ? (
          <Banner kind="attention" glyph="diamond" lead={`Waiting for ${humanize(openGate.kind).toLowerCase()} from ${openGate.eligible?.okta_groups?.join(", ") ?? "approvers"}`}
            acts={<Link to={`/approvals/${openGate.gate_id}`} className="btn sm">Open approval</Link>}>
            {openGate.expires_at ? <>· expires {dateTime(openGate.expires_at)} ({timeLeft(openGate.expires_at)})</> : null}
          </Banner>
        ) : null}
        {s.status === "paused" || s.paused ? <Banner kind="neutral" glyph="bars" lead="Paused" acts={control ? <button className="btn sm" onClick={() => void ctl.run("resume")}>Resume</button> : undefined}>at the next node boundary; nothing was cancelled.</Banner> : null}
        {s.status === "failed" ? (
          <Banner kind="danger" glyph="cross" lead={`Failed${s.terminal?.reason ? `: ${s.terminal.reason}` : ""}.`}>
            {s.terminal?.failure_class ? <>Failure class: {s.terminal.failure_class}. </> : null}Phase <span className="mono">{s.phase}</span>. Nothing was published unless a PR is linked below.
          </Banner>
        ) : null}
        {s.status === "cancelled" ? <Banner kind="neutral" glyph="slash" lead="Cancelled.">{s.terminal?.reason ? `${s.terminal.reason}. ` : ""}Runs were stopped and nothing was published{pr ? `; PR #${pr.number} is ${pr.state}` : ""}.</Banner> : null}
        <div className={"jobgrid" + (tab === "graph" ? " wide" : "")}>
          <div>
            {live ? (
              <div className="livenode">
                <Glyph g={runStatus(live.phase).glyph} family="info" />
                <div className="what"><b>{liveRun?.role ?? live.node_key}</b> <Id id={live.run_id} /> <span className="t2">· {live.phase}{liveRun?.isolation ? ` · ${liveRun.isolation}` : ""}</span></div>
                <div className="row g3 small"><span className="t2 num">{live.started_at ? `running ${duration(Date.now() - (parseTs(live.started_at) ?? Date.now()))}` : live.phase}</span>
                  <Link to={activityHref(jobId, live.node_key, live.run_id)} className="t2" data-testid="live-activity">Activity</Link>
                  <Ext href={grafanaJobUrl(jobId)} className="t2">Logs ↗</Ext></div>
              </div>
            ) : null}
            <nav className="tabs" aria-label="Job sections">
              {tabLink("timeline", "Timeline", tl.data?.length)}
              {tabLink("graph", "Graph")}
              {tabLink("runs", "Runs", runs.length)}
              {tabLink("gates", "Gates", s.gates?.length ?? 0)}
              {tabLink("plan", "Plan")}
              {tabLink("diff", "Diff")}
              {tabLink("qa", "QA")}
            </nav>
            {tab === "timeline" ? (
              tl.isError ? <ErrorBanner error={tl.error} what="the timeline" retry={() => void tl.refetch()} /> :
              !tl.data ? <div className="section"><div className="skel-line w70" /><div className="skel-line w40" /></div> : (
                <ol className="tl" aria-label="Timeline">
                  {rows.map((r) => (
                    <li key={r.key} className={r.cls}>
                      <span className="when" title={r.at}>{clock(r.at)}</span>
                      <span className="gl"><Glyph g={r.glyph} family={r.family} /></span>
                      <div className="body">{r.body}</div>
                      <span className="right">{r.right ?? null}</span>
                    </li>
                  ))}
                </ol>
              )
            ) : null}
            {tab === "graph" ? <GraphTab jobId={jobId} jobStatus={s.status} /> : null}
            {tab === "runs" ? <RunsTab runs={runs} jobId={jobId} /> : null}
            {tab === "gates" ? <GatesTab gates={s.gates ?? []} dir={dir} /> : null}
            {tab === "plan" || tab === "diff" || tab === "qa" ? <PacketTab gate={openGate ?? lastGate} which={tab} /> : null}
          </div>
          {tab === "graph" ? null : <aside className="side">
            <div className="section">
              <dl className="kv">
                <dt>Status</dt><dd><span className="row g1"><Glyph g={st.glyph} family={st.family} />{st.label} · {s.phase}</span></dd>
                <dt>Cost so far</dt><dd><div className="row"><span className="num">{usd(d.cost.usd_so_far ?? 0)}</span><span className="t3 small">{d.cost.usd_cap ? `of ${usd(d.cost.usd_cap)} cap` : "no cost rollup in v0"}</span></div>
                  {d.cost.usd_cap ? <div className={"bar" + ((d.cost.usd_so_far ?? 0) / d.cost.usd_cap >= 0.8 ? " warn" : "")} style={{ marginTop: 5 }}><i style={{ width: `${Math.min(100, ((d.cost.usd_so_far ?? 0) / d.cost.usd_cap) * 100)}%` }} /></div> : null}</dd>
                <dt>Risk</dt><dd><Risk level={s.risk?.level} /> <span className="t3 small">{s.risk?.rules_fired?.length ?? 0} rule{s.risk?.rules_fired?.length === 1 ? "" : "s"}</span></dd>
                <dt>PR</dt><dd>{pr ? <><Ext href={browserGitUrl(pr.url)} className="mono">#{pr.number} ↗</Ext> <span className="t3 small">{pr.state}</span></> : <span className="t3">none yet</span>}</dd>
                <dt>Bundle</dt><dd>{s.release_bundle_id ? (hasCap(me, "release:read") ? <Link to="/releases" className="mono">{s.release_bundle_id.slice(0, 13)}</Link> : <span className="mono">{s.release_bundle_id.slice(0, 13)}</span>) : "—"}</dd>
                <dt>Worker</dt><dd className="mono" style={{ overflowWrap: "anywhere" }}>{s.worker_deployment_version ? `${s.worker_deployment_version.deployment_name}:${s.worker_deployment_version.build_id}` : "—"}</dd>
                <dt>Repo / base</dt><dd className="mono ellipsis" title={repo?.base_sha}>{repo ? `${repo.base_ref} @ ${repo.base_sha?.slice(0, 10) ?? "?"}` : "—"}</dd>
                <dt>Repair budget</dt><dd className="num">{s.repair_budget ? `${s.repair_budget.used} of ${s.repair_budget.max} cycles` : "—"}</dd>
                <dt>Requester</dt><dd>{personName(dir, s.requester)}</dd>
                <dt>Updated</dt><dd className="t2">{age(s.as_of)} ago <span className="t3 small">seq {s.seq}</span></dd>
              </dl>
            </div>
            <MemoryUsed jobId={jobId} snapshotId={s.memory_snapshot_id} />
            <div className="section links">
              <div className="section-h"><h3>Observe</h3></div>
              <a href={grafanaJobUrl(jobId)} target="_blank" rel="noreferrer noopener">Grafana job drilldown <span className="ext">job_id ↗</span></a>
              {hasCap(me, "temporal:ui") ? <a href={temporalWorkflowUrl(jobId)} target="_blank" rel="noreferrer noopener">Temporal workflow <span className="ext">operators ↗</span></a> : null}
              {issueUrl ? <a href={issueUrl} target="_blank" rel="noreferrer noopener">Gitea issue <span className="ext">#{issueNo} ↗</span></a> : null}
              {pr?.url ? <a href={browserGitUrl(pr.url)} target="_blank" rel="noreferrer noopener">Gitea pull request <span className="ext">#{pr.number} ↗</span></a> : null}
              <a href={grafanaContainersUrl()} target="_blank" rel="noreferrer noopener">Containers dashboard <span className="ext">grafana ↗</span></a>
            </div>
          </aside>}
        </div>
      </main>
      {dialog === "cancel" ? <CancelDialog job={d} busy={ctl.busy === "cancel"} onClose={() => setDialog(null)}
        onConfirm={(reason) => { void ctl.run("cancel", reason.trim() ? { reason: reason.trim() } : {}).then(() => setDialog(null)); }} /> : null}
      {dialog === "steer" ? <SteerDialog job={d} onClose={() => setDialog(null)} /> : null}
      {dialog === "pause" ? (
        <PauseDialog live={live?.node_key} onClose={() => setDialog(null)}
          onConfirm={(mode) => { setDialog(null); void ctl.run("pause", { pause_mode: mode }); }} />
      ) : null}
    </>
  );
}

function PauseDialog({ live, onClose, onConfirm }: { live?: string; onClose: () => void; onConfirm: (mode: string) => void }) {
  return (
    <div className="popwrap">
      <div className="scrim" style={{ display: "block" }} onClick={onClose} />
      <div className="dialog" style={{ display: "block", width: 460 }} role="dialog" aria-label="Pause job">
        <div className="dh"><h2>Pause this job?</h2></div>
        <div className="db t2">
          <p><b>At the next node:</b> {live ? `the current ${live} run finishes first; ` : ""}nothing is cancelled.</p>
          <p style={{ marginTop: 6 }}><b>At the next gate:</b> work continues until a gate opens; approvers are not notified while paused.</p>
        </div>
        <div className="df"><span className="grow" /><button className="btn" onClick={onClose}>Keep running</button>
          <button className="btn" onClick={() => onConfirm("at_next_gate")}>At next gate</button>
          <button className="btn primary" onClick={() => onConfirm("at_next_node")}>At next node<Kbd>P</Kbd></button></div>
      </div>
    </div>
  );
}
