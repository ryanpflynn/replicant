import { createContext, useCallback, useContext, useEffect, useState, useSyncExternalStore, type ReactNode } from "react";
import { QueryCache, QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, DEV_USERS, get, getDevUser, request, setDevUser } from "./api";
import type { InboxItem, Me, UiPreferences, WaitingResponse } from "./types";
import { groupWaiting, WAITING_UNAVAILABLE, type Waiting } from "./waiting";

// ---- live indicator store: filled by the query cache (polling stands in for SSE in v0; WEB-04) ----
let live = { ok: true, asOf: Date.now() };
const subs = new Set<() => void>();
const setLive = (ok: boolean) => {
  live = { ok, asOf: ok ? Date.now() : live.asOf };
  subs.forEach((f) => f());
};
export const useLive = () => useSyncExternalStore((f) => { subs.add(f); return () => subs.delete(f); }, () => live);

export const POLL_MS = 4000;

export function makeQueryClient() {
  return new QueryClient({
    queryCache: new QueryCache({
      onSuccess: () => { if (!live.ok || Date.now() - live.asOf > 1000) setLive(true); },
      onError: (e) => { if (!(e instanceof ApiError) || e.status === 0 || e.status >= 500) setLive(false); },
    }),
    defaultOptions: {
      queries: {
        staleTime: 2000,
        retry: (n, e) => !(e instanceof ApiError && e.status >= 400 && e.status < 500) && n < 2,
        refetchOnWindowFocus: true,
      },
    },
  });
}

// ---- session: dev identity and theme ----
type Theme = "light" | "dark";
interface Session {
  user: string; setUser: (login: string) => void;
  theme: Theme; toggleTheme: () => void;
  paletteOpen: boolean; setPaletteOpen: (v: boolean) => void;
}
const Ctx = createContext<Session | null>(null);

function initialTheme(): Theme {
  const t = document.documentElement.dataset.theme;
  if (t === "light" || t === "dark") return t;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [user, setUserState] = useState(getDevUser);
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [paletteOpen, setPaletteOpen] = useState(false);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);
  const setUser = useCallback((login: string) => {
    setDevUser(login);
    setUserState(login);
    qc.removeQueries();
  }, [qc]);
  const toggleTheme = useCallback(() => {
    setTheme((t) => {
      const n = t === "dark" ? "light" : "dark";
      try { localStorage.setItem("replicant.theme", n); } catch { /* storage unavailable */ }
      return n;
    });
  }, []);
  return <Ctx.Provider value={{ user, setUser, theme, toggleTheme, paletteOpen, setPaletteOpen }}>{children}</Ctx.Provider>;
}

export function useSession() {
  const c = useContext(Ctx);
  if (!c) throw new Error("no session");
  return c;
}

export function useMe() {
  const { user } = useSession();
  return useQuery({ queryKey: ["me", user], queryFn: () => get<Me>("/v1/me"), staleTime: 300_000, refetchInterval: 300_000 });
}

export type JobView = "simple" | "detailed";

/** WEB-UIPREF: the caller's job view, read from /v1/me (`ui_preferences`; Simple when unset) and saved per user with
 * PUT /v1/me/ui-preferences. The save sends the version it read (If-Match); a 412 (changed in another tab) re-reads
 * and retries once. The /v1/me cache is updated optimistically and rolled back on failure. */
export function useJobView() {
  const { user } = useSession();
  const qc = useQueryClient();
  const me = useMe().data;
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const view: JobView = me?.ui_preferences?.job_view === "detailed" ? "detailed" : "simple";
  const setView = useCallback(async (next: JobView) => {
    const key = ["me", user];
    const prev = qc.getQueryData<Me>(key);
    if (prev) qc.setQueryData<Me>(key, { ...prev, ui_preferences: { ...prev.ui_preferences, job_view: next } });
    setSaving(true); setError(null);
    try {
      for (let attempt = 0; ; attempt++) {
        const cur = await get<UiPreferences>("/v1/me/ui-preferences");
        try {
          await request<UiPreferences>("/v1/me/ui-preferences", { method: "PUT", body: JSON.stringify({ job_view: next }),
            headers: { "If-Match": `"${cur.version ?? 0}"` } });
          break;
        } catch (e) {
          if (!(e instanceof ApiError && e.status === 412) || attempt > 0) throw e;
        }
      }
    } catch (e) {
      if (prev) qc.setQueryData<Me>(key, prev);
      setError(e instanceof ApiError ? e : new ApiError(0, { code: "client_error", detail: String(e) }));
    } finally {
      setSaving(false);
    }
  }, [qc, user]);
  return { view, ready: !!me, setView, saving, error };
}

export const hasCap = (me: Me | undefined, cap: string) => !!me?.capabilities.includes(cap);

/** principal_id → display name for the static dev users (M1 has no people directory; WEB-05). */
export function useDirectory(): Map<string, string> {
  const q = useQuery({
    queryKey: ["directory"],
    staleTime: Infinity,
    queryFn: async () => {
      const pairs = await Promise.all(DEV_USERS.map(async (u) => {
        try {
          const r = await fetch("/v1/me", { headers: { "X-Replicant-Dev-User": u.login } });
          const d = (await r.json()) as Me;
          return [d.principal_id, u.name] as const;
        } catch { return null; }
      }));
      return pairs.filter((p): p is readonly [string, string] => !!p);
    },
  });
  return new Map(q.data ?? []);
}

export const personName = (dir: Map<string, string>, pid?: string | null) =>
  !pid ? "—" : dir.get(pid) ?? (pid.startsWith("svc_") ? pid : pid.slice(0, 14) + "…");

export function useInbox(view: "mine" | "decided" = "mine") {
  const { user } = useSession();
  return useQuery({
    queryKey: ["approvals", user, view],
    queryFn: () => get<{ items: InboxItem[] }>(`/v1/approvals?view=${view}&limit=100`),
    refetchInterval: 4000,
  });
}

/** "Waiting on you": gates you can decide and open PRs you requested or reviewed. Polled like the inbox. An API that
 * predates the endpoint answers 404 (or 405): that reads as "unavailable", not an error, and is re-checked every minute. */
export function useWaiting() {
  const { user } = useSession();
  return useQuery<Waiting>({
    queryKey: ["waiting", user],
    queryFn: async () => {
      try {
        return groupWaiting(await get<WaitingResponse>("/v1/me/waiting"));
      } catch (e) {
        if (e instanceof ApiError && (e.status === 404 || e.status === 405)) return WAITING_UNAVAILABLE;
        throw e;
      }
    },
    refetchInterval: (q) => (q.state.data && !q.state.data.available ? 60_000 : POLL_MS),
  });
}
