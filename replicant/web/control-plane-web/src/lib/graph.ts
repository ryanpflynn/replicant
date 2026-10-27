// Job graph view (GRAPH-*): types of GET /v1/jobs/{id}/graph and GET /v1/jobs/{id}/runs/{run}/activity, and a small
// layered layout for the C-DEF graph (no layout dependency: ≤ 64 nodes, C-DEF graph-definition maxItems).
//  1. DFS from the entry in edge order; an edge to a node on the DFS stack is a back edge (a loop).
//  2. Rank = longest path from the entry over forward edges (top to bottom).
//  3. Order within a rank by DFS discovery, then a few barycenter sweeps to reduce crossings.
//  4. Back edges are routed in lanes to the right of the graph.

export type NodeStatus = "pending" | "running" | "waiting" | "succeeded" | "failed" | "skipped" | "cancelled";

export interface GraphRun {
  run_id: string; role?: string; phase?: string; cycle?: number; ordinal?: number; started_at?: string;
  finished_at?: string; result_outcome?: string; qa_verdict?: string; reason?: string;
}

export interface GraphNode {
  key: string; type: string; phase?: string; status: NodeStatus; visits: number; max_visits?: number | null;
  agent?: string | null; gate_kind?: string | null; is_agent: boolean; is_gate: boolean; has_guard: boolean;
  duration_ms?: number | null; started_at?: string | null; finished_at?: string | null; current: boolean;
  outcome?: string; reason?: string; runs: GraphRun[];
  history: Array<{ started_at?: string | null; finished_at?: string | null; outcome?: string; reason?: string }>;
  gates?: Array<{ gate_id: string; state: string; opened_at?: string; expires_at?: string; decision?: string }>;
}

export interface GraphEdge { from: string; to: string; kind: "next" | "switch" | "default"; when?: string; label?: string | null; taken?: number }

export interface GraphDoc {
  job_id: string; workflow_type?: string; release_bundle_id?: string; as_of: string;
  definition?: { ref: string; digest: string; name?: string };
  unavailable_reason?: string;
  graph: null | {
    entry: string; nodes: GraphNode[]; edges: GraphEdge[]; ends: string[]; path: string[];
    terminal: { outcome: string; node: string } | null; job_status: string; cycle: number; current_node: string | null;
  };
}

export interface ToolResult { at?: string; is_error: boolean; text: string; truncated?: boolean; lines?: number; interrupted?: boolean }
export interface FeedItem {
  i: number; kind: string; at?: string | null; text?: string; summary?: string; model?: string; tool?: string;
  tool_use_id?: string; command?: string; description?: string; path?: string; diff?: string; diff_truncated?: boolean;
  input?: string; result?: ToolResult; usage?: Record<string, number>; untrusted?: boolean; truncated?: boolean;
  subtype?: string; num_turns?: number; cost_usd?: number; duration_ms?: number; is_error?: boolean; length?: number;
  cli_version?: string; parent_tool_use_id?: string;
}
export interface UsageRow {
  at: string; model?: string; status?: string; http_status?: number; input_tokens: number; output_tokens: number;
  cache_read_tokens: number; cache_write_tokens: number; cost_usd: number; latency_ms?: number; retry_attempt?: number;
  run_spent_usd?: number;
}
export interface RunActivityDoc {
  job_id: string; live: boolean; as_of: string;
  run: GraphRun & { node_key: string; isolation?: string };
  items: FeedItem[]; log: Array<{ kind: "log"; text: string }>; items_dropped: number;
  counts: Record<string, number>; tools: Record<string, number>;
  meta: { model?: string; claude_code_version?: string; result?: { subtype?: string; num_turns?: number; cost_usd?: number }; transcript_usage?: Record<string, number> };
  source: { chunks: number; provisional_chunks: number; last_upload_at?: string | null; error?: string };
  usage?: { requests: UsageRow[]; totals: Record<string, number>; models: string[]; capped?: boolean; error?: string };
  result?: {
    outcome?: string; summary?: string; cli?: Record<string, unknown>; budget?: Record<string, unknown>;
    changes?: { files_changed?: number; insertions?: number; deletions?: number; paths?: string[] };
    usage?: Record<string, number>; verdict?: string; qa_verdict?: string;
  };
  observe: { trace_id: string; loki_datasource: string; loki_expr: string; from?: string; to?: string };
}

// ------------------------------------------------------------------------------------------------ layout
export const NODE_W = 156;
export const NODE_H = 40;
export const COL_W = 176;
export const ROW_H = 62;
export const LANE_W = 7;
const PAD = 16;

