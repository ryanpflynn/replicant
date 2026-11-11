// Job detail → Graph tab (GRAPH-*): the job's C-DEF graph coloured by node status, a node panel with its visits and
// runs, and the selected run's agent activity (assistant messages, tool calls with commands/paths/diffs, results,
// gateway usage), polled while the run is live. URL state: /jobs/:id/graph?node=<key>&run=<run_id>.
import { RepoConfigPanel } from "../components/RepoConfigPanel";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Glyph } from "../components/Glyph";
import { Empty, ErrorBanner, Ext, Id, Pill, Quote } from "../components/bits";
import { get } from "../lib/api";
import { useSession } from "../lib/session";
import { clock, dateTime, duration, humanize, parseTs, usd } from "../lib/format";
import { runStatus, TERMINAL, type Family } from "../lib/status";
import { LINKS, grafanaContainersUrl } from "../lib/config";
import { Link, useRouter } from "../router";
import {
  LIVE_RUN_PHASES, NODE_H, NODE_W, STATUS_FAMILY, STATUS_WORDS, compactTokens, grafanaExploreLoki,
  grafanaExploreTrace, layout, type FeedItem, type GraphDoc, type GraphNode, type GraphRun, type RunActivityDoc,
} from "../lib/graph";
import "../styles/graph.css";


function nodeMeta(n: GraphNode): string {
  const parts: string[] = [];
  if (n.status !== "pending") parts.push(STATUS_WORDS[n.status].toLowerCase());
  if (n.visits > 1) parts.push(`×${n.visits}`);
  if (n.duration_ms != null && n.status !== "skipped" && n.duration_ms >= 1000) parts.push(duration(n.duration_ms));
  if (n.runs.length) parts.push(`${n.runs.length} run${n.runs.length === 1 ? "" : "s"}`);
  if (!parts.length) parts.push(n.is_gate ? "gate" : n.is_agent ? "agent" : n.type.replace(/_/g, " "));
  return parts.join(" · ");
}

