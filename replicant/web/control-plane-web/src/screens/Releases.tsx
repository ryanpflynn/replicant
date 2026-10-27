import { useQuery } from "@tanstack/react-query";
import { TopBar } from "../components/Shell";
import { Glyph } from "../components/Glyph";
import { Empty, ErrorBanner, Id, Pill, SkeletonRows } from "../components/bits";
import { Forbidden } from "./Admin";
import { ApiError, get } from "../lib/api";
import { useSession } from "../lib/session";
import { dateTime } from "../lib/format";
import { bundleStatus } from "../lib/status";
import { useRouter } from "../router";

interface BundleRow {
  release_bundle_id: string; display_version: string; promotion_state: string; worker_build_id: string;
  assignment?: { role: string }; jobs_running?: number; jobs_waiting?: number; failure_rate_24h?: number; terminal_24h?: number;
  created_at?: string; base_release_bundle_id?: string;
}
interface WorkerRow {
  deployment_name: string; build_id: string; routing_status: string; pinned_workflows?: number; bundles?: string[];
  registered_at?: string; ready_at?: string; image_digest?: string; oldest_pinned_started_at?: string;
}
interface Assignment { env: string; version: number; default: string; rollback_target?: string; ramps: unknown[]; tenant_overrides: Record<string, unknown> }
interface BundleDoc {
  release_bundle_id: string; promotion_state: string; required_tier: string; change_classes: string[];
  document: { metadata: Record<string, string>; spec: { agents?: Array<{ ref: string }>; graphs?: Record<string, { ref: string }>; policies?: Array<{ ref: string }>;
    models?: unknown[]; worker_deployment?: { build_id?: string; image_digest?: string } } };
}

const ROUTING: Record<string, { g: string; f: "success" | "info" | "neutral" | "warning" }> = {
  current: { g: "dot", f: "success" }, ramping: { g: "ring-spin", f: "info" }, draining: { g: "ring-half", f: "warning" },
  drained: { g: "dot-hollow", f: "neutral" }, inactive: { g: "dot-hollow", f: "neutral" },
};

function BundlePanel({ rb, a }: { rb: string; a?: Assignment }) {
  const { user } = useSession();
  const q = useQuery({ queryKey: ["bundle", user, rb], queryFn: () => get<BundleDoc>(`/v1/registry/bundles/${rb}`) });
  const d = q.data;
  const spec = d?.document.spec;
  return (
    <aside className="side">
      <div className="section">
        <div className="caps" style={{ marginBottom: 4 }}>Bundle</div>
        <div className="row g1"><Id id={rb} full /></div>
        {d ? <p className="small t2" style={{ marginTop: 4 }}>{d.document.metadata.display_version} · {d.document.metadata.change_summary}</p> : null}
      </div>
      {q.isError ? <ErrorBanner error={q.error} what="the bundle" retry={() => void q.refetch()} /> : null}
      {d ? (
        <>
          <div className="section">
            <dl className="kv">
              <dt>State</dt><dd><Pill s={bundleStatus(d.promotion_state)} /></dd>
              <dt>Assignment</dt><dd>{a?.default === rb ? <>default for <span className="mono">{a.env}</span> <span className="t3 small">v{a.version}</span></> : "not assigned"}</dd>
              <dt>Rollback</dt><dd className="small">{a?.rollback_target ? (a.rollback_target === rb ? <span className="t2">itself (no earlier bundle)</span> : <span className="mono">{a.rollback_target.slice(0, 13)}</span>) : "—"}</dd>
              <dt>Eval tier</dt><dd className="mono">{d.required_tier}</dd>
              <dt>Created by</dt><dd className="mono small">{d.document.metadata.created_by}</dd>
              <dt>Created</dt><dd className="t2">{dateTime(d.document.metadata.created_at)}</dd>
              <dt>Worker</dt><dd className="mono" style={{ overflowWrap: "anywhere" }}>{spec?.worker_deployment?.build_id ?? "—"}</dd>
            </dl>
          </div>
          <div className="section">
            <div className="section-h"><h3>Contents</h3><span className="t3 small">pinned definition versions</span></div>
            <ul className="chg">
              {(spec?.agents ?? []).map((x) => <li key={x.ref}><span className="k">A</span><span>{x.ref}</span></li>)}
              {Object.entries(spec?.graphs ?? {}).map(([k, x]) => <li key={k}><span className="k">G</span><span>{x.ref}</span></li>)}
              {(spec?.policies ?? []).map((x) => <li key={x.ref}><span className="k">P</span><span>{x.ref}</span></li>)}
            </ul>
            <p className="small t3" style={{ marginTop: 6 }}>A agent · G graph · P policy. {spec?.models?.length ? "" : "No model pins (the gateway shim answers locally)."}</p>
          </div>
        </>
      ) : q.isLoading ? <div className="section"><div className="skel-line w70" /><div className="skel-line w40" /></div> : null}
    </aside>
  );
}

