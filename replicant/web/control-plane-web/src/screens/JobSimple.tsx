// Simple job view (WEB-UIPREF; the default for everyone): one plain sentence, six steps, what is needed from you,
// the issue and PR, and milestones only. "Show details" switches to the Detailed view and saves the choice per user.
import { MemoryLine } from "../components/MemoryUsed";
import { useMemo, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { TopBar } from "../components/Shell";
import { Glyph } from "../components/Glyph";
import { Empty, ErrorBanner, Ext, Id, StaleBanner } from "../components/bits";
import { DecisionBar } from "./Approvals";
import { ApiError, get } from "../lib/api";
import { personName, useDirectory, useJobView, useMe, useSession } from "../lib/session";
import { useJobDetail, useTimeline } from "../lib/jobs";
import { headline, milestones, simpleSteps, simpleStepsFromGraph, stageStates, type SimpleStep } from "../lib/simple";
import type { GraphDoc } from "../lib/graph";
import { dateTime, humanize, timeLeft } from "../lib/format";
import { jobStatus, TERMINAL } from "../lib/status";
import { browserGitUrl, repoName } from "../lib/config";
import { Link, useRouter } from "../router";
import "../styles/simple.css";
import type { GateDetail, JobSnapshot, Me } from "../lib/types";

/** "Show details" in Simple, "Simple view" in Detailed; saves the per-user preference. */
export function ViewToggle({ jobId }: { jobId: string }) {
  const { view, setView, saving, error } = useJobView();
  const { navigate, loc } = useRouter();
  const next = view === "simple" && loc.path === `/jobs/${jobId}` ? "detailed" : "simple";
  return (
    <span className="row g1">
      {error ? <span className="err-t small" role="alert">Couldn't save ({error.code})</span> : null}
      <button className="btn" disabled={saving} data-testid="view-toggle" aria-pressed={next === "simple"}
        onClick={() => { void setView(next); if (next === "simple" && loc.path !== `/jobs/${jobId}`) navigate(`/jobs/${jobId}`); }}>
        <Glyph g={next === "detailed" ? "plain" : "dot-hollow"} />{next === "detailed" ? "Show details" : "Simple view"}
      </button>
    </span>
  );
}

const STEP_GLYPH: Record<string, { g: string; f: "success" | "info" | "attention" | "danger" | "neutral"; word: string }> = {
  done: { g: "check", f: "success", word: "done" },
  now: { g: "ring-spin", f: "info", word: "in progress" },
  wait: { g: "diamond", f: "attention", word: "waiting on a person" },
  fail: { g: "cross", f: "danger", word: "stopped here" },
  skip: { g: "dot-hollow", f: "neutral", word: "not needed" },
  "": { g: "ring-dotted", f: "neutral", word: "not yet" },
};

function Steps({ steps }: { steps: SimpleStep[] }) {
  return (
    <ol className="ssteps" aria-label="Progress">
      {steps.map((x, i) => {
        const g = STEP_GLYPH[x.st] ?? STEP_GLYPH[""];
        return (
          <li key={x.key} className={`ss ${x.st || "todo"}`} aria-current={x.st === "now" || x.st === "wait" ? "step" : undefined}>
            <i className="bar" />
            <span className="lbl"><Glyph g={g.g} family={g.f} /><span><span className="n">{i + 1}.</span> {x.label}</span></span>
            <span className="st">{g.word}</span>
          </li>
        );
      })}
    </ol>
  );
}

const NOT_DECIDABLE: Record<string, string> = {
  self_approval_forbidden: "you requested this job, and requesters can't approve their own change",
  not_in_approver_group: "you are not in an approver group for this tenant",
  gate_not_open: "this approval is already closed",
};

function Needed({ s, me, issueUrl }: { s: JobSnapshot; me?: Me; issueUrl?: string }) {
  const { user } = useSession();
  const qc = useQueryClient();
  const dir = useDirectory();
  const gate = (s.gates ?? []).find((g) => g.state === "open" || g.state === "partially_approved");
  const gq = useQuery({ queryKey: ["gate", user, gate?.gate_id], enabled: !!gate,
    queryFn: () => get<GateDetail>(`/v1/gates/${gate!.gate_id}`) });
  const pr = s.pull_request;
  const mine = !!me && me.principal_id === s.requester;
  let body: ReactNode;
  let urgent = false;
  if (gate && s.status === "waiting_human") {
    const who = gate.eligible?.okta_groups?.join(", ") ?? "an approver";
    if (gq.isError) body = <ErrorBanner error={gq.error} what="the approval" retry={() => void gq.refetch()} />;
    else if (!gq.data) body = <div className="skel-line w40" />;
    else if (gq.data.decidable) {
      urgent = true;
      body = (
        <>
          <p><b>Your decision is needed.</b> {gate.kind === "plan_approval" ? "Check Claude's plan, then approve it or send it back." : "Check the change, then approve it to open the pull request, or send it back."}
            {gate.expires_at ? <span className="t3"> Expires {dateTime(gate.expires_at)} ({timeLeft(gate.expires_at)}).</span> : null}</p>
          <p className="small"><Link to={`/approvals/${gate.gate_id}`}>See the plan, change and test results</Link></p>
          <DecisionBar d={gq.data} onDecided={() => { void qc.invalidateQueries({ queryKey: ["job", user, s.job_id] }); void qc.invalidateQueries({ queryKey: ["timeline", user, s.job_id] }); }} />
        </>
      );
    } else {
      const why = gq.data.not_decidable_reason ? NOT_DECIDABLE[gq.data.not_decidable_reason] ?? humanize(gq.data.not_decidable_reason).toLowerCase() : "";
      body = <p>Nothing from you. {humanize(gate.kind).replace(/ approval$/, "")} approval is with <b>{who}</b>{why ? <span className="t3"> ({why})</span> : null}.</p>;
    }
  } else if (s.status === "waiting_external" && pr && pr.state === "open") {
    urgent = mine;
    body = (
      <>
        <p>{mine ? <><b>Review and merge the pull request.</b> Claude's work is done; the change goes in when the PR is merged.</>
          : <>The pull request is waiting for review{personName(dir, s.requester) !== "—" ? <> by {personName(dir, s.requester)} or the repository's reviewers</> : null}.</>}</p>
        {pr.url ? <p><Ext href={browserGitUrl(pr.url)} className="btn primary">Review PR #{pr.number} ↗</Ext></p> : null}
      </>
    );
  } else if (s.status === "failed" || s.status === "rejected" || s.status === "expired") {
    body = <p>Nothing is running. {mine ? <>To try again, re-request from the {issueUrl ? <Ext href={issueUrl}>issue ↗</Ext> : "issue"}.</> : null}</p>;
  } else {
    body = <p className="t2">Nothing right now.{TERMINAL.has(s.status) ? "" : " We'll tell you if that changes."}</p>;
  }
  return (
    <section className={"scard" + (urgent ? " urgent" : "")} aria-label="What's needed from you" data-testid="needed">
      <h2>What's needed from you</h2>
      {body}
    </section>
  );
}

export function JobSimple({ jobId }: { jobId: string }) {
  const me = useMe().data;
  const dir = useDirectory();
  const job = useJobDetail(jobId);
  const tl = useTimeline(jobId, job.isSuccess);
  const s = job.data?.snapshot;
  const { user } = useSession();
  // The job's own graph (same query as the Graph tab): steps follow its workflow; the bugfix track is the fallback.
  const graph = useQuery({
    queryKey: ["graph", user, jobId], queryFn: () => get<GraphDoc>(`/v1/jobs/${jobId}/graph`), enabled: !!s,
    refetchInterval: s && TERMINAL.has(s.status) ? false : 4000,
  });
  const nodes = graph.data?.graph?.nodes;
  const steps = useMemo(() => {
    if (!s) return [];
    if (nodes?.length) return simpleStepsFromGraph(nodes, s, s.workflow_type);
    return tl.data && (graph.isError || graph.data) ? simpleSteps(stageStates(tl.data, s), s) : [];
  }, [s, tl.data, nodes, graph.isError, graph.data]);
  const typeOf = useMemo(() => new Map((nodes ?? []).map((n) => [n.key, n.type])), [nodes]);
  const ms = useMemo(() => (s && tl.data ? milestones(tl.data, s, typeOf).reverse() : []), [s, tl.data, typeOf]);

  const crumbs = (
    <>
      <Link to="/jobs">Jobs</Link><span className="sep">/</span><span>{s?.tenant_id ?? "…"}</span><span className="sep">/</span>
      <b><Id id={jobId} full /></b>
    </>
  );
  if (job.isError) {
    const nf = job.error instanceof ApiError && job.error.status === 404;
    return (
      <>
        <TopBar>{crumbs}</TopBar>
        <main className="main">{nf ? <Empty title={`No job ${jobId} that you can see`}>It may not exist, or it belongs to a tenant or repository you can't read. <Link to="/jobs">Back to jobs</Link></Empty>
          : <ErrorBanner error={job.error} what="this job" retry={() => void job.refetch()} />}</main>
      </>
    );
  }
  if (!s || !job.data) {
    return (<><TopBar>{crumbs}</TopBar><main className="main" aria-busy="true"><section className="simple"><div className="skel-line w70 tall" /><div className="skel-line w40" /></section></main></>);
  }
  const d = job.data;
  const st = jobStatus(s.status);
  const pr = s.pull_request;
  const issueUrl = browserGitUrl(d.source?.url);
  const issueNo = issueUrl?.match(/\/issues\/(\d+)/)?.[1];
  const repo = s.targets?.[0];

  return (
    <>
      <TopBar>{crumbs}</TopBar>
      <main className="main">
        <section className="simple" data-testid="job-simple">
          <div className="shead">
            <div className="grow">
              <h1>{s.title}</h1>
              <p className="t2 small">{repo ? <span className="mono">{repoName(repo.repo)}</span> : null}{repo ? " · " : null}requested by {personName(dir, s.requester)}</p>
            </div>
            <ViewToggle jobId={jobId} />
          </div>
          <p className={`sline f-${st.family}`} data-testid="headline"><Glyph g={st.glyph} family={st.family} /><span>{tl.data ? headline(s, steps) : "…"}</span></p>
          <StaleBanner lag={(d as typeof d & { lag?: number }).lag ?? 0} active={s.status === "running" || s.status === "queued"} />
          {tl.isError ? <ErrorBanner error={tl.error} what="the progress" retry={() => void tl.refetch()} /> : <Steps steps={steps} />}
          <MemoryLine jobId={jobId} />
          <div className="sgrid">
            <div>
              <Needed s={s} me={me} issueUrl={issueUrl} />
              <section className="scard" aria-label="Milestones">
                <h2>What has happened</h2>
                {!tl.data ? <div className="skel-line w40" /> : ms.length ? (
                  <ol className="sms">
                    {ms.map((m) => (
                      <li key={m.key}><Glyph g={m.family === "success" ? "check" : m.family === "danger" ? "cross" : m.family === "attention" ? "diamond" : "dot"} family={m.family} />
                        <span>{m.text}</span><span className="t3 small when" title={m.at}>{dateTime(m.at)}</span></li>
                    ))}
                  </ol>
                ) : <p className="t3">Nothing yet.</p>}
              </section>
            </div>
            <aside className="scard links" aria-label="Links">
              <h2>Links</h2>
              {issueUrl ? <Ext href={issueUrl}>Issue{issueNo ? ` #${issueNo}` : ""} ↗</Ext> : <span className="t3">No linked issue</span>}
              {pr?.url ? <Ext href={browserGitUrl(pr.url)}>Pull request #{pr.number} ↗ <span className="t3 small">{pr.state}</span></Ext> : <span className="t3">No pull request yet</span>}
            </aside>
          </div>
        </section>
      </main>
    </>
  );
}