export function GraphSvg({ doc, selected, onSelect }: { doc: NonNullable<GraphDoc["graph"]>; selected?: string; onSelect: (k: string) => void }) {
  const lay = useMemo(() => layout(doc), [doc]);
  const byKey = useMemo(() => new Map(doc.nodes.map((n) => [n.key, n])), [doc]);
  return (
    <svg className="gsvg" width={lay.width} height={lay.height} viewBox={`0 0 ${lay.width} ${lay.height}`} role="group"
      aria-label={`Job graph, ${doc.nodes.length} nodes`}>
      <defs>
        <marker id="ga" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L8,4 L0,8 z" style={{ fill: "var(--c-line-strong)" }} />
        </marker>
        <marker id="ga-t" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L8,4 L0,8 z" style={{ fill: "var(--c-text-2)" }} />
        </marker>
      </defs>
      {lay.edges.map((pe, i) => (
        <g key={i} className={"ge" + (pe.e.taken ? " taken" : "") + (pe.back ? " back" : "") + (pe.e.kind === "default" ? " dflt" : "")}>
          <title>{`${pe.e.from} → ${pe.e.to}${pe.e.when ? `\nwhen ${pe.e.when}` : pe.e.kind === "default" ? "\notherwise" : ""}${pe.e.taken ? `\ntaken ${pe.e.taken}×` : ""}`}</title>
          <path d={pe.d} markerEnd={`url(#${pe.e.taken ? "ga-t" : "ga"})`} />
          {pe.back && pe.e.taken ? <text x={pe.lx + 2} y={pe.ly} className="gel">{pe.e.taken}×</text> : null}
        </g>
      ))}
      {[...lay.nodes.values()].map((p) => {
        if (p.end) {
          const ok = /merged|succeeded|completed|no_change/.test(p.key);
          return (
            <g key={p.key} className={"gn end " + (ok ? "s-succeeded" : doc.job_status === "failed" ? "s-failed" : "s-skipped")} transform={`translate(${p.x},${p.y})`}>
              <rect width={NODE_W} height={NODE_H - 8} y={4} rx={16} />
              <text x={NODE_W / 2} y={NODE_H / 2 + 4} textAnchor="middle" className="k">{p.key.replace("end:", "").slice(0, 20)}</text>
              <title>{`terminal outcome ${p.key.replace("end:", "")}`}</title>
            </g>
          );
        }
        const n = byKey.get(p.key)!;
        const sel = selected === n.key;
        return (
          <g key={n.key} className={`gn s-${n.status}${sel ? " sel" : ""}${n.current ? " cur" : ""}${n.is_gate ? " gate" : ""}${n.is_agent ? " agent" : ""}`}
            transform={`translate(${p.x},${p.y})`} role="button" tabIndex={0} aria-pressed={sel}
            aria-label={`${n.key}: ${STATUS_WORDS[n.status]}${n.visits > 1 ? `, ${n.visits} visits` : ""}`} data-node={n.key}
            onClick={() => onSelect(n.key)} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(n.key); } }}>
            <title>{`${n.key} (${n.type})\n${STATUS_WORDS[n.status]}${n.outcome ? ` · ${n.outcome}` : ""}${n.reason ? ` · ${n.reason}` : ""}`}</title>
            <rect width={NODE_W} height={NODE_H} rx={n.is_gate ? 3 : 8} className="box" />
            {n.is_gate ? <path className="ico" d="M14 13 L20 19 L14 25 L8 19 Z" /> :
              n.is_agent ? <g className="ico"><circle cx={14} cy={19} r={5.5} /><circle cx={14} cy={19} r={1.8} className="dotc" /></g> :
                <rect className="ico" x={10} y={15} width={8} height={8} rx={1.5} />}
            <text x={26} y={17} className="k">{n.key}</text>
            <text x={26} y={31} className="m">{nodeMeta(n)}</text>
            {n.status === "running" || n.current ? <circle className="beat" cx={NODE_W - 10} cy={10} r={3.5} /> : null}
          </g>
        );
      })}
    </svg>
  );
}

function Legend() {
  return (
    <div className="glegend small t3" aria-label="Legend">
      {(["pending", "running", "waiting", "succeeded", "failed", "skipped"] as const).map((s) => (
        <span key={s}><i className={`sw s-${s}`} />{STATUS_WORDS[s]}</span>
      ))}
      <span><svg width="14" height="10" aria-hidden="true"><path d="M1 5h12" className="lg-taken" /></svg>path taken</span>
      <span><svg width="14" height="10" aria-hidden="true"><path d="M7 1 L12 5 L7 9 L2 5 Z" className="lg-gate" /></svg>gate</span>
      <span><svg width="14" height="10" aria-hidden="true"><circle cx="7" cy="5" r="4" className="lg-agent" /></svg>agent run</span>
    </div>
  );
}

const NODE_GLYPH: Record<string, string> = { succeeded: "check", failed: "cross", running: "ring-spin", waiting: "diamond", skipped: "dot-hollow", cancelled: "slash", pending: "ring-dotted" };

function runLabel(r: GraphRun) {
  return `${r.role ?? "run"} c${r.cycle ?? 0}.n${r.ordinal ?? 1}`;
}