export function Releases() {
  const { user } = useSession();
  const { loc, navigate } = useRouter();
  const bundles = useQuery({ queryKey: ["bundles", user], refetchInterval: 15000, queryFn: () => get<{ items: BundleRow[] }>("/v1/releases/bundles?include_derived=true") });
  const workers = useQuery({ queryKey: ["workers", user], refetchInterval: 15000, queryFn: () => get<{ items: WorkerRow[] }>("/v1/releases/workers") });
  const assignment = useQuery({ queryKey: ["assignment", user], queryFn: () => get<Assignment>("/v1/releases/local/assignment") });
  if (bundles.error instanceof ApiError && bundles.error.status === 403) {
    return <><TopBar><b>Releases</b></TopBar><main className="main"><Forbidden what="releases" perm="release:read" error={bundles.error} /></main></>;
  }
  const items = bundles.data?.items ?? [];
  const sel = loc.search.get("rb") ?? assignment.data?.default ?? items[0]?.release_bundle_id;
  return (
    <>
      <TopBar><b>Releases</b><span className="sep">/</span><span>Bundles</span><span className="t3">· local · read-only</span></TopBar>
      <main className="main">
        <div className="rel">
          <div>
            {bundles.isError ? <ErrorBanner error={bundles.error} what="bundles" retry={() => void bundles.refetch()} /> : null}
            <table className="t" aria-label="Release bundles">
              <thead><tr><th>Bundle</th><th className="p2">Version</th><th>State</th><th>Assignment</th><th className="right">Running</th><th className="right">Waiting</th><th className="right">Failed 24h</th></tr></thead>
              <tbody>
                {!bundles.data && !bundles.isError ? <SkeletonRows cols={[110, 80, 90, 70, 20, 20, 30]} rows={3} /> : null}
                {items.map((b) => (
                  <tr key={b.release_bundle_id} className={b.release_bundle_id === sel ? "sel" : ""} onClick={() => navigate(`/releases?rb=${b.release_bundle_id}`, { replace: true })} style={{ cursor: "pointer" }}>
                    <td><span className="row g1"><Glyph g={bundleStatus(b.promotion_state).glyph} family={bundleStatus(b.promotion_state).family} /><Id id={b.release_bundle_id} /></span></td>
                    <td className="mono small p2">{b.display_version}</td>
                    <td><Pill s={bundleStatus(b.promotion_state)} /></td>
                    <td>{b.assignment?.role === "default" ? "default" : b.assignment?.role === "rollback_target" ? "rollback target" : <span className="t3">—</span>}</td>
                    <td className="right num">{b.jobs_running ?? 0}</td>
                    <td className="right num">{b.jobs_waiting ?? 0}</td>
                    <td className="right num" title={b.terminal_24h ? `${b.terminal_24h} jobs ended in 24 h` : undefined}>{b.failure_rate_24h != null ? `${(b.failure_rate_24h * 100).toFixed(1)}%` : <span className="t3">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {bundles.data && !items.length ? <Empty>No bundles yet. Build one: <span className="mono">replicant dev seed</span> registers the local dev bundle.</Empty> : null}
            <div className="section">
              <div className="section-h"><h3>Assignment</h3><span className="t3 small">which bundle new jobs pin to</span></div>
              {assignment.data ? (
                <dl className="kv">
                  <dt>Environment</dt><dd className="mono">{assignment.data.env} <span className="t3">v{assignment.data.version}</span></dd>
                  <dt>Default</dt><dd><Id id={assignment.data.default} full /></dd>
                  <dt>Ramps</dt><dd className="t2">{assignment.data.ramps.length ? `${assignment.data.ramps.length} ramp rows` : "none: no canary, experiment or holdback is running"}</dd>
                  <dt>Overrides</dt><dd className="t2">{Object.keys(assignment.data.tenant_overrides).length || "no tenant overrides"}</dd>
                </dl>
              ) : assignment.isError ? <ErrorBanner error={assignment.error} what="the assignment" /> : <div className="skel-line w40" />}
            </div>
            <div className="section">
              <div className="section-h"><h3>Worker deployment</h3><span className="mono small t2">replicant-worker</span>
                <span className="t3 small">routing from the registry (DescribeWorkerDeployment not queried in v0)</span></div>
              <table className="t" aria-label="Worker versions">
                <thead><tr><th>Build</th><th>Status</th><th className="right">Pinned</th><th>Bundles</th><th className="p2">Ready</th><th className="p3">Image</th></tr></thead>
                <tbody>
                  {(workers.data?.items ?? []).map((w) => {
                    const r = ROUTING[w.routing_status] ?? ROUTING.inactive;
                    return (
                      <tr key={w.deployment_name + w.build_id}>
                        <td className="mono">{w.build_id}</td>
                        <td><span className="row g1"><Glyph g={r.g} family={r.f} />{w.routing_status}</span></td>
                        <td className="right num">{w.pinned_workflows ?? 0}</td>
                        <td className="mono small">{(w.bundles ?? []).map((b) => b.slice(0, 13)).join(", ") || "—"}</td>
                        <td className="p2 t2">{dateTime(w.ready_at)}</td>
                        <td className="p3 mono small t3" title={w.image_digest}>{w.image_digest?.slice(0, 19)}…</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {workers.isError ? <ErrorBanner error={workers.error} what="worker versions" /> : null}
            </div>
          </div>
          {sel ? <BundlePanel rb={sel} a={assignment.data} /> : <aside className="side" />}
        </div>
      </main>
    </>
  );
}
