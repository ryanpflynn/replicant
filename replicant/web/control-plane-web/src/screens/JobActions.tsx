// Steer, pause/resume, cancel (ui/job-actions.md): Updates through the outbox; every action shows submitting
// ("Recording…"), result, or recording (202) states.
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Dialog } from "../components/Dialog";
import { Glyph } from "../components/Glyph";
import { Kbd } from "../components/bits";
import { ApiError, post } from "../lib/api";
import { newId } from "../lib/ids";
import type { JobDetail } from "../lib/types";

type Result = { tone: "ok" | "info" | "err"; text: string } | null;

const CONTROL_WORDS: Record<string, string> = {
  accepted: "Recorded.", no_op: "Nothing to do: the job is already in that state.", denied: "Denied",
};

export function useControl(jobId: string) {
  const qc = useQueryClient();
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<Result>(null);
  const run = async (action: "cancel" | "pause" | "resume", body: Record<string, string> = {}) => {
    setBusy(action);
    setResult(null);
    try {
      const r = await post<{ outcome?: string; status?: string; denial_reason?: string }>(
        `/v1/jobs/${jobId}:${action}`, { action_id: newId("act"), ...body });
      if (r.status === 202) setResult({ tone: "info", text: "Still recording; you can leave this page. The outcome appears on the timeline." });
      else {
        const o = r.data.outcome ?? "accepted";
        setResult({ tone: o === "denied" ? "err" : "ok",
          text: `${action === "cancel" ? "Cancel" : action === "pause" ? "Pause" : "Resume"}: ${CONTROL_WORDS[o] ?? o}${r.data.denial_reason ? ` (${r.data.denial_reason.replace(/_/g, " ")})` : ""}${r.data.status ? ` Status: ${r.data.status.replace(/_/g, " ")}.` : ""}` });
      }
    } catch (e) {
      const err = e instanceof ApiError ? e : new ApiError(0, {});
      setResult({ tone: "err", text: `${err.title}${err.detail ? `: ${err.detail}` : ""}` });
    } finally {
      setBusy(null);
      void qc.invalidateQueries({ queryKey: ["job"] });
      void qc.invalidateQueries({ queryKey: ["timeline"] });
      void qc.invalidateQueries({ queryKey: ["jobs"] });
    }
  };
  return { busy, result, setResult, run };
}

export function CancelDialog({ job, onClose, onConfirm, busy }: { job: JobDetail; onClose: () => void; onConfirm: (reason: string) => void; busy: boolean }) {
  const [reason, setReason] = useState("");
  const s = job.snapshot;
  const live = job.live_run;
  const pr = s.pull_request;
  const openGates = (s.gates ?? []).filter((g) => g.state === "open");
  return (
    <Dialog label="Cancel job" title={<>Cancel <span className="mono">{s.job_id.slice(0, 14)}</span>?</>} onClose={onClose}
      foot={<><span className="grow" /><button className="btn" onClick={onClose}>Keep running</button>
        <button className="btn danger-solid" disabled={busy} onClick={() => onConfirm(reason)} data-testid="confirm-cancel">{busy ? <><Glyph g="ring-spin" />Recording…</> : "Cancel job"}</button></>}>
      <ul className="consequences">
        <li>{live ? <>The running <b>{live.node_key}</b> run (<span className="mono">{live.run_id.slice(0, 14)}…</span>) is cancelled; partial artifacts are kept.</> : "No run is active; the workflow stops before the next node."}</li>
        <li>{pr && pr.state === "open" ? <>PR #{pr.number} is closed with a comment.</> : "No PR is open for this job."}</li>
        <li>{openGates.length ? <>{openGates.length} open gate{openGates.length === 1 ? " is" : "s are"} cancelled in every channel.</> : "No gate is open."}</li>
      </ul>
      <label className="caps" htmlFor="cancel-reason" style={{ display: "block", marginTop: 12, marginBottom: 4 }}>Reason (shown on the issue and in the timeline)</label>
      <textarea id="cancel-reason" className="field" rows={3} value={reason} maxLength={2000} onChange={(e) => setReason(e.target.value)} />
    </Dialog>
  );
}

export function SteerDialog({ job, onClose }: { job: JobDetail; onClose: () => void }) {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const [delivery, setDelivery] = useState<"auto" | "next_node" | "live_only">("auto");
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<Result>(null);
  const [mid] = useState(() => newId("msg"));
  const live = job.live_run;
  const send = async () => {
    if (!text.trim()) return;
    setBusy(true);
    try {
      const r = await post<{ outcome?: string; run_id?: string; denial_reason?: string }>(`/v1/jobs/${job.snapshot.job_id}/steering`,
        { message_id: mid, text: text.trim(), delivery });
      const o = r.status === 202 ? "recording" : r.data.outcome;
      setRes({ tone: o === "denied" || o === "not_deliverable" ? "err" : "ok", text:
        o === "delivered_live" ? `Delivered live to ${r.data.run_id ?? "the active run"}.` :
        o === "queued_next_node" ? "Queued: it will be included in the next node's inputs." :
        o === "not_deliverable" ? "No live run accepts messages now; nothing was sent." :
        o === "recording" ? "Still recording; the outcome appears on the timeline." :
        `Denied: ${r.data.denial_reason ?? "not allowed"}` });
      void qc.invalidateQueries({ queryKey: ["timeline"] });
    } catch (e) {
      const err = e instanceof ApiError ? e : new ApiError(0, {});
      setRes({ tone: "err", text: `${err.title}${err.detail ? `: ${err.detail}` : ""}` });
    } finally { setBusy(false); }
  };
  const opt = (k: typeof delivery, label: string, sub: string) => (
    <div className={"opt" + (delivery === k ? " on" : "")} onClick={() => setDelivery(k)} role="radio" aria-checked={delivery === k} tabIndex={0}
      onKeyDown={(e) => { if (e.key === " " || e.key === "Enter") setDelivery(k); }}>
      <span className={"radio" + (delivery === k ? " on" : "")} /><span><b>{label}</b><br /><span className="t3 small">{sub}</span></span>
    </div>
  );
  return (
    <Dialog label="Steer job" title={<>Steer <span className="mono">{job.snapshot.job_id.slice(0, 14)}</span></>}
      sub="Sent to the agent as a user message, not as instructions to Replicant. Visible to everyone who can see the job." onClose={onClose}
      foot={<>{res ? <span className={res.tone === "err" ? "err-t small" : "t2 small"} role="status">{res.text}</span> : <span className="t3 small">16,000 chars max</span>}
        <span className="grow" /><button className="btn" onClick={onClose}>{res ? "Close" : "Cancel"}</button>
        {!res ? <button className="btn primary" disabled={busy || !text.trim()} onClick={() => void send()}>{busy ? <><Glyph g="ring-spin" />Recording…</> : <>Send<Kbd>⌘↵</Kbd></>}</button> : null}</>}>
      <textarea className="field" rows={4} value={text} maxLength={16000} autoFocus onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void send(); }} aria-label="Steering message" />
      <fieldset className="opts" role="radiogroup">
        <legend className="caps">Delivery</legend>
        {opt("auto", "Auto", live ? `Live to the ${live.node_key} run if it accepts steering, else the next node` : "No run is active: queued for the next node")}
        {opt("next_node", "Next node only", "Always queued into the next node's inputs")}
        {opt("live_only", "Live only", "Fails with nothing sent if no live run accepts messages")}
      </fieldset>
    </Dialog>
  );
}
