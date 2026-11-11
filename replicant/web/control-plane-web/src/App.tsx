import { useEffect, useRef } from "react";
import { Palette, Rail } from "./components/Shell";
import { TopBar } from "./components/Shell";
import { Empty } from "./components/bits";
import { JobList } from "./screens/JobList";
import { JobDetail } from "./screens/JobDetail";
import { Approvals } from "./screens/Approvals";
import { Waiting } from "./screens/Waiting";
import { Releases } from "./screens/Releases";
import { Admin } from "./screens/Admin";
import { Memory } from "./screens/Memory";
import { Fleet } from "./screens/Fleet";
import { ProjectSettings, Projects } from "./screens/ProjectSettings";
import { hasCap, useMe, useSession, useWaiting } from "./lib/session";
import { badgeCount, titleWithCount } from "./lib/waiting";
import { Link, match, useRouter } from "./router";

function Routes() {
  const { loc, navigate } = useRouter();
  const p = loc.path;
  useEffect(() => { if (p === "/" || p === "") navigate("/jobs", { replace: true }); }, [p, navigate]);
  let m: Record<string, string> | null;
  if ((m = match("/jobs/:jobId/:tab?", p))) return <JobDetail key={m.jobId} jobId={m.jobId} tab={m.tab} />;
  if (match("/jobs", p)) return <JobList />;
  if (p === "/waiting") return <Waiting />;
  if ((m = match("/approvals/:gateId?", p))) return <Approvals gateId={m.gateId} />;
  if (p.startsWith("/releases")) return <Releases />;
  if ((m = match("/projects/:projectId/settings/:tab?", p))) return <ProjectSettings key={m.projectId} projectId={m.projectId} tab={m.tab} />;
  if (p === "/projects") return <Projects />;
  if (p.startsWith("/admin/memory")) return <Memory />;
  if (p.startsWith("/admin/fleet")) return <Fleet />;
  if (p === "/admin") return <Admin />;
  return (
    <>
      <TopBar><b>Not found</b></TopBar>
      <main className="main"><Empty title="No page at this address">Nothing lives at <span className="mono">{p}</span>. <Link to="/jobs">Go to jobs</Link>.</Empty></main>
    </>
  );
}

function useGlobalKeys() {
  const { setPaletteOpen, paletteOpen, toggleTheme } = useSession();
  const { navigate } = useRouter();
  const me = useMe().data;
  const g = useRef(0);
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setPaletteOpen(!paletteOpen); return; }
      const t = e.target as HTMLElement;
      if (t.closest("input, textarea, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey || paletteOpen) return;
      if (e.key === "T" && e.shiftKey) { e.preventDefault(); toggleTheme(); return; }
      if (e.key === "g") { g.current = Date.now(); return; }
      if (Date.now() - g.current < 1200) {
        const to = { j: "/jobs", w: "/waiting", a: "/approvals", r: hasCap(me, "release:read") ? "/releases" : null, m: hasCap(me, "memory:admin") ? "/admin/memory" : null }[e.key];
        g.current = 0;
        if (to) { e.preventDefault(); navigate(to); }
      }
    };
    addEventListener("keydown", on);
    return () => removeEventListener("keydown", on);
  }, [setPaletteOpen, paletteOpen, toggleTheme, navigate, me]);
}

/** Browser tab shows how much is waiting on you: "(2) replicant". */
function useWaitingTitle() {
  const n = badgeCount(useWaiting().data);
  useEffect(() => { document.title = titleWithCount(n); }, [n]);
}

export function App() {
  const { user } = useSession();
  useGlobalKeys();
  useWaitingTitle();
  return (
    <div className="shell" key={user}>
      <Rail />
      <Routes />
      <Palette />
    </div>
  );
}
