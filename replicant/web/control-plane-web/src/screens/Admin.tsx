import { useQuery } from "@tanstack/react-query";
import { TopBar } from "../components/Shell";
import { Glyph } from "../components/Glyph";
import { ErrorBanner, Ext, SkeletonRows } from "../components/bits";
import { ApiError, get } from "../lib/api";
import { hasCap, useMe, useSession } from "../lib/session";
import { dateTime, duration, parseTs } from "../lib/format";
import { grafanaContainersUrl, LINKS } from "../lib/config";
import { Link } from "../router";

export function Forbidden({ what, perm, error, children }: { what: string; perm: string; error?: ApiError; children?: React.ReactNode }) {
  const me = useMe().data;
  return (
    <div className="denied">
      <div className="caps">403 · {what}</div>
      <h1>{children ? "This page is limited to replicant admins" : `You can't open ${what}`}</h1>
      <p className="t2">{children ?? <>This page needs the permission <span className="mono">{perm}</span>, and that is checked on every request. Your account, {me?.display_name ?? "…"}, does not hold it.</>}</p>
      <p className="t2" style={{ marginTop: 10 }}>You can still see every job you have access to under <Link to="/jobs">Jobs</Link>, and the gates you can decide under <Link to="/approvals">Approvals</Link>.</p>
      <div className="codeblock">{`request   GET ${location.pathname}
decision  deny · permission ${perm}${error ? ` · ${error.code}` : ""}
${error?.requestId ? `request   ${error.requestId}\n` : ""}principal ${me?.principal_id ?? "…"}`}</div>
      <p className="t3 small" style={{ marginTop: 10 }}>Locally, roles come from the static dev users; switch user from the menu at the bottom of the rail. In production, group membership is managed in Okta, not here.</p>
    </div>
  );
}

interface KillSwitch { scope: string; scope_value: string; intake: string; starts: string; runs: string; publishing: string; reason_code: string; note?: string; set_by?: string; set_at?: string }
interface Migrations { current_revision: string; last_runs: Array<{ revision: string; started_at: string; finished_at?: string; outcome: string }> }

const INFRA = [
  { name: "Grafana", href: LINKS.grafana + "/d/replicant-overview", what: "Replicant overview: jobs, gates, runs, gateway usage", port: "3001" },
  { name: "Containers dashboard", href: grafanaContainersUrl(), what: "CPU, memory, restarts for every container, sandboxes included", port: "3001" },
  { name: "Temporal UI", href: `${LINKS.temporalUi}/namespaces/${LINKS.temporalNamespace}/workflows`, what: "ReplicantJob workflows, history, pending activities", port: "8233" },
  { name: "Gitea", href: LINKS.gitea + "/acme-payments/ledger-api", what: "Local git host: issues that trigger jobs, replicant PRs", port: "3300" },
  { name: "Prometheus", href: LINKS.prometheus + "/targets", what: "Scrape targets and raw metrics", port: "9090" },
];

const modeCell = (v: string) => v === "open" ? <span className="t3">open</span>
  : <span className="row g1"><Glyph g="bars" family="warning" /><b>{v}</b></span>;

