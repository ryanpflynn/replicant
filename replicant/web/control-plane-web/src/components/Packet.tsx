// Approval packet sections in the fixed order of components.md §8. Agent-written content carries a provenance
// eyebrow and is rendered as plain text (never Markdown, T-PI-04).
import { Fragment } from "react";
import { useQuery } from "@tanstack/react-query";
import { Glyph } from "./Glyph";
import { ErrorBanner, Pill, Quote } from "./bits";
import { getBytes } from "../lib/api";
import { BIDI_RE, parsePatch, unbundle, wordDiff, type DiffFile, type DiffLine } from "../lib/diff";
import type { Packet } from "../lib/types";

const RULE_WORDS: Record<string, string> = {
  "v0.diff.default_low": "no risk rule matched the diff; the v0 default level applies",
};

export function RulesSection({ packet }: { packet: Packet }) {
  const r = packet.content?.risk;
  return (
    <div className="section">
      <div className="section-h"><h3>Why this needs a human</h3><span className="t3 small">deterministic rules · risk <span className={`risk ${r?.level ?? ""}`}>{r?.level ?? "—"}</span></span></div>
      {r?.rules_fired?.length ? (
        <ul className="rules">
          {r.rules_fired.map((id) => {
            const lvl = id.includes("high") ? "high" : id.includes("medium") ? "medium" : r.level;
            return (
              <li key={id}>
                <Glyph g={lvl === "high" || lvl === "critical" ? "triangle" : "diamond-hollow"} family={lvl === "high" || lvl === "critical" ? "danger" : lvl === "medium" ? "warning" : "neutral"} />
                <span><span className="rid">{id}</span> <span className="why">{RULE_WORDS[id] ?? ""}</span></span>
                <span className={`risk ${lvl}`}>{lvl}</span>
              </li>
            );
          })}
        </ul>
      ) : <p className="t3">No rules fired.</p>}
      <p className="small t3" style={{ marginTop: 6 }}>
        {r?.raised_by_llm ? <b className="t2">The LLM classifier raised the level above the rules.</b> : "The LLM classifier did not raise the level."}
        {" "}Publishing gates are on for every risk level locally, so a low-risk change still asks for approval.
      </p>
    </div>
  );
}

export function PlanSection({ packet }: { packet: Packet }) {
  const plan = packet.resolved?.plan;
  if (!plan) return null;
  return (
    <div className="section plan">
      <div className="section-h"><h3>Plan</h3><span className="prov">Agent-written · untrusted</span>
        <span className="t3 small">{plan.steps?.length ?? 0} steps</span></div>
      {plan.summary ? <p className="t2 plantext">{plan.summary}</p> : null}
      {plan.steps?.length ? (
        <ol style={{ marginTop: 8 }}>
          {plan.steps.map((s) => (
            <li key={s.id}>{s.title}{s.files?.length ? <span className="t3 small"> · <span className="mono">{s.files.join(", ")}</span></span> : null}</li>
          ))}
        </ol>
      ) : null}
      {plan.acceptance_criteria?.length ? (
        <>
          <div className="caps" style={{ marginTop: 12, marginBottom: 4 }}>Acceptance criteria</div>
          <ul className="plain">
            {plan.acceptance_criteria.map((a) => <li key={a.id}><span className="mono t3">{a.id}</span> {a.text}</li>)}
          </ul>
        </>
      ) : null}
      {plan.risks?.length ? <p className="small t2" style={{ marginTop: 8 }}><span className="t3">Risks noted:</span> {plan.risks.join(" ")}</p> : null}
    </div>
  );
}