function NodePanel({ jobId, n, doc, run, onRun }: { jobId: string; n: GraphNode; doc: NonNullable<GraphDoc["graph"]>; run?: string; onRun: (r: string) => void }) {
  const exits = doc.edges.filter((e) => e.from === n.key);
  const fam = STATUS_FAMILY[n.status] as Family;
  return (
    <div className="section gnode">
      <div className="section-h">
        <h3 className="mono">{n.key}</h3>
        <Pill s={{ family: fam, glyph: NODE_GLYPH[n.status], label: STATUS_WORDS[n.status] }} />
        <span className="t3 small aside">{n.type.replace(/_/g, " ")}{n.phase ? ` · phase ${n.phase}` : ""}</span>
      </div>
      <dl className="kv">
        {n.agent ? <><dt>Agent</dt><dd className="mono">{n.agent}</dd></> : null}
        {n.gate_kind ? <><dt>Gate</dt><dd>{humanize(n.gate_kind)}</dd></> : null}
        <dt>Visits</dt><dd className="num">{n.visits}{n.max_visits ? <span className="t3"> of max {n.max_visits}</span> : null}{n.has_guard ? <span className="t3 small"> · guarded (may be skipped)</span> : null}</dd>
        {n.outcome ? <><dt>Outcome</dt><dd className="mono">{n.outcome}{n.reason ? <span className="err-t"> · {n.reason}</span> : null}</dd></> : null}
        {n.duration_ms != null ? <><dt>Time in node</dt><dd className="num">{duration(n.duration_ms)}{n.started_at ? <span className="t3 small"> · first entered {dateTime(n.started_at)}</span> : null}</dd></> : null}
      </dl>
      {n.gates?.length ? (
        <ul className="gexits">{n.gates.map((g) => <li key={g.gate_id}><Id id={g.gate_id} to={`/approvals/${g.gate_id}`} /> <span className="t2">{g.state}{g.decision ? ` · ${g.decision}` : ""}</span></li>)}</ul>
      ) : null}
      {n.history.length > 1 ? (
        <details className="ghist"><summary className="small t2">Visit history ({n.history.length})</summary>
          <ol>{n.history.map((h, i) => <li key={i} className="small"><span className="mono t3">{clock(h.started_at)}</span> {h.outcome ?? "…"}{h.started_at && h.finished_at ? <span className="t3"> · {duration((parseTs(h.finished_at) ?? 0) - (parseTs(h.started_at) ?? 0))}</span> : null}</li>)}</ol>
        </details>
      ) : null}
      {exits.length ? (
        <div className="gexits small"><span className="t3">Exits</span>
          {exits.map((e, i) => <span key={i} className={"gx" + (e.taken ? " on" : "")} title={e.when ?? (e.kind === "default" ? "otherwise" : "always")}>
            → <span className="mono">{e.to.replace("end:", "end · ")}</span>{e.label ? <span className="t3"> ({e.label})</span> : null}{e.taken ? <b> {e.taken}×</b> : null}</span>)}
        </div>
      ) : null}
      {n.runs.length ? (
        <div className="gruns" role="tablist" aria-label="Runs of this node">
          {n.runs.map((r) => {
            const st = runStatus(r.phase ?? "");
            const end = parseTs(r.finished_at) ?? (LIVE_RUN_PHASES.has(r.phase ?? "") ? Date.now() : null);
            return (
              <button key={r.run_id} role="tab" aria-selected={run === r.run_id} className={"grun" + (run === r.run_id ? " on" : "")} onClick={() => onRun(r.run_id)} data-run={r.run_id}>
                <Glyph g={st.glyph} family={st.family} /><span>{runLabel(r)}</span>
                <span className="t3 mono small">{r.run_id.slice(0, 12)}…</span>
                <span className="t3 small">{r.started_at && end ? duration(end - (parseTs(r.started_at) ?? end)) : st.label}</span>
                {r.result_outcome || r.qa_verdict ? <span className="t2 small">{r.result_outcome ?? r.qa_verdict}</span> : null}
              </button>
            );
          })}
        </div>
      ) : n.is_agent ? <p className="small t3">No run yet.</p> : <p className="small t3">This node runs inside the workflow (no sandbox, no agent).</p>}
      {n.runs.length && run ? <RunActivity jobId={jobId} runId={run} /> : null}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ activity feed
const FILTERS = [["all", "All"], ["msg", "Messages"], ["tools", "Tool calls"], ["errors", "Errors"]] as const;
type Filter = (typeof FILTERS)[number][0];

function DiffLines({ diff }: { diff: string }) {
  return (
    <pre className="udiff">{diff.split("\n").map((l, i) => {
      const c = l.startsWith("+++") || l.startsWith("---") ? "fh" : l.startsWith("@@") ? "hk" : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : "";
      return <span key={i} className={c}>{l || " "}{"\n"}</span>;
    })}</pre>
  );
}

function Clip({ text, lines = 14, className }: { text: string; lines?: number; className?: string }) {
  const [open, setOpen] = useState(false);
  const n = text.split("\n").length;
  const shown = open || n <= lines ? text : text.split("\n").slice(0, lines).join("\n");
  return (
    <>
      <pre className={className}>{shown}</pre>
      {n > lines ? <button className="linkbtn small" onClick={() => setOpen(!open)}>{open ? "show less" : `show all ${n} lines`}</button> : null}
    </>
  );
}

function toolVerb(it: FeedItem): ReactNode {
  const t = it.tool ?? "tool";
  if (t === "Bash") return <><b>Bash</b> <code className="cmd">{it.command?.split("\n")[0]}</code></>;
  if (it.path) return <><b>{t}</b> <span className="mono">{it.summary ?? it.path}</span></>;
  return <><b>{t}</b> <span className="t2">{it.summary}</span></>;
}

function FeedRow({ it }: { it: FeedItem }) {
  const [open, setOpen] = useState(() => it.kind === "tool_call" && (!!it.diff || !!it.result?.is_error));
  const at = <span className="when mono" title={it.at ?? ""}>{clock(it.at)}</span>;
  if (it.kind === "assistant_text") {
    return <li className="fi say">{at}<span className="gl"><i className="kind">AI</i></span><div className="body"><div className="saytext">{it.text}</div>
      {it.usage ? <span className="t3 small tok">{compactTokens(it.usage.input_tokens)} in · {compactTokens(it.usage.output_tokens)} out</span> : null}</div></li>;
  }
  if (it.kind === "user_message" || it.kind === "steering") {
    return <li className="fi user">{at}<span className="gl"><i className="kind">{it.kind === "steering" ? "ST" : "IN"}</i></span><div className="body">
      <Quote label={it.kind === "steering" ? "steering message (untrusted)" : "task prompt (untrusted input inside)"}><Clip text={it.text ?? ""} lines={6} /></Quote></div></li>;
  }
  if (it.kind === "tool_call") {
    const r = it.result;
    const dur = r?.at && it.at ? (parseTs(r.at) ?? 0) - (parseTs(it.at) ?? 0) : null;
    return (
      <li className={"fi tool" + (r?.is_error ? " err" : "") + (!r ? " pending" : "")}>{at}
        <span className="gl">{r ? <Glyph g={r.is_error ? "cross" : "check"} family={r.is_error ? "danger" : "success"} /> : <Glyph g="ring-spin" family="info" />}</span>
        <div className="body">
          <button className="frow" aria-expanded={open} onClick={() => setOpen(!open)}>
            <span className="caret">{open ? "▾" : "▸"}</span><span className="verb">{toolVerb(it)}</span>
            <span className="t3 small right">{r ? `${r.is_error ? "error" : "ok"}${dur != null && dur >= 0 ? ` · ${duration(dur)}` : ""}${r.lines ? ` · ${r.lines} line${r.lines === 1 ? "" : "s"}` : ""}` : "running…"}</span>
          </button>
          {it.description && it.tool === "Bash" ? <div className="t3 small">{it.description}</div> : null}
          {open ? (
            <div className="fdet">
              {it.tool === "Bash" && it.command && it.command.includes("\n") ? <pre className="code">{it.command}</pre> : null}
              {it.diff ? <DiffLines diff={it.diff} /> : null}
              {it.diff_truncated ? <p className="small t3">diff truncated</p> : null}
              {it.input && !it.diff ? <Clip text={it.input} className="code" /> : null}
              {r ? (r.text ? <><div className="small t3 rlabel">{r.is_error ? "error output" : "result"}{r.truncated ? " (truncated)" : ""}</div><Clip text={r.text} className={"code out" + (r.is_error ? " bad" : "")} /></>
                : <p className="small t3">no output recorded</p>) : <p className="small t3">waiting for the tool result…</p>}
            </div>
          ) : null}
        </div>
      </li>
    );
  }
  if (it.kind === "result") {
    return <li className="fi res">{at}<span className="gl"><Glyph g={it.is_error ? "cross" : "check"} family={it.is_error ? "danger" : "success"} /></span>
      <div className="body"><b>Agent finished</b> <span className="t2">· {it.subtype}{it.num_turns != null ? ` · ${it.num_turns} turns` : ""}{it.cost_usd != null ? ` · ${usd(it.cost_usd)} reported by the CLI` : ""}{it.duration_ms ? ` · ${duration(it.duration_ms)}` : ""}</span></div></li>;
  }
  return <li className="fi misc">{at}<span className="gl"><Glyph g="plain" /></span><div className="body t2 small">{it.summary ?? it.kind}</div></li>;
}

export function RunActivity({ jobId, runId }: { jobId: string; runId: string }) {
  const { user } = useSession();
  const [filter, setFilter] = useState<Filter>("all");
  const q = useQuery({
    queryKey: ["activity", user, jobId, runId],
    queryFn: () => get<RunActivityDoc>(`/v1/jobs/${jobId}/runs/${runId}/activity`),
    refetchInterval: (qq) => (qq.state.data?.live === false ? false : 3000),
  });
  if (q.isError) return <ErrorBanner error={q.error} what="the run's activity" retry={() => void q.refetch()} />;
  const d = q.data;
  if (!d) return <div className="gact" aria-busy="true"><div className="skel-line w70" /><div className="skel-line w40" /><div className="skel-line w70" /></div>;
  const st = runStatus(d.run.phase ?? "");
  const items = d.items.filter((it) => filter === "all" ? true : filter === "msg" ? ["assistant_text", "user_message", "steering", "result"].includes(it.kind)
    : filter === "tools" ? it.kind === "tool_call" : (it.kind === "tool_call" && it.result?.is_error) || it.is_error || it.kind === "api_retry");
  const fromGateway = !!d.usage?.totals?.requests;
  const tu = d.meta.transcript_usage;
  const u = fromGateway ? d.usage?.totals : tu ? { ...tu, requests: d.counts.assistant_text ?? 0 } : d.usage?.totals;
  const end = parseTs(d.run.finished_at) ?? Date.now();
  const cli = d.result?.cli as Record<string, string | number> | undefined;
  const lastUp = d.source.last_upload_at ? Math.max(0, Math.round((Date.now() - (parseTs(d.source.last_upload_at) ?? Date.now())) / 1000)) : null;
  return (
    <div className="gact" aria-label={`Activity of ${runId}`} data-testid="run-activity">
      <div className="gact-h">
        <Pill s={st} />
        <span><b>{d.run.role}</b> <Id id={runId} full /></span>
        <span className="t3 small">{d.run.node_key}.c{d.run.cycle ?? 0}.n{d.run.ordinal ?? 1}{d.run.isolation ? ` · ${d.run.isolation}` : ""}{d.meta.model ? ` · ${d.meta.model}` : ""}{d.meta.claude_code_version ? ` · claude-code ${d.meta.claude_code_version}` : ""}</span>
        {d.live ? <span className="livepill small" title="Transcript chunks are shipped by the sandbox uploader; the feed polls every 3 s">● live{lastUp != null ? ` · last upload ${lastUp}s ago` : " · waiting for the first upload"}</span> : null}
      </div>
      <div className="gstats">
        <div><span className="t3 small">Duration</span><b className="num">{d.run.started_at ? duration(end - (parseTs(d.run.started_at) ?? end)) : "—"}</b></div>
        <div title={fromGateway ? "gateway shim usage log" : "from the transcript; the gateway usage log is flushed in batches"}><span className="t3 small">Model requests{fromGateway ? "" : " (transcript)"}</span><b className="num">{u?.requests ?? "—"}</b></div>
        <div><span className="t3 small">Tokens in / out</span><b className="num">{compactTokens(u?.input_tokens)} / {compactTokens(u?.output_tokens)}</b>{u?.cache_read_tokens ? <span className="t3 small"> cache {compactTokens(u.cache_read_tokens)}</span> : null}</div>
        <div><span className="t3 small">Cost (gateway)</span><b className="num">{fromGateway && u ? `$${(u.cost_usd ?? 0).toFixed(4)}` : "pending"}</b></div>
        <div><span className="t3 small">Tool calls</span><b className="num">{d.counts.tool_call ?? 0}</b><span className="t3 small"> {Object.entries(d.tools).map(([k, v]) => `${k} ${v}`).join(" · ")}</span></div>
        <div><span className="t3 small">Exit</span><b className="num">{cli ? `${cli.exit_code ?? "—"} · ${cli.result_subtype ?? ""}` : d.live ? "running" : d.run.phase}</b>{d.run.reason ? <span className="err-t small"> {d.run.reason}</span> : null}</div>
      </div>
      <div className="gact-links small">
        <Ext href={grafanaExploreLoki(LINKS.grafana, d.observe.loki_datasource, d.observe.loki_expr, d.observe.from, d.observe.to)}>Sandbox logs (Loki) ↗</Ext>
        <Ext href={grafanaExploreTrace(LINKS.grafana, d.observe.trace_id)}>Trace <span className="mono">{d.observe.trace_id.slice(0, 10)}…</span> ↗</Ext>
        <Ext href={grafanaContainersUrl()}>Containers ↗</Ext>
        <span className="t3">{d.source.chunks} transcript chunk{d.source.chunks === 1 ? "" : "s"}{d.source.provisional_chunks ? ` (${d.source.provisional_chunks} in progress)` : ""}</span>
      </div>
      {d.result?.summary ? <div className="gsum"><span className="t3 small">Agent report{d.result.outcome ? ` · ${d.result.outcome}` : ""}</span><p>{d.result.summary}</p>
        {d.result.changes?.files_changed != null ? <p className="small t2">{d.result.changes.files_changed} file{d.result.changes.files_changed === 1 ? "" : "s"} changed <span className="a-mark">+{d.result.changes.insertions ?? 0}</span> <span className="d-mark">−{d.result.changes.deletions ?? 0}</span> {d.result.changes.paths?.map((p) => <span key={p} className="tag mono">{p}</span>)}</p> : null}</div> : null}
      <RepoConfigPanel cli={d.result?.cli} />
      <div className="row g1 gfilt" role="radiogroup" aria-label="Filter activity">
        {FILTERS.map(([k, label]) => <button key={k} role="radio" aria-checked={filter === k} className={"chip" + (filter === k ? " on" : "")} onClick={() => setFilter(k)}>{label}</button>)}
        {d.items_dropped ? <span className="t3 small">{d.items_dropped} earlier items not shown</span> : null}
      </div>
      {items.length ? <ol className="feed" aria-label="Agent activity">{items.map((it) => <FeedRow key={it.i} it={it} />)}</ol>
        : <Empty>{d.items.length ? "Nothing matches this filter." : d.live ? "No activity uploaded yet. The sandbox ships its transcript in chunks; the first one appears within about 30 seconds of the agent starting." : d.log.length ? "This run has no agent transcript (see the sandbox log below)." : "No transcript was recorded for this run."}</Empty>}
      {d.log.length ? <details className="glog" open={!d.items.length}><summary className="small t2">Sandbox log ({d.log.length} lines: QA stages, bootstrap, CLI stderr)</summary><pre className="code">{d.log.map((l) => l.text).join("\n")}</pre></details> : null}
      {d.usage?.requests.length ? (
        <details className="glog"><summary className="small t2">Gateway requests ({d.usage.requests.length}) · {d.usage.models.join(", ")}</summary>
          <table className="t small" aria-label="Gateway requests">
            <thead><tr><th>Time</th><th>Model</th><th className="right">In</th><th className="right">Out</th><th className="right">Cache read</th><th className="right">Cost</th><th className="right">Latency</th><th>Status</th></tr></thead>
            <tbody>{d.usage.requests.map((r, i) => <tr key={i}><td className="mono">{clock(r.at)}</td><td className="mono">{r.model}</td><td className="right num">{r.input_tokens.toLocaleString()}</td><td className="right num">{r.output_tokens.toLocaleString()}</td><td className="right num">{r.cache_read_tokens.toLocaleString()}</td><td className="right num">${r.cost_usd.toFixed(4)}</td><td className="right num">{r.latency_ms ?? "—"} ms</td><td>{r.status}{r.retry_attempt ? ` (retry ${r.retry_attempt})` : ""}</td></tr>)}</tbody>
          </table>
        </details>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ tab
function defaultNode(g: NonNullable<GraphDoc["graph"]>): string {
  if (g.current_node) return g.current_node;
  const withRuns = [...g.path].reverse().find((k) => g.nodes.find((n) => n.key === k)?.runs.length);
  return withRuns ?? g.path[g.path.length - 1] ?? g.entry;
}

export function GraphTab({ jobId, jobStatus }: { jobId: string; jobStatus: string }) {
  const { user } = useSession();
  const { loc, navigate } = useRouter();
  const q = useQuery({
    queryKey: ["graph", user, jobId], queryFn: () => get<GraphDoc>(`/v1/jobs/${jobId}/graph`),
    refetchInterval: TERMINAL.has(jobStatus) ? false : 4000,
  });
  const g = q.data?.graph ?? null;
  const nodeKey = loc.search.get("node") ?? (g ? defaultNode(g) : undefined);
  const node = g?.nodes.find((n) => n.key === nodeKey);
  const runParam = loc.search.get("run") ?? undefined;
  const run = node?.runs.find((r) => r.run_id === runParam)?.run_id ?? node?.runs[node.runs.length - 1]?.run_id;
  const go = (k: string, r?: string) => {
    const sp = new URLSearchParams();
    sp.set("node", k);
    if (r) sp.set("run", r);
    navigate(`/jobs/${jobId}/graph?${sp}`, { replace: true });
  };
  useEffect(() => {
    if (!g) return;
    const el = document.querySelector(`.gwrap [data-node="${nodeKey}"]`);
    (el as SVGElement | null)?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  }, [g, nodeKey]);
  if (q.isError) return <ErrorBanner error={q.error} what="the job graph" retry={() => void q.refetch()} />;
  if (!q.data) return <div className="section"><div className="skel-line w40" /><div className="skel-line w70" /></div>;
  if (!g) {
    return <Empty title="No graph to show yet">{q.data.unavailable_reason === "not_routed" ? "The job has not been routed to a workflow type yet." : "The graph definition pinned by this job's release bundle could not be read from the registry."}</Empty>;
  }
  return (
    <div className="gtab">
      <div className="gleft">
        <div className="gmeta small t3">
          <span className="mono">{q.data.definition?.ref}</span> · {g.nodes.length} nodes · cycle {g.cycle}
          {g.terminal ? <> · ended <span className="mono">{g.terminal.outcome}</span></> : null}
        </div>
        <div className="gwrap"><GraphSvg doc={g} selected={nodeKey} onSelect={(k) => go(k)} /></div>
        <Legend />
      </div>
      <div className="gright">
        {node ? <NodePanel jobId={jobId} n={node} doc={g} run={run} onRun={(r) => go(node.key, r)} /> : <Empty>Select a node.</Empty>}
      </div>
    </div>
  );
}

/** Link from the Runs tab / live strip to a run's activity. */
export function activityHref(jobId: string, nodeKey: string, runId: string) {
  return `/jobs/${jobId}/graph?node=${encodeURIComponent(nodeKey)}&run=${encodeURIComponent(runId)}`;
}

export function ActivityLink({ jobId, nodeKey, runId, children }: { jobId: string; nodeKey: string; runId: string; children: ReactNode }) {
  return <Link to={activityHref(jobId, nodeKey, runId)}>{children}</Link>;
}
