import { useQuery } from "@tanstack/react-query";
import { lagSeconds, request } from "./api";
import { OPEN_STATUSES, TERMINAL } from "./status";
import { useSession } from "./session";
import type { JobSummary } from "./types";

// Saved views of the job list (ui/job-list.md (1)): Open = non-terminal; terminal jobs live under Recent (R2-035).
export const VIEWS = [
  { key: "open", label: "Open", title: "Non-terminal jobs", params: { status: OPEN_STATUSES.join(",") } },
  { key: "needs_human", label: "Needs a human", title: "Waiting at an approval gate", params: { view: "needs_human" } },
  { key: "running", label: "Running", title: "Queued or running", params: { view: "running" } },
  { key: "pr_review", label: "In PR review", title: "PR open, waiting for review or merge", params: { view: "pr_review" } },
  { key: "recent", label: "Recent", title: "Terminal jobs, last 7 days", params: { status: [...TERMINAL].join(",") } },
  { key: "failed_24h", label: "Failed 24h", title: "Failed in the last 24 hours", params: { view: "failed_24h" } },
  { key: "mine", label: "My jobs", title: "Jobs you requested", params: { view: "mine" } },
] as const;
export type ViewKey = (typeof VIEWS)[number]["key"];

const LIMIT = 200;

export function useJobs(view: ViewKey, q = "") {
  const { user } = useSession();
  const v = VIEWS.find((x) => x.key === view) ?? VIEWS[0];
  return useQuery({
    queryKey: ["jobs", user, view, q],
    refetchInterval: 4000,
    queryFn: async () => {
      const sp = new URLSearchParams({ ...v.params, limit: String(LIMIT), sort: "updated_desc" });
      if (q.trim()) sp.set("q", q.trim());
      const r = await request<{ items: JobSummary[]; next_cursor: string | null }>(`/v1/jobs?${sp}`);
      let items = r.data.items;
      if (view === "recent") {
        const cut = Date.now() - 7 * 86_400_000;
        items = items.filter((j) => Date.parse(j.updated_at) >= cut);
      }
      return { items, more: !!r.data.next_cursor, lag: lagSeconds(r.headers) };
    },
  });
}

export const countLabel = (d?: { items: unknown[]; more: boolean }) => (d ? `${d.items.length}${d.more ? "+" : ""}` : "…");
