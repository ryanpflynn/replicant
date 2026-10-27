import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Glyph, Icon } from "./Glyph";
import { Avatar, Kbd } from "./bits";
import { DEV_USERS, get } from "../lib/api";
import { countLabel, useJobs } from "../lib/jobs";
import { hasCap, useInbox, useLive, useMe, useSession, useWaiting } from "../lib/session";
import { badgeCount } from "../lib/waiting";
import { clock } from "../lib/format";
import { jobStatus } from "../lib/status";
import { LINKS } from "../lib/config";
import { Link, useRouter } from "../router";
import type { JobSummary } from "../lib/types";

function NavItem({ to, icon, label, count, attn, active, ext }: {
  to: string; icon: string; label: string; count?: string | number | null; attn?: boolean; active?: boolean; ext?: boolean;
}) {
  const body = (
    <>
      <Icon k={icon} /><span>{label}</span>
      {count != null && count !== "" ? <span className={"count" + (attn ? " attn" : "")}>{count}</span> : null}
    </>
  );
  if (ext) return <a href={to} target="_blank" rel="noreferrer noopener">{body}</a>;
  return <Link to={to} className={active ? "on" : ""} aria-current={active ? "page" : undefined}>{body}</Link>;
}

export function Rail() {
  const { loc } = useRouter();
  const me = useMe().data;
  const open = useJobs("open").data;
  const mine = useJobs("mine").data;
  const inbox = useInbox().data;
  const p = loc.path;
  const view = loc.search.get("view");
  const isAdmin = hasCap(me, "admin:platform") || hasCap(me, "identity:admin");
  const isMemoryAdmin = hasCap(me, "memory:admin");
  const mineOpen = mine ? mine.items.filter((j) => !["succeeded", "failed", "cancelled", "rejected", "expired", "taken_over"].includes(j.status)).length : null;
  const nInbox = inbox?.items.length ?? 0;
  const nWaiting = badgeCount(useWaiting().data);
  return (
    <aside className="rail">
      <div className="brand">
        <span className="mark" aria-hidden="true"><i /><i /><i /><i /></span>
        <span className="word">replicant</span>
        <span className="env" title="Local development stack (podman, v0 profile)">local</span>
      </div>
      <nav className="nav" aria-label="Primary">
        <div className="group caps">Work</div>
        <NavItem to="/waiting" icon="waiting" label="Waiting on you" count={nWaiting} attn={nWaiting != null}
          active={p === "/waiting"} />
        <NavItem to="/jobs" icon="jobs" label="Jobs" count={open ? countLabel(open) : null}
          active={(p === "/jobs" && view !== "mine") || p.startsWith("/jobs/")} />
        <NavItem to="/jobs?view=mine" icon="mine" label="My jobs" count={mineOpen} active={p === "/jobs" && view === "mine"} />
        <NavItem to="/approvals" icon="approvals" label="Approvals" count={nInbox ? nInbox : null} attn={nInbox > 0}
          active={p.startsWith("/approvals")} />
        {hasCap(me, "release:read") ? (
          <>
            <div className="group caps">Platform</div>
            <NavItem to="/releases" icon="releases" label="Releases" active={p.startsWith("/releases")} />
          </>
        ) : null}
        {isAdmin || isMemoryAdmin ? <div className="group caps">Replicant admin</div> : null}
        {isMemoryAdmin ? <NavItem to="/admin/memory" icon="memory" label="Memory" active={p.startsWith("/admin/memory")} /> : null}
        {isAdmin ? <NavItem to="/admin" icon="admin" label="Admin" active={p === "/admin" || (p.startsWith("/admin/") && !p.startsWith("/admin/memory"))} /> : null}
        {hasCap(me, "temporal:ui") ? (
          <>
            <div className="group caps">Operators</div>
            <NavItem to={LINKS.temporalUi + "/namespaces/" + LINKS.temporalNamespace + "/workflows"} icon="temporal" label="Temporal UI" count="↗" ext />
            <NavItem to={LINKS.grafana + "/d/replicant-overview"} icon="grafana" label="Grafana" count="↗" ext />
          </>
        ) : null}
      </nav>
      <UserSwitcher />
    </aside>
  );
}

function roleLine(login: string) {
  return DEV_USERS.find((u) => u.login === login)?.role ?? "";
}

