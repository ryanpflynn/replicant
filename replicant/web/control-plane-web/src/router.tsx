// Minimal history router: the URL holds every view and filter (shareable deep links, ui/README.md §Global).
import { createContext, useCallback, useContext, useEffect, useState, type AnchorHTMLAttributes, type ReactNode } from "react";

interface Loc { path: string; search: URLSearchParams }
const RouterCtx = createContext<{ loc: Loc; navigate: (to: string, opts?: { replace?: boolean }) => void } | null>(null);

const read = (): Loc => ({ path: location.pathname, search: new URLSearchParams(location.search) });

export function Router({ children }: { children: ReactNode }) {
  const [loc, setLoc] = useState<Loc>(read);
  useEffect(() => {
    const on = () => setLoc(read());
    addEventListener("popstate", on);
    return () => removeEventListener("popstate", on);
  }, []);
  const navigate = useCallback((to: string, opts?: { replace?: boolean }) => {
    if (opts?.replace) history.replaceState(null, "", to); else history.pushState(null, "", to);
    setLoc(read());
    if (!opts?.replace) document.querySelector(".main")?.scrollTo?.(0, 0);
  }, []);
  return <RouterCtx.Provider value={{ loc, navigate }}>{children}</RouterCtx.Provider>;
}

export function useRouter() {
  const c = useContext(RouterCtx);
  if (!c) throw new Error("no router");
  return c;
}

/** Matches "/jobs/:jobId/:tab?" style patterns. */
export function match(pattern: string, path: string): Record<string, string> | null {
  const p = pattern.split("/").filter(Boolean);
  const s = path.split("/").filter(Boolean);
  const out: Record<string, string> = {};
  for (let i = 0; i < Math.max(p.length, s.length); i++) {
    const seg = p[i];
    if (seg === undefined) return null;
    const opt = seg.endsWith("?");
    const name = seg.replace(/^:/, "").replace(/\?$/, "");
    if (s[i] === undefined) { if (opt) continue; return null; }
    if (seg.startsWith(":")) out[name] = decodeURIComponent(s[i]);
    else if (seg !== s[i]) return null;
  }
  return out;
}

export function Link({ to, children, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement> & { to: string }) {
  const { navigate } = useRouter();
  return (
    <a href={to} {...rest} onClick={(e) => {
      rest.onClick?.(e);
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
      e.preventDefault();
      navigate(to);
    }}>{children}</a>
  );
}
