import { MemoryUsed } from "../components/MemoryUsed";
import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { TopBar } from "../components/Shell";
import { Glyph } from "../components/Glyph";
import { Banner, Empty, ErrorBanner, Ext, Id, Kbd, Pill, Risk } from "../components/bits";
import { DiffSection, HandoffSection, PlanSection, QASection, ReviewSection, RulesSection } from "../components/Packet";
import { ApiError, get, post } from "../lib/api";
import { newId } from "../lib/ids";
import { personName, useDirectory, useInbox, useSession } from "../lib/session";
import { age, clock, dateTime, hoursUntil, humanize, timeLeft } from "../lib/format";
import { GATE_KIND_SHORT, gateStatus } from "../lib/status";
import { browserGitUrl, repoName, repoShort, repoUrl } from "../lib/config";
import { Link, useRouter } from "../router";
import type { GateDetail, InboxItem, JobDetail } from "../lib/types";

const NOT_DECIDABLE: Record<string, string> = {
  self_approval_forbidden: "You requested this job, and this gate does not let requesters approve their own change.",
  not_in_approver_group: "You can't decide this: you are not in an approver group for this tenant.",
  gate_not_open: "This gate is already closed.",
};

function GateItem({ g, sel, dir }: { g: InboxItem; sel: boolean; dir: Map<string, string> }) {
  const left = g.state === "open" ? timeLeft(g.expires_at) : "";
  const soon = g.state === "open" && hoursUntil(g.expires_at) < 12;
  const st = gateStatus(g.state === "open" ? "open" : g.decision === "approve" ? "approved" : g.decision === "reject" ? "rejected" : g.decision === "request_changes" ? "changes_requested" : g.state);
  return (
    <Link to={`/approvals/${g.gate_id}`} className={"gitem" + (sel ? " sel" : "")} aria-current={sel ? "true" : undefined} data-gate-id={g.gate_id}>
      <span className="gl"><Glyph g={g.state === "open" ? (g.batchable ? "diamond-hollow" : "diamond") : st.glyph} family={st.family} /></span>
      <span className="ttl">{GATE_KIND_SHORT[g.kind] ?? humanize(g.kind)} · {g.title || g.job_id}</span>
      <span className={"age" + (soon ? " soon" : "")} title={g.expires_at ? `Expires ${g.expires_at}` : undefined}>{left || age(g.opened_at)}</span>
      <span className="sub">
        <Risk level={g.risk_level} />
        <span className="mono">{repoShort(g.repo)}</span>
        <span className="hide-md">{personName(dir, g.requester)}</span>
        <span className="chan">{g.channels.map((c) => <i key={c}>{c === "ui" ? "UI" : c === "cli" ? "CLI" : c}</i>)}</span>
        {g.escalation_level ? <span className="esc" title="Escalation level">ESC {g.escalation_level}</span> : null}
      </span>
    </Link>
  );
}

type BarState =
  | { s: "idle" }
  | { s: "submitting"; decision: string }
  | { s: "done"; decision: string; outcome: string; at: string; detail?: string }
  | { s: "recording"; decision: string }
  | { s: "error"; decision: string; error: ApiError };

const DECISION_WORD: Record<string, string> = { approve: "Approved", reject: "Rejected", request_changes: "Changes requested", answer: "Answered" };