function UserSwitcher() {
  const { user, setUser } = useSession();
  const me = useMe().data;
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const on = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", on);
    document.addEventListener("keydown", key);
    return () => { document.removeEventListener("mousedown", on); document.removeEventListener("keydown", key); };
  }, [open]);
  const name = me?.display_name ?? DEV_USERS.find((u) => u.login === user)?.name ?? user;
  return (
    <div className="me-wrap" ref={ref}>
      {open ? (
        <div className="menu usermenu" role="menu" aria-label="Switch local dev user">
          <div className="caps menu-h">Local dev identity · X-Replicant-Dev-User</div>
          {DEV_USERS.map((u) => (
            <button key={u.login} role="menuitemradio" aria-checked={u.login === user} className={"menu-it" + (u.login === user ? " on" : "")}
              onClick={() => { setUser(u.login); setOpen(false); }}>
              <Avatar name={u.name} sm /><span className="mono">{u.login}</span><span className="t3 small ellipsis">{u.role}</span>
              {u.login === user ? <Glyph g="check" family="success" /> : null}
            </button>
          ))}
        </div>
      ) : null}
      <button className="me" onClick={() => setOpen((v) => !v)} aria-haspopup="menu" aria-expanded={open} data-testid="user-switcher"
        title="Switch local dev user">
        <Avatar name={name} />
        <span className="who small"><span>{name}</span><br /><span className="t3 micro">{roleLine(user)}</span></span>
        <span className="t3 micro chev" aria-hidden="true">▴</span>
      </button>
    </div>
  );
}

export function TopBar({ children }: { children: ReactNode }) {
  const live = useLive();
  const { toggleTheme, setPaletteOpen } = useSession();
  return (
    <header className="topbar">
      <div className="crumbs">{children}</div>
      {live.ok ? <span className="live" aria-live="polite" title="Polling control-plane-api every 4 s"><i />Live</span>
        : <span className="live off" aria-live="polite"><i />Reconnecting · data as of {clock(new Date(live.asOf).toISOString())}</span>}
      <button className="search" onClick={() => setPaletteOpen(true)} aria-label="Open command palette">
        <Icon k="search" className="" /><span>Jump to job, gate, run, bundle…</span><Kbd>⌘K</Kbd>
      </button>
      <button className="iconbtn" aria-label="Toggle theme" title="Toggle theme (⇧T)" onClick={toggleTheme}><Icon k="theme" className="" /></button>
    </header>
  );
}

// ---- Command palette (components.md §10) ----
const GROUP_ORDER = ["Navigation", "Approvals", "Jobs", "PRs", "Commands"];
interface Item { group: string; key: string; label: ReactNode; text: string; hint?: ReactNode; run: () => void }

