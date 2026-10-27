import { useEffect, useState, type MouseEvent } from "react";
import { TopBar } from "../components/Shell";
import { Glyph } from "../components/Glyph";
import { Empty, ErrorBanner, Id, Kbd, Risk, SkeletonRows } from "../components/bits";
import { personName, useDirectory, useWaiting } from "../lib/session";
import { age, clock, dateTime, hoursUntil, humanize, timeLeft } from "../lib/format";
import { GATE_KIND_SHORT } from "../lib/status";
import { browserGitUrl, repoName, repoShort } from "../lib/config";
import { prIdle, ROLE_TEXT, waitingSummary } from "../lib/waiting";
import { Link, useRouter } from "../router";
import type { WaitingGate, WaitingPr } from "../lib/types";

const gateHref = (g: WaitingGate) => `/approvals/${g.gate_id}`;
const prHref = (p: WaitingPr) => `/jobs/${p.job_id}`;

/** Row click opens the row's page, unless the click was on a link inside it (which handles itself). */
const rowNav = (navigate: (to: string) => void, to: string) => (e: MouseEvent) => {
  if ((e.target as HTMLElement).closest("a, button")) return;
  navigate(to);
};

function GateRow({ g, sel, dir }: { g: WaitingGate; sel: boolean; dir: Map<string, string> }) {
  const { navigate } = useRouter();
  const left = timeLeft(g.expires_at);
  const soon = hoursUntil(g.expires_at) < 12;
  return (
    <tr className={sel ? "sel" : ""} onClick={rowNav(navigate, gateHref(g))} data-waiting-kind="gate" data-gate-id={g.gate_id}>
      <td className="kindcell"><span className="row g1"><Glyph g="diamond" family="attention" />{GATE_KIND_SHORT[g.gate_kind] ?? humanize(g.gate_kind)}</span></td>
      <td className="title">
        <Link to={gateHref(g)} className="rowlink">{g.title || <span className="t3">untitled</span>}</Link>
        <span className="second mono">{repoShort(g.repo)} · {personName(dir, g.requester)}</span>
      </td>
      <td className="mono small p2" title={repoName(g.repo)}>{repoShort(g.repo)}</td>
      <td><Risk level={g.risk_level} /></td>
      <td className="req p3">{personName(dir, g.requester)}</td>
      <td className="right t3" title={`Opened ${dateTime(g.opened_at)}`}>{age(g.waiting_since ?? g.opened_at)}</td>
      <td className="right" title={g.expires_at ? `Expires ${dateTime(g.expires_at)}; then the job expires` : "No expiry"}>
        {left ? <span className={soon ? "attn-t" : "t2"}>{left}</span> : <span className="t3">—</span>}
      </td>
    </tr>
  );
}