function Line({ l, words }: { l: DiffLine; words?: Array<{ text: string; changed: boolean }> }) {
  if (l.kind === "hunk") return <div className="ln hunk">{l.text}</div>;
  if (l.kind === "meta") return <div className="ln hunk">{l.text}</div>;
  const mark = l.kind === "add" ? "+" : l.kind === "del" ? "−" : " ";
  const Tag = l.kind === "add" ? "ins" : "del";
  return (
    <div className={"ln" + (l.kind === "ctx" ? "" : " " + l.kind)}>
      <span className="n">{l.oldNo ?? ""}</span><span className="n">{l.newNo ?? ""}</span><span className="m">{mark}</span>
      <span>{words ? words.map((w, i) => w.changed ? <Tag key={i}>{w.text}</Tag> : <Fragment key={i}>{w.text}</Fragment>) : l.text}</span>
    </div>
  );
}

function FileDiff({ f }: { f: DiffFile }) {
  // Pair consecutive del/add runs for word-level highlights.
  const words = new Map<number, Array<{ text: string; changed: boolean }>>();
  for (let i = 0; i < f.lines.length; i++) {
    if (f.lines[i].kind !== "del") continue;
    let j = i; while (j < f.lines.length && f.lines[j].kind === "del") j++;
    let k = j; while (k < f.lines.length && f.lines[k].kind === "add") k++;
    const n = Math.min(j - i, k - j);
    for (let x = 0; x < n; x++) {
      const [a, b] = wordDiff(f.lines[i + x].text, f.lines[j + x].text);
      words.set(i + x, a);
      words.set(j + x, b);
    }
    i = k - 1;
  }
  const long = f.lines.filter((l) => l.text.length > 160).length;
  const bidi = f.lines.some((l) => BIDI_RE.test(l.text));
  return (
    <>
      {long || bidi ? (
        <div className="facts">
          {long ? <span className="fact"><Glyph g="triangle" family="warning" />{long} line{long === 1 ? "" : "s"} over 160 characters <span className="mono">{f.path}</span></span> : null}
          {bidi ? <span className="fact"><Glyph g="triangle" family="danger" />Invisible or bidi control characters <span className="mono">{f.path}</span></span> : null}
        </div>
      ) : null}
      <div className="diff">
        <div className="fh"><span className="path">{f.path}</span><span className="stat mono"><span className="a">+{f.adds}</span> <span className="d">−{f.dels}</span></span></div>
        {f.binary ? <div className="ln hunk">Binary file</div> : f.lines.map((l, i) => <Line key={i} l={l} words={words.get(i)} />)}
      </div>
    </>
  );
}

export function DiffSection({ packet, gateId }: { packet: Packet; gateId: string }) {
  const link = packet.links?.diff_bundle;
  const q = useQuery({
    queryKey: ["diff", gateId, packet.packet_revision],
    enabled: !!link,
    staleTime: Infinity,
    queryFn: async () => {
      const { patch } = unbundle(await getBytes(link!));
      return parsePatch(patch);
    },
  });
  const files = q.data ?? [];
  const adds = files.reduce((a, f) => a + f.adds, 0);
  const dels = files.reduce((a, f) => a + f.dels, 0);
  const digest = packet.content?.diff_sha256;
  return (
    <div className="section">
      <div className="section-h"><h3>Diff</h3><span className="prov">Agent-written change</span>
        <span className="t3 small">{files.length} file{files.length === 1 ? "" : "s"} · <span className="mono a-mark">+{adds}</span> <span className="mono d-mark">−{dels}</span>
          {digest ? <> · <span className="mono" title={`sha256:${digest}`}>sha256:{digest.slice(0, 4)}…{digest.slice(-3)}</span></> : null}</span></div>
      {!link ? <p className="t3">No diff bundle is referenced by this packet.</p> : null}
      {q.isError ? <ErrorBanner error={q.error} what="the diff bundle" retry={() => void q.refetch()} /> : null}
      {q.isLoading ? <div className="diff skel-block" aria-hidden="true"><i /><i /><i /></div> : null}
      <div className="stack">{files.map((f) => <FileDiff key={f.path} f={f} />)}</div>
    </div>
  );
}