export function Palette() {
  const { paletteOpen: open, setPaletteOpen, setUser, toggleTheme, user } = useSession();
  const { navigate } = useRouter();
  const me = useMe().data;
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const recent = useQuery({
    queryKey: ["palette-jobs", user],
    enabled: open,
    queryFn: () => get<{ items: JobSummary[] }>("/v1/jobs?limit=200&sort=updated_desc"),
  });
  const inbox = useInbox().data;
  useEffect(() => { if (open) { setQ(""); setSel(0); setTimeout(() => inputRef.current?.focus(), 0); } }, [open]);
  const close = () => setPaletteOpen(false);
  const go = (to: string) => () => { navigate(to); close(); };
  const items = useMemo<Item[]>(() => {
    const out: Item[] = [];
    const nav: Array<[string, string, boolean]> = [
      ["Waiting on you", "/waiting", true], ["Jobs", "/jobs", true], ["My jobs", "/jobs?view=mine", true], ["Needs a human", "/jobs?view=needs_human", true],
      ["Approvals", "/approvals", true], ["Releases", "/releases", hasCap(me, "release:read")],
      ["Admin", "/admin", hasCap(me, "admin:platform")], ["Memory", "/admin/memory", hasCap(me, "memory:admin")],
    ];
    for (const [label, to, ok] of nav) if (ok) out.push({ group: "Navigation", key: "n" + to, label, text: label, run: go(to) });
    for (const g of inbox?.items ?? []) {
      out.push({ group: "Approvals", key: g.gate_id, text: `${g.gate_id} ${g.title} ${g.kind}`,
        label: <><span className="mono">{g.gate_id.slice(0, 15)}</span><span className="t2 ellipsis">{g.title}</span></>,
        hint: <span className="t3 small">{g.kind}</span>, run: go(`/approvals/${g.gate_id}`) });
    }
    for (const j of recent.data?.items ?? []) {
      out.push({ group: "Jobs", key: j.job_id, text: `${j.job_id} ${j.title} ${j.repos.join(" ")} ${j.pr ? "#" + j.pr.number : ""}`,
        label: <><span className="mono">{j.job_id.slice(0, 14)}</span><span className="t2 ellipsis">{j.title}</span></>,
        hint: <span className="t3 small">{jobStatus(j.status).label}</span>, run: go(`/jobs/${j.job_id}`) });
      for (const r of (j.pr ? [j.pr] : [])) {
        out.push({ group: "PRs", key: j.job_id + "pr", text: `#${r.number} ${j.title}`,
          label: <><span className="mono">#{r.number}</span><span className="t2 ellipsis">{j.title}</span></>,
          hint: <span className="t3 small">{r.state}</span>, run: go(`/jobs/${j.job_id}`) });
      }
    }
    out.push({ group: "Commands", key: "theme", label: "Toggle theme", text: "> toggle theme dark light", hint: <><Kbd>⇧</Kbd><Kbd>T</Kbd></>, run: () => { toggleTheme(); close(); } });
    for (const u of DEV_USERS) {
      out.push({ group: "Commands", key: "u" + u.login, text: `> switch user ${u.login} ${u.role}`,
        label: <>Switch user to <span className="mono">{u.login}</span></>, hint: <span className="t3 small">{u.role}</span>,
        run: () => { setUser(u.login); close(); } });
    }
    if (hasCap(me, "temporal:ui")) {
      out.push({ group: "Commands", key: "temporal", label: "Open Temporal UI", text: "> temporal ui workflows",
        hint: <span className="t3 small">↗</span>, run: () => { window.open(LINKS.temporalUi, "_blank", "noopener"); close(); } });
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me, inbox, recent.data, user]);

  const ql = q.trim().toLowerCase();
  const cmd = ql.startsWith(">");
  const filtered = items.filter((it) => {
    if (!ql) return it.group !== "PRs" && it.group !== "Commands";
    if (cmd) return it.group === "Commands" && it.text.toLowerCase().includes(ql.slice(1).trim());
    return ql.split(/\s+/).every((t) => it.text.toLowerCase().includes(t));
  }).sort((a, b) => GROUP_ORDER.indexOf(a.group) - GROUP_ORDER.indexOf(b.group)).slice(0, 12);
  const exact = /^job_[0-9a-hjkmnp-tv-z]{26}$/.test(q.trim()) ? q.trim() : null;
  const list: Item[] = exact && !filtered.some((f) => f.key === exact)
    ? [{ group: "Jobs", key: exact, text: exact, label: <span className="mono">{exact}</span>, hint: "open", run: go(`/jobs/${exact}`) }, ...filtered]
    : filtered;
  if (!open) return null;
  const s = Math.min(sel, Math.max(0, list.length - 1));
  let last = "";
  return (
    <>
      <div className="scrim" style={{ display: "block" }} onClick={close} />
      <div className="palette" style={{ display: "block" }} role="dialog" aria-label="Command palette">
        <div className="q">
          <Icon k="search" className="" />
          <input ref={inputRef} value={q} placeholder="Jump to a job, gate, PR (#2), or type > for commands"
            aria-label="Search" onChange={(e) => { setQ(e.target.value); setSel(0); }}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") { e.preventDefault(); setSel((x) => Math.min(x + 1, list.length - 1)); }
              else if (e.key === "ArrowUp") { e.preventDefault(); setSel((x) => Math.max(0, x - 1)); }
              else if (e.key === "Enter") { e.preventDefault(); list[s]?.run(); }
              else if (e.key === "Escape") close();
            }} />
        </div>
        <div className="pl-body">
          {list.length === 0 ? <div className="it t3">No matches. IDs, titles, repos and PR numbers are searched.</div> : null}
          {list.map((it, i) => {
            const head = it.group !== last ? <div className="grp caps" key={"g" + it.group}>{it.group}</div> : null;
            last = it.group;
            return (
              <div key={it.key}>
                {head}
                <div className={"it" + (i === s ? " on" : "")} onMouseEnter={() => setSel(i)} onClick={it.run} role="option" aria-selected={i === s}>
                  {it.label}{it.hint ? <span className="hint">{it.hint}</span> : null}
                </div>
              </div>
            );
          })}
        </div>
        <div className="ft"><span><Kbd>↑</Kbd><Kbd>↓</Kbd> move</span><span><Kbd>↵</Kbd> open</span><span><Kbd>esc</Kbd> close</span>
          <span style={{ marginLeft: "auto" }}>Type <span className="mono">&gt;</span> for commands, <span className="mono">#</span> for PRs</span></div>
      </div>
    </>
  );
}

