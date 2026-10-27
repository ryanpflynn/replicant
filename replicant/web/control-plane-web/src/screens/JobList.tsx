import { useEffect, useMemo, useRef, useState } from "react";
import { TopBar } from "../components/Shell";
import { Empty, ErrorBanner, Id, Kbd, Pill, SkeletonRows } from "../components/bits";
import { countLabel, useJobs, VIEWS, type ViewKey } from "../lib/jobs";
import { personName, useDirectory, useMe } from "../lib/session";
import { age, timeLeft, hoursUntil, usd } from "../lib/format";
import { jobStatus, OUTCOME_WORDS } from "../lib/status";
import { repoShort } from "../lib/config";
import { Link, useRouter } from "../router";
import type { JobSummary } from "../lib/types";

const CHIPS: ViewKey[] = ["open", "needs_human", "running", "pr_review", "recent", "failed_24h"];

function phaseCell(j: JobSummary) {
  if (j.status === "waiting_human" && j.open_gate) {
    const left = timeLeft(j.open_gate.expires_at);
    return <>{j.open_gate.kind.replace("_approval", "_gate")} {left ? <span className={hoursUntil(j.open_gate.expires_at) < 12 ? "attn-t" : "t3"}>· {left}</span> : null}</>;
  }
  if (j.status === "waiting_external" && j.pr) return <>pr_review <span className="t3">· PR #{j.pr.number}</span></>;
  if (j.sub_status) return <>waiting: {j.sub_status.replace(/_/g, " ")}</>;
  if (j.status === "succeeded" && j.pr) return <>{j.phase} <span className="t3">· PR #{j.pr.number} {j.pr.state}</span></>;
  return <>{j.phase}{j.current_node && j.current_node !== j.phase ? <span className="t3"> · {j.current_node}</span> : null}</>;
}

function Chip({ k, active }: { k: ViewKey; active: boolean }) {
  const v = VIEWS.find((x) => x.key === k)!;
  const d = useJobs(k).data;
  const { navigate, loc } = useRouter();
  const q = loc.search.get("q");
  return (
    <button className={"chip" + (active ? " on" : "")} title={v.title} aria-pressed={active}
      onClick={() => navigate(`/jobs?view=${k}${q ? "&q=" + encodeURIComponent(q) : ""}`)}>
      {v.label} <span className="n">{k === "recent" ? "7d" : countLabel(d)}</span>
    </button>
  );
}

