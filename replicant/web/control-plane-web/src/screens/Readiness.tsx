// Project settings → Readiness (design §8c, onboarding-interfaces.md §10): for each repo, whether jobs can run on it,
// the environment Replicant inferred and where each command came from, the repo's Claude Code setup, warnings with a
// "Request support" action for project admins, the last readiness probe, and "Re-check".
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Pill, Empty, ErrorBanner } from "../components/bits";
import { Dialog } from "../components/Dialog";
import { ApiError } from "../lib/api";
import { useSession } from "../lib/session";
import { dateTime } from "../lib/format";
import { Link } from "../router";
import {
  classBadge, ENV_COMMANDS, getReadiness, readinessBadge, recheckRepo, requestSupport, sourceWord,
  type ReadinessRepo, type ReadinessWarning, type RepoConfigSummary,
} from "../lib/onboarding";

export function Readiness({ projectId, canRecheck, canRequest }: { projectId: string; canRecheck: boolean; canRequest: boolean }) {
  const { user } = useSession();
  const q = useQuery({ queryKey: ["project-readiness", user, projectId], queryFn: () => getReadiness(projectId), refetchInterval: 15_000 });
  if (q.isError) {
    if (q.error instanceof ApiError && (q.error.status === 404 || q.error.status === 405)) {
      return <Empty title="Readiness isn't available yet">This control plane doesn't report repo readiness. It appears once repo onboarding is deployed.</Empty>;
    }
    return <ErrorBanner error={q.error} what="repo readiness" retry={() => void q.refetch()} />;
  }
  if (!q.data) return <div><div className="skel-line w40" /><div className="skel-line w70" /></div>;
  if (!q.data.repos.length) return <Empty title="No repos">This project has no repositories yet.</Empty>;
  return (
    <div className="ready" aria-label="Readiness">
      <p className="t2 small">Replicant reads each repo's CI, version files and Claude Code setup, works out how to install, build and test it, and runs a quick check in the sandbox. Jobs can't start on a blocked repo unless the project allows it under Triggers.</p>
      {q.data.repos.map((r) => <RepoCard key={r.repo_id} projectId={projectId} r={r} canRecheck={canRecheck} canRequest={canRequest} />)}
    </div>
  );
}

function RepoCard({ projectId, r, canRecheck, canRequest }: { projectId: string; r: ReadinessRepo; canRecheck: boolean; canRequest: boolean }) {
  const { user } = useSession();
  const qc = useQueryClient();
  const b = readinessBadge(r.status);
  const env = r.environment;
  const [recheck, setRecheck] = useState<{ job_id?: string; error?: unknown } | null>(null);
  const [ask, setAsk] = useState<ReadinessWarning | null>(null);
  const [asked, setAsked] = useState<Set<string>>(new Set());
  const doRecheck = async () => {
    setRecheck(null);
    try {
      const res = await recheckRepo(projectId, r.repo_id);
      setRecheck({ job_id: res.data.job_id });
      await qc.invalidateQueries({ queryKey: ["project-readiness", user, projectId] });
    } catch (e) { setRecheck({ error: e }); }
  };
  return (
    <section className="readycard" aria-label={`Readiness of ${r.full_name}`}>
      <div className="section-h">
        <h3 className="mono">{r.full_name}</h3>
        <span aria-label="Readiness status"><Pill s={b} /></span>
        <span className="t2 small">{b.plain}</span>
        {canRecheck ? <span className="aside"><button className="btn sm" onClick={() => void doRecheck()}>Re-check</button></span> : null}
      </div>
      {recheck?.job_id ? <p className="small">Check started: <Link to={`/jobs/${recheck.job_id}`} className="mono">{recheck.job_id.slice(0, 14)}…</Link>. The status updates when it finishes.</p> : null}
      {recheck?.error ? <ErrorBanner error={recheck.error} what="the re-check" /> : null}
      <dl className="kv">
        <dt>Environment</dt>
        <dd aria-label="Environment">{env ? <>
          <b>{env.environment}</b>{env.version ? <span className="t2"> · {env.version}</span> : null}
          {env.supported === false ? <div className="err-t small">Not supported{env.unsupported_reason ? `: ${env.unsupported_reason}` : ""}</div> : null}
        </> : <span className="t3">not worked out yet</span>}
          {r.sha ? <span className="t3 small"> · profile at <span className="mono">{r.sha.slice(0, 10)}</span></span> : null}</dd>
        <dt>Commands</dt>
        <dd>{env ? (
          <table className="t compact" aria-label="Commands">
            <thead><tr><th>Step</th><th>Command</th><th>Where it came from</th></tr></thead>
            <tbody>{ENV_COMMANDS.map((k) => {
              const c = env.commands?.[k];
              return <tr key={k}><td>{k}</td><td className="mono small">{c ?? <span className="t3">none</span>}</td><td className="small t2">{c ? sourceWord(env.source?.[k]) : "—"}</td></tr>;
            })}</tbody>
          </table>) : <span className="t3">—</span>}</dd>
        <dt>Claude Code setup</dt>
        <dd aria-label="Repo configuration"><RepoConfigLines rc={r.repo_config} /></dd>
        <dt>Last check</dt>
        <dd aria-label="Last check">{r.last_probe ? <>
          <span>{r.last_probe.status}</span> <span className="t3 small">· {dateTime(r.last_probe.at)} · </span>
          <Link to={`/jobs/${r.last_probe.job_id}`} className="mono small">{r.last_probe.job_id.slice(0, 14)}…</Link>
          {r.last_probe.causes?.length ? <ul className="plain small">{r.last_probe.causes.map((c, i) => (
            <li key={i}><span className={`pill small ${classBadge(c.cause).family}`}>{classBadge(c.cause).label}</span>{c.stage ? <span className="t2"> during {c.stage}</span> : null}{c.detail ? <span className="mono t2"> · {c.detail}</span> : null}</li>))}</ul> : null}
        </> : <span className="t3">never checked</span>}</dd>
      </dl>
      <h4 className="cfgh">Warnings</h4>
      {r.warnings?.length ? (
        <ul className="plain" aria-label="Warnings">
          {r.warnings.map((w) => {
            const key = w.item_id ?? w.identity;
            const cb = classBadge(w.class);
            return (
              <li key={w.kind + w.identity} className="readywarn">
                <span className={`pill small ${cb.family}`}>{cb.label}</span>
                <span>{w.message}</span>
                <span className="mono t3 small">{w.identity}</span>
                {canRequest ? (asked.has(key) ? <span className="t3 small">support requested</span>
                  : <button className="btn sm" onClick={() => setAsk(w)}>Request support</button>) : null}
              </li>
            );
          })}
        </ul>
      ) : <p className="small t3">None.</p>}
      {ask ? <RequestDialog projectId={projectId} w={ask} onClose={() => setAsk(null)}
        onDone={() => { setAsked(new Set([...asked, ask.item_id ?? ask.identity])); setAsk(null); }} /> : null}
    </section>
  );
}