export interface PlacedNode { key: string; x: number; y: number; rank: number; slot: number; end?: boolean }
export interface PlacedEdge { e: GraphEdge; back: boolean; d: string; lane?: number; lx: number; ly: number }
export interface Layout { nodes: Map<string, PlacedNode>; edges: PlacedEdge[]; width: number; height: number }

export function backEdges(keys: string[], edges: Array<{ from: string; to: string }>, entry: string): Set<string> {
  const out = new Map<string, string[]>();
  for (const e of edges) (out.get(e.from) ?? out.set(e.from, []).get(e.from)!).push(e.to);
  const state = new Map<string, 1 | 2>(); // 1 on stack, 2 done
  const back = new Set<string>();
  const visit = (start: string) => {
    // iterative DFS keeping edge order
    const stack: Array<{ n: string; i: number }> = [{ n: start, i: 0 }];
    state.set(start, 1);
    while (stack.length) {
      const top = stack[stack.length - 1];
      const succ = out.get(top.n) ?? [];
      if (top.i >= succ.length) { state.set(top.n, 2); stack.pop(); continue; }
      const v = succ[top.i++];
      const s = state.get(v);
      if (s === 1) back.add(`${top.n}\u0000${v}`);
      else if (!s) { state.set(v, 1); stack.push({ n: v, i: 0 }); }
    }
  };
  if (keys.includes(entry)) visit(entry);
  for (const k of keys) if (!state.get(k)) visit(k);
  return back;
}

/** Layered layout of the graph's nodes plus at most one end node (the reached terminal outcome). */
export function layout(g: NonNullable<GraphDoc["graph"]>): Layout {
  const keys = g.nodes.map((n) => n.key);
  const known = new Set(keys);
  const endNode = g.terminal?.node;
  const all = endNode ? [...keys, endNode] : keys;
  const drawn = g.edges.filter((e) => known.has(e.from) && (known.has(e.to) || e.to === endNode));
  // one drawn edge per (from, to): switch arms that share a target are merged
  const uniq = new Map<string, GraphEdge>();
  for (const e of drawn) {
    const k = `${e.from}\u0000${e.to}`;
    const prev = uniq.get(k);
    if (!prev) uniq.set(k, { ...e });
    else {
      prev.taken = Math.max(prev.taken ?? 0, e.taken ?? 0) || undefined;
      prev.label = [prev.label, e.label].filter(Boolean).join(" · ") || null;
    }
  }
  const es = [...uniq.values()];
  const back = backEdges(all, es, g.entry);
  const fwd = es.filter((e) => !back.has(`${e.from}\u0000${e.to}`));

  // rank: longest path over forward edges (Kahn order)
  const indeg = new Map(all.map((k) => [k, 0]));
  const succ = new Map<string, string[]>();
  const pred = new Map<string, string[]>();
  for (const e of fwd) {
    indeg.set(e.to, (indeg.get(e.to) ?? 0) + 1);
    (succ.get(e.from) ?? succ.set(e.from, []).get(e.from)!).push(e.to);
    (pred.get(e.to) ?? pred.set(e.to, []).get(e.to)!).push(e.from);
  }
  const rank = new Map(all.map((k) => [k, 0]));
  const q = all.filter((k) => (indeg.get(k) ?? 0) === 0);
  const order: string[] = [];
  while (q.length) {
    const n = q.shift()!;
    order.push(n);
    for (const v of succ.get(n) ?? []) {
      rank.set(v, Math.max(rank.get(v) ?? 0, (rank.get(n) ?? 0) + 1));
      indeg.set(v, (indeg.get(v) ?? 0) - 1);
      if (indeg.get(v) === 0) q.push(v);
    }
  }
  const maxRank = Math.max(0, ...rank.values());
  const rows: string[][] = Array.from({ length: maxRank + 1 }, () => []);
  for (const k of order) rows[rank.get(k)!].push(k);
  const pos = new Map<string, number>();
  const setPos = () => rows.forEach((r) => r.forEach((k, i) => pos.set(k, i - (r.length - 1) / 2)));
  setPos();
  const bary = (k: string, nb: Map<string, string[]>) => {
    const ns = (nb.get(k) ?? []).filter((x) => pos.has(x));
    return ns.length ? ns.reduce((s, x) => s + pos.get(x)!, 0) / ns.length : pos.get(k)!;
  };
  for (let sweep = 0; sweep < 4; sweep++) {
    const down = sweep % 2 === 0;
    const idx = down ? rows.map((_, i) => i) : rows.map((_, i) => rows.length - 1 - i);
    for (const i of idx) {
      const r = rows[i];
      const b = new Map(r.map((k) => [k, bary(k, down ? pred : succ)]));
      r.sort((a, c) => b.get(a)! - b.get(c)!);
      r.forEach((k, j) => pos.set(k, j - (r.length - 1) / 2));
    }
  }
  const widest = Math.max(1, ...rows.map((r) => r.length));
  const cx = PAD + ((widest - 1) / 2) * COL_W + NODE_W / 2;
  const nodes = new Map<string, PlacedNode>();
  rows.forEach((r, ri) => r.forEach((k, j) => {
    nodes.set(k, { key: k, rank: ri, slot: j, x: cx + pos.get(k)! * COL_W - NODE_W / 2, y: PAD + ri * ROW_H, end: k === endNode });
  }));
  const minX = Math.min(...[...nodes.values()].map((n) => n.x));
  if (minX < PAD) for (const n of nodes.values()) n.x += PAD - minX;
  const right = Math.max(...[...nodes.values()].map((n) => n.x + NODE_W));

  // edges
  const placed: PlacedEdge[] = [];
  let lane = 0;
  for (const e of es) {
    const a = nodes.get(e.from)!;
    const b = nodes.get(e.to)!;
    const isBack = back.has(`${e.from}\u0000${e.to}`);
    if (isBack || e.from === e.to) {
      lane++;
      const lx = right + 8 + lane * LANE_W;
      const y1 = a.y + NODE_H / 2;
      const y2 = b.y + NODE_H / 2;
      const x1 = a.x + NODE_W;
      const x2 = b.x + NODE_W;
      const d = e.from === e.to
        ? `M${x1},${y1 - 8} C${x1 + 28},${y1 - 22} ${x1 + 28},${y1 + 22} ${x1},${y1 + 8}`
        : `M${x1},${y1} L${lx - 6},${y1} Q${lx},${y1} ${lx},${y1 - 6} L${lx},${y2 + 6} Q${lx},${y2} ${lx - 6},${y2} L${x2 + 4},${y2}`;
      placed.push({ e, back: true, d, lane, lx: lx + 3, ly: (y1 + y2) / 2 });
      continue;
    }
    const x1 = a.x + NODE_W / 2;
    const y1 = a.y + NODE_H;
    const x2 = b.x + NODE_W / 2;
    const y2 = b.y - 2;
    const span = b.rank - a.rank;
    let d: string;
    if (span > 1 && Math.abs(x1 - x2) < 4) {
      // a long straight drop would run through the nodes in between: bow out to the left
      const bx = Math.min(a.x, b.x) - 14;
      d = `M${x1 - 30},${y1} C${bx},${y1 + 20} ${bx},${y2 - 20} ${x2 - 30},${y2}`;
    } else {
      const my = (y1 + y2) / 2;
      d = `M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`;
    }
    placed.push({ e, back: false, d, lx: (x1 + x2) / 2, ly: (y1 + y2) / 2 });
  }
  return { nodes, edges: placed, width: right + 24 + lane * LANE_W + 40, height: PAD * 2 + (maxRank + 1) * ROW_H };
}