const QA_FAMILY: Record<string, "success" | "warning" | "danger" | "neutral"> = {
  pass: "success", pass_with_warnings: "warning", fail: "danger", inconclusive: "warning", not_applicable: "neutral",
};

export function QASection({ packet }: { packet: Packet }) {
  const qa = packet.resolved?.qa_report;
  if (!qa) return <div className="section"><div className="section-h"><h3>QA</h3></div><p className="t3">No QA report in this packet.</p></div>;
  const fam = QA_FAMILY[qa.verdict] ?? "neutral";
  const c = qa.counts ?? {};
  const total = (qa.stages ?? []).reduce((a, s) => a + (s.duration_seconds ?? 0), 0);
  return (
    <div className="section">
      <div className="section-h"><h3>QA</h3>
        <span className="aside"><Pill s={{ family: fam, glyph: fam === "success" ? "check" : fam === "danger" ? "cross" : "triangle", label: qa.verdict === "pass" ? "Passed" : qa.verdict.replace(/_/g, " ") }} /></span></div>
      <p className="small t2">{c.passed ?? 0} of {c.total ?? 0} checks passed{c.failed ? ` · ${c.failed} failed` : ""}{c.skipped ? ` · ${c.skipped} skipped` : ""} · {total.toFixed(1)} s</p>
      {qa.harness ? <p className="small t3"><span className="mono">{qa.harness.name} {qa.harness.version}</span></p> : null}
      <table className="t compact" style={{ marginTop: 8 }} aria-label="QA stages">
        <thead><tr><th>Stage</th><th>Result</th><th className="right">Duration</th></tr></thead>
        <tbody>
          {(qa.stages ?? []).map((s) => (
            <tr key={s.stage}><td className="mono">{s.stage}</td>
              <td><span className="row g1"><Glyph g={s.status === "passed" ? "check" : s.status === "failed" ? "cross" : "dot-hollow"} family={s.status === "passed" ? "success" : s.status === "failed" ? "danger" : "neutral"} />{s.status}</span></td>
              <td className="right num t2">{(s.duration_seconds ?? 0).toFixed(1)} s</td></tr>
          ))}
        </tbody>
      </table>
      {qa.reason_code ? <p className="small t3" style={{ marginTop: 6 }}>reason <span className="mono">{qa.reason_code}</span></p> : null}
    </div>
  );
}

export function ReviewSection({ packet }: { packet: Packet }) {
  const rv = packet.resolved?.review_report;
  const vr = packet.resolved?.verification_report;
  const ar = rv?.agent_report;
  return (
    <div className="section">
      <div className="section-h"><h3>Review</h3>{ar?.verdict ? <span className="aside small"><b>{ar.verdict}</b> <span className="t3">· {ar.findings?.length ?? 0} findings</span></span> : null}</div>
      {ar?.summary ? <Quote label="agent-written · reviewer">{ar.summary}</Quote> : <p className="t3">No review report.</p>}
      {vr ? (
        <dl className="kv" style={{ marginTop: 10 }}>
          <dt>Verification</dt>
          <dd><span className="row g1"><Glyph g={vr.passed ? "check" : "cross"} family={vr.passed ? "success" : "danger"} />{vr.passed ? "passed" : "failed"}</span>
            <span className="t3 small mono">{(vr.checks ?? []).join(" · ")}</span></dd>
        </dl>
      ) : null}
    </div>
  );
}

export function HandoffSection({ packet }: { packet: Packet }) {
  const h = packet.resolved?.handoff;
  if (!h?.narrative_markdown) return null;
  return (
    <div className="section">
      <div className="section-h"><h3>Implementer handoff</h3><span className="prov">Agent-written · untrusted</span>
        {h.producer?.cli ? <span className="t3 small mono">{h.producer.cli} {h.producer.cli_version} · {h.producer.model}</span> : null}</div>
      <Quote label="handoff narrative">{h.narrative_markdown}</Quote>
    </div>
  );
}