export function Admin() {
  const { user } = useSession();
  const me = useMe().data;
  const tenants = useQuery({ queryKey: ["tenants", user], queryFn: () => get<{ items: Array<{ tenant_id: string; display_name: string; status: string }> }>("/v1/tenants") });
  const ks = useQuery({ queryKey: ["kill", user], refetchInterval: 10000, queryFn: () => get<{ items: KillSwitch[] }>("/v1/admin/kill-switches") });
  const mig = useQuery({ queryKey: ["migrations", user], queryFn: () => get<Migrations>("/v1/admin/migrations") });
  if (me && !hasCap(me, "admin:platform") && !hasCap(me, "identity:admin")) {
    return <><TopBar><b>Admin</b></TopBar><main className="main"><Forbidden what="platform administration" perm="admin:platform" /></main></>;
  }
  const engaged = (ks.data?.items ?? []).filter((k) => [k.intake, k.starts, k.runs, k.publishing].some((m) => m !== "open"));
  return (
    <>
      <TopBar><b>Admin</b><span className="t3">· platform · local</span></TopBar>
      <main className="main">
        {engaged.some((k) => k.scope === "global" || k.scope === "tenant") ? (
          <div className="banner warning" role="status"><Glyph g="triangle" style={{ marginTop: 3 }} /><span className="t"><b>A kill switch is engaged.</b> Work in its scope is paused until it is released.</span></div>
        ) : null}
        <div className="admin-grid">
          <div>
            <div className="section">
              <div className="section-h"><h3>Tenants</h3><span className="t3 small">{tenants.data ? `${tenants.data.items.length} tenants` : ""}</span></div>
              {tenants.isError ? <ErrorBanner error={tenants.error} what="tenants" retry={() => void tenants.refetch()} /> : null}
              <table className="t" aria-label="Tenants">
                <thead><tr><th>Tenant</th><th>Name</th><th>Status</th></tr></thead>
                <tbody>
                  {!tenants.data && !tenants.isError ? <SkeletonRows cols={[80, 140, 50]} rows={3} /> : null}
                  {tenants.data?.items.map((t) => (
                    <tr key={t.tenant_id}><td className="mono">{t.tenant_id}</td><td>{t.display_name}</td>
                      <td><span className="row g1"><Glyph g={t.status === "active" ? "dot" : "dot-hollow"} family={t.status === "active" ? "success" : "neutral"} />{t.status}</span></td></tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="section">
              <div className="section-h"><h3>Kill switches</h3><span className="t3 small">scopes global, tenant, repo, bundle · modes intake, starts, runs, publishing</span></div>
              {ks.isError ? <ErrorBanner error={ks.error} what="kill switches" retry={() => void ks.refetch()} /> : null}
              {ks.data && ks.data.items.length === 0 ? (
                <p className="t2">No kill switch has been set. Intake, starts, runs and publishing are open everywhere. <span className="t3">Engaging one from the UI is not built in v0; the API's write endpoint is not implemented yet.</span></p>
              ) : null}
              {ks.data && ks.data.items.length ? (
                <table className="t" aria-label="Kill switches">
                  <thead><tr><th>Scope</th><th>Intake</th><th>Starts</th><th>Runs</th><th>Publishing</th><th>Reason</th><th>Set</th></tr></thead>
                  <tbody>{ks.data.items.map((k) => (
                    <tr key={k.scope + k.scope_value}><td><span className="mono">{k.scope}:{k.scope_value}</span></td><td>{modeCell(k.intake)}</td><td>{modeCell(k.starts)}</td>
                      <td>{modeCell(k.runs)}</td><td>{modeCell(k.publishing)}</td><td className="mono small">{k.reason_code}</td><td className="t2">{dateTime(k.set_at)}</td></tr>
                  ))}</tbody>
                </table>
              ) : null}
            </div>
            <div className="section">
              <div className="section-h"><h3>Database</h3><span className="t3 small">migrations (read-only; no screen offers SQL)</span></div>
              {mig.isError ? <ErrorBanner error={mig.error} what="migration status" retry={() => void mig.refetch()} /> : null}
              {mig.data ? (
                <>
                  <dl className="kv">
                    <dt>Schema revision</dt><dd className="mono">{mig.data.current_revision || "—"} <span className="t3">(head)</span></dd>
                    <dt>Pending</dt><dd>none</dd>
                  </dl>
                  <table className="t" style={{ marginTop: 10 }} aria-label="Migration runs">
                    <thead><tr><th>Revision</th><th>Started</th><th className="right">Duration</th><th>Outcome</th></tr></thead>
                    <tbody>{mig.data.last_runs.slice(0, 5).map((r, i) => (
                      <tr key={i}><td className="mono small">{r.revision}</td><td className="t2">{dateTime(r.started_at)}</td>
                        <td className="right num">{r.finished_at ? duration((parseTs(r.finished_at) ?? 0) - (parseTs(r.started_at) ?? 0)) : "—"}</td>
                        <td><span className="row g1"><Glyph g={r.outcome === "succeeded" ? "check" : "cross"} family={r.outcome === "succeeded" ? "success" : "danger"} />{r.outcome}</span></td></tr>
                    ))}</tbody>
                  </table>
                </>
              ) : null}
            </div>
          </div>
          <aside className="side">
            <div className="section links infra">
              <div className="section-h"><h3>Infrastructure</h3><span className="t3 small">local stack, 127.0.0.1</span></div>
              {INFRA.map((l) => (
                <Ext key={l.name} href={l.href}><span className="iname">{l.name}<span className="t3 small">{l.what}</span></span><span className="ext">:{l.port} ↗</span></Ext>
              ))}
            </div>
            <div className="section">
              <div className="section-h"><h3>Not in v0</h3></div>
              <p className="small t2">Access (role bindings), identity links, operations catalogue and audit need identity-service and the ops endpoints, which the v0 stack does not run.</p>
            </div>
          </aside>
        </div>
      </main>
    </>
  );
}