// ------------------------------------------------------------------------------------------------ helpers
export const STATUS_WORDS: Record<NodeStatus, string> = {
  pending: "Pending", running: "Running", waiting: "Waiting", succeeded: "Succeeded", failed: "Failed",
  skipped: "Skipped", cancelled: "Cancelled",
};

export const STATUS_FAMILY: Record<NodeStatus, string> = {
  pending: "neutral", running: "info", waiting: "attention", succeeded: "success", failed: "danger", skipped: "neutral",
  cancelled: "neutral",
};

export const LIVE_RUN_PHASES = new Set(["Pending", "Provisioning", "Running", "Finalizing"]);

/** Grafana Explore URL (Loki sandbox tenant, fixed server-built selector for the run). */
export function grafanaExploreLoki(base: string, ds: string, expr: string, from?: string, to?: string): string {
  const range = { from: from ? String(Date.parse(from) - 60_000) : "now-6h", to: to ? String(Date.parse(to) + 120_000) : "now" };
  const panes = { a: { datasource: ds, queries: [{ refId: "A", expr, datasource: { type: "loki", uid: ds } }], range } };
  return `${base}/explore?schemaVersion=1&orgId=1&panes=${encodeURIComponent(JSON.stringify(panes))}`;
}

export function grafanaExploreTrace(base: string, traceId: string): string {
  const panes = { a: { datasource: "tempo", queries: [{ refId: "A", queryType: "traceql", query: traceId, datasource: { type: "tempo", uid: "tempo" } }], range: { from: "now-7d", to: "now" } } };
  return `${base}/explore?schemaVersion=1&orgId=1&panes=${encodeURIComponent(JSON.stringify(panes))}`;
}

export function compactTokens(n?: number): string {
  if (n == null) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}