export function JobList() {
  const { loc, navigate } = useRouter();
  const me = useMe().data;
  const dir = useDirectory();
  const view = (loc.search.get("view") as ViewKey) || "open";
  const q = loc.search.get("q") ?? "";
  const [draft, setDraft] = useState(q);
  useEffect(() => setDraft(q), [q]);
  const jobs = useJobs(view, q);
  const items = jobs.data?.items;
  const [sel, setSel] = useState(0);
  const filterRef = useRef<HTMLInputElement>(null);

  // Rows never reorder while the pointer is over the table; a pill counts held updates (ui/job-list.md (4)).
  const [hover, setHover] = useState(false);
  const [held, setHeld] = useState<JobSummary[] | undefined>(undefined);
  useEffect(() => { if (!hover) setHeld(items); }, [hover, items]);
  const shown = hover && held ? held.map((h) => items?.find((x) => x.job_id === h.job_id) ?? h) : items;
  const pending = useMemo(() => {
    if (!hover || !held || !items) return 0;
    const order = (l: JobSummary[]) => l.map((x) => x.job_id + x.updated_at).join("|");
    return order(held) === order(items) ? 0 : Math.max(1, Math.abs(items.length - held.length) || items.filter((x, i) => held[i]?.job_id !== x.job_id).length);
  }, [hover, held, items]);

  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t.closest("input, textarea, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
      const n = shown?.length ?? 0;
      if (e.key === "j") { setSel((s) => Math.min(s + 1, n - 1)); e.preventDefault(); }
      else if (e.key === "k") { setSel((s) => Math.max(0, s - 1)); e.preventDefault(); }
      else if (e.key === "Enter" && shown?.[sel]) navigate(`/jobs/${shown[sel].job_id}`);
      else if (e.key === "/") { filterRef.current?.focus(); e.preventDefault(); }
    };
    addEventListener("keydown", on);
    return () => removeEventListener("keydown", on);
  }, [shown, sel, navigate]);

  const tenants = me?.tenants.map((t) => t.tenant_id) ?? [];
  const label = VIEWS.find((v) => v.key === view)?.label ?? "Jobs";
  const noTenant = me && tenants.length === 0 && !me.capabilities.includes("admin:platform") && !me.capabilities.includes("release:read");
  return (
    <>
      <TopBar>
        <b>{view === "mine" ? "My jobs" : "Jobs"}</b>
        <span className="t3">· {tenants.length === 1 ? `tenant ${tenants[0]}` : tenants.length ? `${tenants.length} tenants` : "all tenants"}</span>
        {jobs.data && view === "open" ? <span className="t3">· {jobs.data.items.length} open</span> : null}
      </TopBar>
      <main className="main">
        <div className="filters">
          {CHIPS.map((k) => <Chip key={k} k={k} active={view === k} />)}
          {view === "mine" ? <span className="chip on" aria-pressed="true">My jobs <span className="n">{countLabel(jobs.data)}</span></span> : null}
          <span className="grow" />
          <form className="qform" onSubmit={(e) => { e.preventDefault(); navigate(`/jobs?view=${view}${draft.trim() ? "&q=" + encodeURIComponent(draft.trim()) : ""}`); }}>
            <input ref={filterRef} className="qinput mono" value={draft} onChange={(e) => setDraft(e.target.value)}
              placeholder="repo:ledger-* requester:me text" aria-label="Filter jobs (query language)"
              onKeyDown={(e) => { if (e.key === "Escape") { setDraft(""); navigate(`/jobs?view=${view}`); (e.target as HTMLInputElement).blur(); } }} />
            <Kbd>/</Kbd>
          </form>
        </div>
        {jobs.isError ? <ErrorBanner error={jobs.error} what="jobs" retry={() => void jobs.refetch()} /> : null}
        {pending ? <div className="holdpill" role="status"><button className="btn sm" onClick={() => setHover(false)}>{pending} update{pending === 1 ? "" : "s"} · show</button></div> : null}
        {noTenant ? (
          <Empty title="You are not in any replicant tenant">Tenant access comes from Okta groups (ask your team lead). Locally, switch to another dev user from the menu at the bottom of the rail.</Empty>
        ) : (
          <table className="t jl" aria-label={`${label} jobs`} onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
            <thead><tr><th>Job</th><th>Title</th><th>Status</th><th>Phase / node</th><th className="p2">Repo</th><th className="p3">Requester</th><th className="right p3">Cost</th><th className="right sorted">Updated</th></tr></thead>
            <tbody>
              {!shown && !jobs.isError ? <SkeletonRows cols={[96, 260, 110, 150, 90, 90, 40, 30]} /> : null}
              {shown?.map((j, i) => {
                const st = jobStatus(j.status);
                const req = personName(dir, j.requester);
                return (
                  <tr key={j.job_id} className={i === sel ? "sel" : ""} onClick={() => navigate(`/jobs/${j.job_id}`)} data-job-id={j.job_id}>
                    <td><Id id={j.job_id} to={`/jobs/${j.job_id}`} /></td>
                    <td className="title">
                      <Link to={`/jobs/${j.job_id}`} className="rowlink">{j.title || <span className="t3">untitled</span>}</Link>
                      <span className="second mono">{j.repos.map(repoShort).join(", ")} · {req} · {usd(j.cost_usd ?? 0)}</span>
                    </td>
                    <td><Pill s={st} /></td>
                    <td className="phase" title={j.status === "failed" ? OUTCOME_WORDS[j.phase] ?? j.phase : undefined}>{phaseCell(j)}</td>
                    <td className="mono small p2">{j.repos.map(repoShort).join(", ")}</td>
                    <td className="req p3">{j.on_behalf_of ? <><span className="t2">{req}</span> <span className="t3">for</span> {personName(dir, j.on_behalf_of)}</> : req}</td>
                    <td className="right num p3">{usd(j.cost_usd ?? 0)}</td>
                    <td className="right t3" title={j.updated_at}>{age(j.updated_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        {shown && shown.length === 0 ? (
          q ? <Empty>No jobs match <span className="mono">{q}</span>. <Kbd>Esc</Kbd> in the filter clears it.</Empty>
            : view === "open" ? <Empty title="No open jobs">Terminal jobs are under <Link to="/jobs?view=recent">Recent</Link>. Start one by labelling a Gitea issue <span className="tag mono">replicant</span> in <span className="mono">acme-payments/ledger-api</span>.</Empty>
            : <Empty>No jobs in “{label}” right now.</Empty>
        ) : null}
        {shown && shown.length > 0 ? (
          <div className="row small t3 listfoot">
            {shown.length}{jobs.data?.more ? "+" : ""} {view === "open" ? "open " : ""}job{shown.length === 1 ? "" : "s"}
            {view === "open" ? " · terminal jobs are under Recent" : ""} · updates every 4 s · <Kbd>J</Kbd><Kbd>K</Kbd> move · <Kbd>↵</Kbd> open · <Kbd>/</Kbd> filter
          </div>
        ) : null}
      </main>
    </>
  );
}
