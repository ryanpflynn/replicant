// "Repo configuration" for one run (design §8c, onboarding-interfaces.md §6): what of the repo's own Claude Code setup
// the agent loaded (CLAUDE.md files, rules, skills, hooks and how they ran), which MCP servers were skipped and why,
// and the hash of the effective settings. Old runs have no `repo_config` section and show nothing.
import { hookOutcome, mcpSkipWord, repoConfigHeadline, runRepoConfig } from "../lib/onboarding";

export function RepoConfigPanel({ cli }: { cli?: Record<string, unknown> | null }) {
  const rc = runRepoConfig(cli);
  if (!rc) return null;
  if (rc.mode === "ignore") {
    return <div className="gsum" aria-label="Repo configuration"><span className="t3 small">Repo configuration</span><p className="t2">Repo configuration not used. This project runs agents with Replicant's own settings only.</p></div>;
  }
  const evs = rc.hook_events_available !== false;
  const tags = (xs?: string[]) => xs?.length ? xs.map((x) => <span key={x} className="tag mono">{x}</span>) : <span className="t3">none</span>;
  return (
    <details className="gsum rcpanel" aria-label="Repo configuration" open>
      <summary><span className="t3 small">Repo configuration</span></summary>
      <p>{repoConfigHeadline(rc)}</p>
      <dl className="kv small">
        <dt>CLAUDE.md</dt><dd>{tags(rc.claude_md)}</dd>
        <dt>Rules</dt><dd>{tags(rc.rules)}</dd>
        <dt>Skills</dt><dd>{tags(rc.skills)}</dd>
        <dt>Hooks</dt><dd aria-label="Hooks">{rc.hooks?.length ? <ul className="plain">{rc.hooks.map((h, i) => {
          const o = hookOutcome(h, evs);
          return <li key={i}><span className="mono">{h.event}</span> <span className={o.bad ? "err-t" : "t2"}>{o.text}</span>{h.command_hash ? <span className="t3 mono"> {h.command_hash.slice(0, 15)}…</span> : null}</li>;
        })}</ul> : <span className="t3">none</span>}</dd>
        <dt>MCP servers</dt><dd aria-label="MCP servers">
          {rc.mcp_allowed?.length ? <div>allowed: {tags(rc.mcp_allowed)}</div> : null}
          {rc.mcp_skipped?.length ? <ul className="plain">{rc.mcp_skipped.map((m) => <li key={m.name + (m.identity ?? "")}><span className="mono">{m.name}</span> <span className="t2">skipped: {mcpSkipWord(m.reason)}</span>{m.identity ? <span className="t3 mono"> {m.identity}</span> : null}</li>)}</ul> : null}
          {!rc.mcp_allowed?.length && !rc.mcp_skipped?.length ? <span className="t3">none</span> : null}</dd>
        {rc.permission_rules_ignored ? <><dt>Permissions</dt><dd className="t2">{rc.permission_rules_ignored} repo permission rule{rc.permission_rules_ignored === 1 ? "" : "s"} recorded, not applied (platform rules only)</dd></> : null}
        <dt>Settings</dt><dd className="mono t3">{rc.settings_hash ?? "—"}{rc.config_base_sha ? <> · from <span title={rc.config_base_sha}>{rc.config_base_sha.slice(0, 10)}</span></> : null}</dd>
      </dl>
      {!evs ? <p className="t3 small">This CLI version doesn't report hook runs, so only the configured hooks are shown.</p> : null}
    </details>
  );
}