function PrRow({ p, sel }: { p: WaitingPr; sel: boolean }) {
  const { navigate } = useRouter();
  const url = browserGitUrl(p.pr.url);
  const reminded = p.last_reminded_at ? dateTime(p.last_reminded_at) : null;
  return (
    <tr className={sel ? "sel" : ""} onClick={rowNav(navigate, prHref(p))} data-waiting-kind="pr" data-job-id={p.job_id}>
      <td className="kindcell">
        {url ? <a href={url} target="_blank" rel="noreferrer noopener" className="mono prlink" title="Open the pull request in Gitea (new tab)">PR #{p.pr.number}<span className="t3"> ↗</span></a>
          : <span className="mono">PR #{p.pr.number}</span>}
      </td>
      <td className="title">
        <Link to={prHref(p)} className="rowlink">{p.title || <span className="t3">untitled</span>}</Link>
        <span className="second mono">{repoShort(p.repo)} · {p.role} · idle {prIdle(p)}</span>
      </td>
      <td className="mono small p2" title={repoName(p.repo)}>{repoShort(p.repo)}</td>
      <td><span className="tag" title={ROLE_TEXT[p.role]}>{p.role === "requester" ? "Requester" : "Reviewer"}</span></td>
      <td className="p3"><Id id={p.job_id} to={prHref(p)} /></td>
      <td className="right t3" title={`Waiting since ${p.waiting_since}`}>{dateTime(p.waiting_since)}</td>
      <td className="right" title="Time since the last activity on the PR">{prIdle(p)}</td>
      <td className="right t3 p2">{reminded ?? "none"}</td>
    </tr>
  );
}

export function Waiting() {
  const q = useWaiting();
  const dir = useDirectory();
  const { navigate } = useRouter();
  const w = q.data;
  const hrefs = w ? [...w.gates.map(gateHref), ...w.prs.map(prHref)] : [];
  const [sel, setSel] = useState(-1);
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t.closest("input, textarea, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "j") { setSel((s) => Math.min(s + 1, hrefs.length - 1)); e.preventDefault(); }
      else if (e.key === "k") { setSel((s) => Math.max(0, s - 1)); e.preventDefault(); }
      else if (e.key === "Enter" && hrefs[sel]) navigate(hrefs[sel]);
    };
    addEventListener("keydown", on);
    return () => removeEventListener("keydown", on);
  }, [hrefs, sel, navigate]);

  const loading = !w && !q.isError;
  const nothing = w && w.gates.length + w.prs.length === 0;
  const nGates = w?.gates.length ?? 0;
  return (
    <>
      <TopBar>
        <b>Waiting on you</b>
        <span className="t3">· {w ? waitingSummary(w) : "…"}</span>
      </TopBar>
      <main className="main waiting">
        {q.isError ? <ErrorBanner error={q.error} what="what is waiting on you" retry={() => void q.refetch()} /> : null}
        {nothing ? (
          <Empty title="Nothing is waiting on you">
            {w.available ? <>Approvals you can decide and open pull requests you requested or reviewed show up here as soon as they need you.
              Gates you can't decide, for example on your own job, are left out; see <Link to="/approvals">Approvals</Link> and <Link to="/jobs?view=pr_review">In PR review</Link> for everything else.</>
              : <>This control-plane-api does not serve <span className="mono">/v1/me/waiting</span> yet, so this list can't be filled. Gates you can decide are still under <Link to="/approvals">Approvals</Link>.</>}
          </Empty>
        ) : null}
        {loading || (w && !nothing) ? (
          <>
            <div className="section">
              <div className="section-h"><h3>Approvals you can decide</h3><span className="t3 small">{w ? `${nGates} · soonest expiry first` : ""}</span></div>
              {w && nGates === 0 ? <p className="t3 small">No gates are waiting on your decision.</p> : (
                <table className="t jl wt" aria-label="Approvals you can decide">
                  <thead><tr><th>Gate</th><th>Title</th><th className="p2">Repo</th><th>Risk</th><th className="p3">Requester</th><th className="right">Waiting</th><th className="right sorted">Expires</th></tr></thead>
                  <tbody>
                    {loading ? <SkeletonRows cols={[60, 300, 90, 40, 70, 30, 50]} rows={2} /> : null}
                    {w?.gates.map((g, i) => <GateRow key={g.gate_id} g={g} sel={i === sel} dir={dir} />)}
                  </tbody>
                </table>
              )}
            </div>
            <div className="section">
              <div className="section-h"><h3>Pull requests waiting for review</h3><span className="t3 small">{w ? `${w.prs.length} · longest waiting first` : ""}</span></div>
              {w && w.prs.length === 0 ? <p className="t3 small">No open pull requests are waiting on you.</p> : (
                <table className="t jl wt" aria-label="Pull requests waiting for review">
                  <thead><tr><th>PR</th><th>Title</th><th className="p2">Repo</th><th>Your role</th><th className="p3">Job</th><th className="right sorted">Waiting since</th><th className="right">Idle</th><th className="right p2">Last reminder</th></tr></thead>
                  <tbody>
                    {loading ? <SkeletonRows cols={[50, 300, 90, 60, 96, 60, 30, 60]} rows={2} /> : null}
                    {w?.prs.map((p, i) => <PrRow key={p.job_id + p.pr.number} p={p} sel={nGates + i === sel} />)}
                  </tbody>
                </table>
              )}
            </div>
            {w ? (
              <div className="row small t3 listfoot">
                Updates every 4 s{w.asOf ? ` · as of ${clock(w.asOf)}` : ""} · <Kbd>J</Kbd><Kbd>K</Kbd> move · <Kbd>↵</Kbd> open · PR links open Gitea in a new tab
              </div>
            ) : null}
          </>
        ) : null}
      </main>
    </>
  );
}