function RepoConfigLines({ rc }: { rc?: RepoConfigSummary | null }) {
  if (!rc) return <span className="t3">not scanned yet</span>;
  const list = (label: string, xs?: string[]) => xs?.length ? <div className="small"><span className="t2">{label}:</span> {xs.map((x) => <span key={x} className="tag mono">{x}</span>)}</div> : null;
  const empty = !rc.settings_present && !rc.hooks?.length && !rc.rules?.length && !rc.skills?.length && !rc.claude_md?.length && !rc.commands?.length && !rc.agents?.length;
  if (empty) return <span className="t3 small">No Claude Code configuration in this repo.</span>;
  return (
    <div>
      {list("CLAUDE.md files", rc.claude_md)}
      {list("Rules", rc.rules)}
      {list("Skills", rc.skills)}
      {list("Commands", rc.commands)}
      {list("Agents", rc.agents)}
      {rc.hooks?.length ? <div className="small"><span className="t2">Hooks:</span> {rc.hooks.map((h, i) => <span key={i} className="tag mono" title={h.command}>{h.event}{h.matcher ? ` (${h.matcher})` : ""}</span>)}</div> : null}
      {rc.permission_rules ? <div className="small t3">{rc.permission_rules} permission rule{rc.permission_rules === 1 ? "" : "s"} recorded but not applied: the platform's own rules always apply.</div> : null}
    </div>
  );
}

function RequestDialog({ projectId, w, onClose, onDone }: { projectId: string; w: ReadinessWarning; onClose: () => void; onDone: () => void }) {
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const send = async () => {
    setBusy(true); setErr(null);
    try { await requestSupport(projectId, w.item_id ?? w.identity, reason.trim()); onDone(); } catch (e) { setErr(e); } finally { setBusy(false); }
  };
  return (
    <Dialog label="Request support" title="Request support" sub="Replicant admins see this in their fleet queue, with your reason." onClose={onClose}
      foot={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!reason.trim() || busy} onClick={() => void send()}>Send request</button></>}>
      <p className="small"><span className={`pill small ${classBadge(w.class).family}`}>{classBadge(w.class).label}</span> {w.message}</p>
      <p className="small mono t3">{w.identity}</p>
      <label className="small t2" style={{ display: "block", marginTop: 12 }}>Why does your team need this? (required)
        <textarea className="field" rows={3} aria-label="Support reason" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. our tests need this MCP server to read the schema" /></label>
      {err ? <ErrorBanner error={err} what="the request" /> : null}
    </Dialog>
  );
}
