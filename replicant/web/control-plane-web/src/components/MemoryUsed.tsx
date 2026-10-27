// "Memory used" (MEM): the entries injected into a job's runs, read-only for anyone who can read the job
// (GET /v1/jobs/{id}/memory: statement, type, scope; no author identity or scores).
import { useQuery } from "@tanstack/react-query";
import { get } from "../lib/api";

export interface JobMemory {
  job_id: string; available: boolean; memory_snapshot_id?: string | null;
  runs: Array<{ run_id: string; role: string; node_key: string; refs: string[] }>;
  entries: Array<{ ref: string; memory_type: string; scope: string; title?: string | null; statement?: string | null; purged?: boolean }>;
}

export function useJobMemory(jobId: string | undefined) {
  return useQuery({ queryKey: ["job-memory", jobId], enabled: !!jobId, staleTime: 15_000,
    queryFn: () => get<JobMemory>(`/v1/jobs/${jobId}/memory`) });
}

export function MemoryUsed({ jobId, snapshotId }: { jobId: string; snapshotId?: string | null }) {
  const q = useJobMemory(jobId);
  const d = q.data;
  const snap = d?.memory_snapshot_id ?? snapshotId;
  return (
    <div className="section" aria-label="Memory used">
      <div className="section-h"><h3>Memory used</h3><span className="t3 small">read-only</span></div>
      {q.isLoading ? <p className="small t3">Loading…</p> : !d?.available ? <p className="small t3">Memory is unavailable right now{snap ? <> (snapshot <span className="mono">{snap.slice(0, 16)}…</span>)</> : null}.</p>
        : !d.entries.length ? <p className="small t3">No entries applied to this job's runs{snap ? <> (snapshot <span className="mono">{snap.slice(0, 16)}…</span>)</> : null}.</p>
        : <>
          <ul className="memlist" style={{ paddingLeft: 16, margin: 0 }}>
            {d.entries.map((e) => (
              <li key={e.ref} className="small" style={{ marginBottom: 6 }}>
                <span className="t3">{e.memory_type} · {e.scope}</span><br />
                {e.purged ? <i className="t3">removed by an admin</i> : <>{e.title ? <b>{e.title}. </b> : null}{e.statement}</>}
              </li>))}
          </ul>
          <p className="small t3" style={{ marginTop: 4 }}>{d.runs.filter((r) => r.refs.length).length} of {d.runs.length} runs received memory · snapshot <span className="mono">{(snap ?? "").slice(0, 16)}…</span></p>
        </>}
    </div>
  );
}

/** One line for the Simple view. */
export function MemoryLine({ jobId }: { jobId: string }) {
  const d = useJobMemory(jobId).data;
  if (!d?.available || !d.entries.length) return null;
  return <p className="small t2" aria-label="Memory used">Used {d.entries.length} team convention{d.entries.length === 1 ? "" : "s"} from memory: {d.entries.slice(0, 2).map((e) => e.title ?? e.statement?.slice(0, 60)).join("; ")}{d.entries.length > 2 ? "; …" : ""}</p>;
}
