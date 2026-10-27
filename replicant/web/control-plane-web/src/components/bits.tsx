import { useState, type ReactNode } from "react";
import { Glyph } from "./Glyph";
import { splitId } from "../lib/ids";
import type { StatusStyle } from "../lib/status";
import { ApiError } from "../lib/api";
import { Link } from "../router";

/** IDs as typography: prefix dimmed, first 10 body characters in ink, the rest dimmed; click copies (components.md §2). */
export function Id({ id, full = false, to, headLen = 10 }: { id: string; full?: boolean; to?: string; headLen?: number }) {
  const [copied, setCopied] = useState(false);
  const p = splitId(id, headLen);
  const inner = (
    <>
      <span className="pfx">{p.pfx}</span>{p.head}{full && p.rest ? <span className="rest">{p.rest}</span> : null}
    </>
  );
  if (to) return <Link to={to} className="id mono" title={id} aria-label={id}>{inner}</Link>;
  return (
    <span className="id mono copyable" title={`${id} · click to copy`} aria-label={id} role="button" tabIndex={0}
      onClick={() => { void navigator.clipboard?.writeText(id).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1200); }); }}>
      {inner}{copied ? <span className="copied">copied</span> : null}
    </span>
  );
}

export function Pill({ s, children }: { s: StatusStyle; children?: ReactNode }) {
  return <span className={`pill ${s.family}`}><Glyph g={s.glyph} />{children ?? s.label}</span>;
}

export function Risk({ level }: { level?: string }) {
  if (!level) return <span className="t3">—</span>;
  return <span className={`risk ${level}`}>{level}</span>;
}

export function Avatar({ name, sm }: { name: string; sm?: boolean }) {
  const ini = name.split(/[\s_-]+/).filter(Boolean).slice(0, 2).map((w) => w[0]?.toUpperCase()).join("") || "?";
  return <span className={"avatar" + (sm ? " sm" : "")} aria-hidden="true">{ini}</span>;
}

export function Banner({ kind, glyph, lead, children, acts, role }: {
  kind: "warning" | "danger" | "neutral" | "attention" | "info"; glyph: string; lead: ReactNode; children?: ReactNode;
  acts?: ReactNode; role?: "status" | "alert";
}) {
  return (
    <div className={`banner ${kind}`} role={role ?? (kind === "danger" ? "alert" : "status")}>
      <Glyph g={glyph} style={{ marginTop: 3 }} />
      <span className="t"><b>{lead}</b>{children ? <> {children}</> : null}</span>
      {acts ? <span className="acts">{acts}</span> : null}
    </div>
  );
}

/** Region-scoped API error (components.md §9): problem title, code and request ID, with Retry. */
export function ErrorBanner({ error, what, retry }: { error: unknown; what: string; retry?: () => void }) {
  const e = error instanceof ApiError ? error : new ApiError(0, { code: "client_error", detail: String(error) });
  const rid = e.requestId ? ` · request ${e.requestId.slice(0, 10)}…` : "";
  return (
    <Banner kind="danger" glyph="cross" lead={`Couldn't load ${what}`}
      acts={retry ? <button className="btn sm" onClick={retry}>Retry</button> : undefined}>
      <span className="mono small">({e.status || "network"} · {e.code}{rid})</span>{e.detail ? <span> {e.detail}</span> : null}
    </Banner>
  );
}

/** M1's lag header is "seconds since the job's last projection write" (WEB-06), which also grows while a job
 * simply waits; so it is only trusted for queued/running jobs, and only after 10 minutes without an update. */
export function StaleBanner({ lag, active }: { lag: number; active: boolean }) {
  if (!active || lag <= 600) return null;
  return (
    <Banner kind="warning" glyph="triangle" lead={`No update from this job for ${Math.round(lag / 60)} min.`}>
      The projection may be behind the workflow, or the current run is long and quiet. Data shown is as of the last update.
    </Banner>
  );
}

export function SkeletonRows({ cols, rows = 8 }: { cols: number[]; rows?: number }) {
  return (
    <>
      {Array.from({ length: rows }, (_, i) => (
        <tr key={i} className="skel" aria-hidden="true">
          {cols.map((w, j) => <td key={j}><i style={{ width: w }} /></td>)}
        </tr>
      ))}
    </>
  );
}

export function Empty({ title, children }: { title?: string; children: ReactNode }) {
  return <div className="empty">{title ? <h3>{title}</h3> : null}<p className="t2">{children}</p></div>;
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd>{children}</kbd>;
}

export function Ext({ href, children, className }: { href?: string; children: ReactNode; className?: string }) {
  if (!href) return <span className={className}>{children}</span>;
  return <a href={href} target="_blank" rel="noreferrer noopener" className={className}>{children}</a>;
}

/** Untrusted text: only inside a labelled quote block, plain text, never Markdown (components.md §5, T-PI-04). */
export function Quote({ label, children }: { label: string; children: ReactNode }) {
  return <div className="quote"><span className="untrusted">{label}</span><span className="qt">{children}</span></div>;
}
