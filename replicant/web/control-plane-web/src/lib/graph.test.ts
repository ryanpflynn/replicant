import { describe, expect, it } from "vitest";
import { backEdges, layout, NODE_H, type GraphDoc, type GraphNode } from "./graph";

const node = (key: string, extra: Partial<GraphNode> = {}): GraphNode => ({
  key, type: "x", status: "pending", visits: 0, is_agent: false, is_gate: false, has_guard: false, current: false,
  runs: [], history: [], ...extra,
});

const g: NonNullable<GraphDoc["graph"]> = {
  entry: "a", job_status: "failed", cycle: 0, current_node: null, path: ["a", "b", "c", "b"], ends: ["end:qa_failed"],
  terminal: { outcome: "qa_failed", node: "end:qa_failed" },
  nodes: ["a", "b", "c", "d"].map((k) => node(k)),
  edges: [
    { from: "a", to: "b", kind: "next", taken: 1 },
    { from: "b", to: "c", kind: "switch", label: "ok", taken: 1 },
    { from: "b", to: "d", kind: "switch", label: "other" },
    { from: "c", to: "b", kind: "default", taken: 1 },
    { from: "c", to: "end:qa_failed", kind: "switch", taken: 1 },
    { from: "d", to: "end:never", kind: "next" },
  ],
};

describe("graph layout", () => {
  it("finds loops as back edges", () => {
    const back = backEdges(["a", "b", "c", "d"], g.edges, "a");
    expect([...back]).toEqual(["c\u0000b"]);
  });
  it("ranks top-down, places siblings side by side and only the reached end", () => {
    const l = layout(g);
    const y = (k: string) => l.nodes.get(k)!.y;
    expect(y("a")).toBeLessThan(y("b"));
    expect(y("c")).toBe(y("d"));
    expect(l.nodes.get("c")!.x).not.toBe(l.nodes.get("d")!.x);
    expect(l.nodes.has("end:qa_failed")).toBe(true);
    expect(l.nodes.has("end:never")).toBe(false);
    expect(y("end:qa_failed")).toBeGreaterThan(y("c") + NODE_H - 1);
    const loop = l.edges.find((e) => e.e.from === "c" && e.e.to === "b")!;
    expect(loop.back).toBe(true);
    expect(l.edges.filter((e) => e.back)).toHaveLength(1);
    expect(l.width).toBeGreaterThan(0);
  });
});