export function DecisionBar({ d, onDecided }: { d: GateDetail; onDecided: (decision?: string) => void }) {
  const { user } = useSession();
  const dir = useDirectory();
  const [comment, setComment] = useState("");
  const [bar, setBar] = useState<BarState>({ s: "idle" });
  const [need, setNeed] = useState(false);
  const keys = useRef<Record<string, string>>({});
  const ta = useRef<HTMLTextAreaElement>(null);
  const g = d.gate;
  useEffect(() => { setBar({ s: "idle" }); setComment(""); setNeed(false); keys.current = {}; }, [g.gate_id, user]);

  const send = async (decision: string) => {
    if ((decision === "reject" || decision === "request_changes") && !comment.trim()) {
      setNeed(true); ta.current?.focus(); return;
    }
    if (bar.s === "submitting") return;
    keys.current[decision] ??= newId("idem");          // a double click re-sends the same key: same decision_id
    setBar({ s: "submitting", decision });
    try {
      const body: Record<string, string> = { decision, presented_digest: d.packet.presented_digest };
      if (comment.trim()) body.comment = comment.trim();
      const r = await post<{ outcome?: string; state?: string; denial_reason?: string; winning_decision?: { decision?: string } }>(
        `/v1/gates/${g.gate_id}/decisions`, body, keys.current[decision]);
      if (r.status === 202) setBar({ s: "recording", decision });
      else setBar({ s: "done", decision, outcome: r.data.outcome ?? "accepted", at: new Date().toISOString(), detail: r.data.denial_reason });
      onDecided(r.status === 200 && (r.data.outcome ?? "accepted") === "accepted" ? decision : undefined);
    } catch (e) {
      setBar({ s: "error", decision, error: e instanceof ApiError ? e : new ApiError(0, {}) });
      onDecided();
    }
  };

  useEffect(() => {
    if (g.state !== "open" || !d.decidable) return;
    const on = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t.closest("input, textarea") || e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return;
      if (e.key === "a") { e.preventDefault(); void send("approve"); }
      else if (e.key === "r") { e.preventDefault(); void send("request_changes"); }
      else if (e.key === "x") { e.preventDefault(); void send("reject"); }
    };
    addEventListener("keydown", on);
    return () => removeEventListener("keydown", on);
  });

  if (bar.s === "done" || bar.s === "recording") {
    const who = "you in the UI";
    const msg = bar.s === "recording" ? <>Still recording your decision; you can leave this page. The outcome appears here and on the job.</>
      : bar.outcome === "accepted" ? <><b>{DECISION_WORD[bar.decision] ?? bar.decision} by {who}</b> · {clock(bar.at)}{bar.decision === "approve" && g.kind === "publish_approval" ? " · Replicant now opens the PR" : ""}</>
      : bar.outcome === "superseded" ? <><b>Already decided</b> by another approver; nothing else changed.</>
      : bar.outcome === "denied" ? <><b>Denied:</b> {bar.detail?.replace(/_/g, " ") ?? "the authorization check failed"}. The gate stays open.</>
      : <><b>Recorded</b> · more approvals are needed.</>;
    return (
      <div className="decide done" role="status" data-testid="decision-outcome">
        <span className="row g1"><Glyph g={bar.s === "recording" ? "ring-spin" : bar.outcome === "accepted" ? (bar.decision === "approve" ? "check" : bar.decision === "reject" ? "cross" : "return") : "dot-hollow"}
          family={bar.s === "recording" ? "info" : bar.outcome === "accepted" ? (bar.decision === "approve" ? "success" : bar.decision === "reject" ? "danger" : "special") : "neutral"} /><span>{msg}</span></span>
      </div>
    );
  }
  if (g.state !== "open") {
    const dec = g.decision;
    return (
      <div className="decide done" role="status" data-testid="decision-outcome">
        <span className="row g1"><Pill s={gateStatus(g.state)} />
          {dec ? <span>by <b>{personName(dir, dec.principal_id)}</b> in the {dec.channel === "ui" ? "UI" : dec.channel.toUpperCase()} · {dateTime(dec.decided_at)}</span> : null}</span>
        {dec?.comment ? <div className="quote" style={{ gridColumn: "1 / -1" }}><span className="untrusted">approver comment</span>{dec.comment}</div> : null}
      </div>
    );
  }
  const busy = bar.s === "submitting";
  const err = bar.s === "error" ? bar.error : null;
  const errText = err ? (
    err.code === "stale_approval_view" ? "The packet changed since you opened it. Reload the packet and review it again." :
    err.code === "gate_already_decided" ? "Someone else decided this gate first; first decision wins." :
    err.code === "comment_required" ? "A comment is required for Reject and Request changes." :
    `${err.title}${err.detail ? `: ${err.detail}` : ""}`
  ) : null;
  const digest = d.packet.presented_digest;
  const primaryLabel = g.kind === "publish_approval" ? "Approve and publish" : g.kind === "plan_approval" ? "Approve plan" : "Approve";
  return (
    <div className="decide">
      <textarea ref={ta} className={"comment" + (need ? " need" : "")} rows={1} value={comment} disabled={!d.decidable || busy}
        onChange={(e) => { setComment(e.target.value); if (e.target.value.trim()) setNeed(false); }}
        placeholder="Comment (optional; required for Reject and Request changes)" aria-label="Decision comment" />
      <div className="row g3 stamp">
        {need ? <span className="attn-t">A comment is required for this decision.</span>
          : errText ? <span className="err-t" role="alert">{errText} <span className="mono t3">{err?.requestId ? `request ${err.requestId.slice(0, 10)}…` : ""}</span></span>
          : <span>Packet <span className="mono">r{d.packet.packet_revision} · {digest.slice(0, 11)}…{digest.slice(-4)}</span><span className="hide-md"> · also open in the CLI</span> · first decision wins</span>}
      </div>
      <div className="row">
        <button className="btn ghost danger" disabled={!d.decidable || busy} onClick={() => void send("reject")}>
          {busy && bar.decision === "reject" ? <><Glyph g="ring-spin" />Recording…</> : <>Reject<Kbd>X</Kbd></>}</button>
        {g.kind === "plan_approval" || g.kind === "publish_approval" ? (
          <button className="btn" disabled={!d.decidable || busy} onClick={() => void send("request_changes")}>
            {busy && bar.decision === "request_changes" ? <><Glyph g="ring-spin" />Recording…</> : <>Request changes<Kbd>R</Kbd></>}</button>
        ) : null}
        <button className="btn primary" disabled={!d.decidable || busy} onClick={() => void send("approve")} data-testid="approve">
          {busy && bar.decision === "approve" ? <><Glyph g="ring-spin" />Recording…</> : <>{primaryLabel}<Kbd>A</Kbd></>}</button>
      </div>
    </div>
  );
}

function PacketView({ gateId }: { gateId: string }) {
  const { user } = useSession();
  const dir = useDirectory();
  const qc = useQueryClient();
  const [mine, setMine] = useState<string | undefined>();
  const q = useQuery({ queryKey: ["gate", user, gateId], queryFn: () => get<GateDetail>(`/v1/gates/${gateId}`), refetchInterval: 5000 });
  const d = q.data;
  const jobId = d?.packet.content ? (d.packet.packet_key.match(/jobs\/(job_[0-9a-z]+)\//)?.[1]) : d?.packet.packet_key.match(/jobs\/(job_[0-9a-z]+)\//)?.[1];
  const job = useQuery({ queryKey: ["job", user, jobId], enabled: !!jobId, queryFn: () => get<JobDetail>(`/v1/jobs/${jobId}`) });
  if (q.isError) return <section className="packet"><ErrorBanner error={q.error} what="the approval packet" retry={() => void q.refetch()} /></section>;
  if (!d) return <section className="packet" aria-busy="true"><div className="hd"><div className="skel-line w40" /><div className="skel-line w70" /></div></section>;
  const g = d.gate;
  const snap = job.data?.snapshot;
  const repo = snap?.targets?.[0]?.repo ?? "";
  const issueUrl = browserGitUrl(job.data?.source?.url);
  const issueNo = issueUrl?.match(/\/issues\/(\d+)/)?.[1];
  const open = g.state === "open" && !mine;
  const onDecided = (decision?: string) => {
    if (decision) setMine(decision);
    void qc.invalidateQueries({ queryKey: ["approvals"] });
    void qc.invalidateQueries({ queryKey: ["waiting"] });
    void qc.invalidateQueries({ queryKey: ["gate"] });
    void qc.invalidateQueries({ queryKey: ["jobs"] });
    void qc.invalidateQueries({ queryKey: ["job"] });
  };
  return (
    <section className="packet" aria-label="Approval packet">
      <div className="hd">
        <div className="row" style={{ alignItems: "flex-start" }}>
          <div className="grow">
            <div className="caps capsline" style={{ marginBottom: 4 }}>{humanize(g.kind)} · <span className="mono idcase">{g.gate_id}</span> · cycle {snap?.cycle ?? 0} · packet r{d.packet.packet_revision}</div>
            <h2>{snap?.title ?? "…"}</h2>
            <div className="row g3 small t2 metaline">
              {repo ? <Ext href={repoUrl(repo)} className="mono">{repoName(repo)}</Ext> : null}
              {issueNo ? <Ext href={issueUrl} className="mono">#{issueNo}</Ext> : null}
              {jobId ? <Id id={jobId} to={`/jobs/${jobId}`} /> : null}
              <span>requested by {personName(dir, snap?.requester)}</span>
              <span>opened {age(g.opened_at)} ago</span>
              {open && g.expires_at ? <span className="row g1"><Glyph g="hourglass" family="attention" /><b>expires {dateTime(g.expires_at)}</b> · then the job expires</span> : null}
            </div>
          </div>
          {mine ? <Pill s={gateStatus(mine === "approve" ? "approved" : mine === "reject" ? "rejected" : "changes_requested")} /> : open ? (d.decidable ? <span className="pill attention"><Glyph g="diamond" />Waiting on you</span>
            : <span className="pill neutral"><Glyph g="diamond-hollow" />Waiting on {g.eligible?.okta_groups?.join(", ") ?? "approvers"}</span>)
            : <Pill s={gateStatus(g.state)} />}
        </div>
      </div>
      {g.escalation_level ? <Banner kind="warning" glyph="triangle" lead={`Escalated (level ${g.escalation_level}).`}>Reminders went out without a decision.</Banner> : null}
      {open && !d.decidable && d.not_decidable_reason ? (
        <Banner kind="neutral" glyph="dot-hollow" lead="Read-only.">{NOT_DECIDABLE[d.not_decidable_reason] ?? d.not_decidable_reason.replace(/_/g, " ")}
          {" "}Eligible: <span className="mono">{g.eligible?.okta_groups?.join(", ")}</span> with {g.eligible?.min_repo_permission ?? "write"} on the repo.</Banner>
      ) : null}
      {d.packet.resolve_error ? <Banner kind="warning" glyph="triangle" lead="The packet body could not be loaded from storage.">Decisions still work; they are checked against the digest.</Banner> : null}
      <div className="body">
        <div>
          <RulesSection packet={d.packet} />
          <PlanSection packet={d.packet} />
          <DiffSection packet={d.packet} gateId={g.gate_id} />
          <HandoffSection packet={d.packet} />
        </div>
        <aside className="side">
          <QASection packet={d.packet} />
          <ReviewSection packet={d.packet} />
          <div className="section">
            <dl className="kv">
              <dt>Cost</dt><dd><span className="num">${(job.data?.cost.usd_so_far ?? 0).toFixed(2)}</span> <span className="t3 small">{snap?.runs?.length ?? 0} runs</span></dd>
              <dt>Bundle</dt><dd className="mono ellipsis">{snap?.release_bundle_id ? <Id id={snap.release_bundle_id} /> : "—"}</dd>
              <dt>Channels</dt><dd><span className="chan">{(g.channels ?? []).map((c) => <i key={c}>{c.toUpperCase()}</i>)}</span></dd>
              <dt>Eligible</dt><dd className="small"><span className="mono">{g.eligible?.okta_groups?.join(", ") ?? "—"}</span></dd>
            </dl>
          </div>
          {snap?.job_id ? <MemoryUsed jobId={snap.job_id} snapshotId={snap.memory_snapshot_id} /> : null}
        </aside>
      </div>
      <DecisionBar d={d} onDecided={onDecided} />
    </section>
  );
}

export function Approvals({ gateId }: { gateId?: string }) {
  const { loc, navigate } = useRouter();
  const decided = loc.search.get("view") === "decided";
  const mine = useInbox("mine");
  const done = useInbox("decided");
  const list = decided ? done : mine;
  const dir = useDirectory();
  const items = list.data?.items ?? [];
  const selected = gateId ?? items[0]?.gate_id;
  const nOpen = mine.data?.items.length ?? 0;
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t.closest("input, textarea") || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key !== "n" && e.key !== "N" && e.key !== "j" && e.key !== "k") return;
      const i = items.findIndex((x) => x.gate_id === selected);
      const next = e.key === "N" || e.key === "k" ? items[Math.max(0, i - 1)] : items[Math.min(items.length - 1, i + 1)];
      if (next) navigate(`/approvals/${next.gate_id}${decided ? "?view=decided" : ""}`);
    };
    addEventListener("keydown", on);
    return () => removeEventListener("keydown", on);
  }, [items, selected, navigate, decided]);
  return (
    <>
      <TopBar>
        <b>Approvals</b>
        <span className="t3">· {mine.data ? (nOpen ? `${nOpen} waiting on you` : "nothing waiting on you") : "…"}</span>
      </TopBar>
      <main className="main" style={{ overflow: "hidden" }}>
        <div className="inbox">
          <section className="list" aria-label="Gates">
            <div className="filters">
              <button className={"chip" + (!decided ? " on" : "")} aria-pressed={!decided} onClick={() => navigate("/approvals")}>Mine <span className="n">{mine.data ? nOpen : "…"}</span></button>
              <button className={"chip" + (decided ? " on" : "")} aria-pressed={decided} onClick={() => navigate("/approvals?view=decided")}>Decided <span className="n">{done.data ? done.data.items.length : "…"}</span></button>
              <span className="grow" />
              <span className="t3 small sortctl">{decided ? "Recent" : "Expiry"} ▾</span>
            </div>
            {list.isError ? <ErrorBanner error={list.error} what="the inbox" retry={() => void list.refetch()} /> : null}
            {!list.data && !list.isError ? Array.from({ length: 6 }, (_, i) => <div key={i} className="gitem skel-item" aria-hidden="true"><i /><i /></div>) : null}
            {list.data && items.length === 0 ? (
              decided ? <Empty>No decided gates yet.</Empty>
                : <Empty title="Nothing is waiting on you">Publish and plan gates for your tenants appear here as jobs reach them. Gates you can't decide (for example on your own job) are left out.</Empty>
            ) : null}
            {items.map((g) => <GateItem key={g.gate_id} g={g} sel={g.gate_id === selected} dir={dir} />)}
          </section>
          {selected ? <PacketView key={selected} gateId={selected} /> : <section className="packet" aria-label="Approval packet" />}
        </div>
      </main>
    </>
  );
}
