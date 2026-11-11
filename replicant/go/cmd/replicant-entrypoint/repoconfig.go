package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"example.com/replicant/internal/runner/ipc"
	"example.com/replicant/internal/runner/jcsx"
	"example.com/replicant/internal/runner/sandbox"
)

// Repo configuration at run time (design §8c, onboarding-interfaces.md §5–6): with repo_config use, the CLI loads the
// repo's project settings, hooks, rules, skills and nested CLAUDE.md under the managed settings. The configuration is
// trusted only as it is on the default branch (config_base_sha), and the run reports what it loaded, skipped and
// what the hooks did (cli.repo_config).

// repoConfigPaths are the configuration paths restored from config_base_sha (a subset of agentConfigPaths, which
// diff collection strips and guardPaths protects).
var repoConfigPaths = []string{"**/CLAUDE.md", "**/CLAUDE.local.md", "**/AGENTS.md", ".claude/**", "**/.claude/**", "**/.mcp.json"}

func isRepoConfigPath(rel string) bool {
	for _, g := range repoConfigPaths {
		if globMatch(g, rel) {
			return true
		}
	}
	return false
}

// restoreBaseConfig makes the checkout's configuration paths exactly what they are at sha (fetched when missing):
// files changed on the branch are restored, files added on the branch are removed, and the result is read-only.
// This covers PR-repair jobs whose branch carries human commits (§8c "Config comes from the base branch only").
func restoreBaseConfig(ctx context.Context, repo, sha string) error {
	env := gitEnv()
	if _, err := git(ctx, repo, env, "cat-file", "-e", sha+"^{commit}"); err != nil {
		if _, err := git(ctx, repo, env, "fetch", "--no-tags", "--filter=blob:none", "--depth", "1", "origin", sha); err != nil {
			return err
		}
	}
	baseOut, err := git(ctx, repo, env, "ls-tree", "-r", "-z", "--name-only", sha)
	if err != nil {
		return err
	}
	headOut, err := git(ctx, repo, env, "ls-files", "-z")
	if err != nil {
		return err
	}
	base := map[string]bool{}
	var restore []string
	for _, f := range strings.Split(baseOut, "\x00") {
		if f != "" && isRepoConfigPath(f) {
			base[f] = true
			restore = append(restore, f)
		}
	}
	var remove []string
	for _, f := range strings.Split(headOut, "\x00") {
		if f != "" && isRepoConfigPath(f) && !base[f] {
			remove = append(remove, f)
		}
	}
	for _, batch := range batches(remove, 200) {
		if _, err := git(ctx, repo, env, append([]string{"rm", "-q", "-f", "--"}, batch...)...); err != nil {
			return err
		}
	}
	for _, batch := range batches(restore, 200) {
		if _, err := git(ctx, repo, env, append([]string{"checkout", sha, "--"}, batch...)...); err != nil {
			return err
		}
	}
	for _, f := range restore {
		p := filepath.Join(repo, f)
		if fi, err := os.Lstat(p); err == nil && fi.Mode().IsRegular() {
			_ = os.Chmod(p, fi.Mode().Perm()&^0o222)
		}
	}
	log.Info("repo configuration restored from base", "sha", sha, "restored", len(restore), "removed", len(remove))
	return nil
}

func batches(s []string, n int) [][]string {
	var out [][]string
	for len(s) > 0 {
		k := min(n, len(s))
		out = append(out, s[:k])
		s = s[k:]
	}
	return out
}

// projectSettings is the subset of the repo's .claude/settings.json the report needs.
type projectSettings struct {
	Hooks       map[string][]hookGroup `json:"hooks"`
	Permissions struct {
		Allow []json.RawMessage `json:"allow"`
		Deny  []json.RawMessage `json:"deny"`
		Ask   []json.RawMessage `json:"ask"`
	} `json:"permissions"`
}

type hookGroup struct {
	Matcher string `json:"matcher"`
	Hooks   []struct {
		Type    string `json:"type"`
		Command string `json:"command"`
	} `json:"hooks"`
}

// repoHook is one configured project command hook, in configuration order (events sorted by name).
type repoHook struct {
	Event, Matcher, Command string
}

func readProjectSettings(repo string) ([]byte, *projectSettings) {
	b, err := os.ReadFile(filepath.Join(repo, ".claude", "settings.json"))
	if err != nil {
		return nil, nil
	}
	var ps projectSettings
	if json.Unmarshal(b, &ps) != nil {
		return b, nil
	}
	return b, &ps
}

func (ps *projectSettings) commandHooks() []repoHook {
	if ps == nil {
		return nil
	}
	var events []string
	for e := range ps.Hooks {
		events = append(events, e)
	}
	sort.Strings(events)
	var out []repoHook
	for _, e := range events {
		for _, g := range ps.Hooks[e] {
			for _, h := range g.Hooks {
				if h.Type == "command" && h.Command != "" {
					out = append(out, repoHook{Event: e, Matcher: g.Matcher, Command: h.Command})
				}
			}
		}
	}
	return out
}

var simpleMatcherRe = regexp.MustCompile(`^[A-Za-z0-9_|]+$`)

// hookMatches is Claude Code's matcher rule: empty or "*" matches everything, a plain `A|B` list matches exactly,
// anything else is a regular expression. Events without a matcher query (Stop, UserPromptSubmit) match every group.
func hookMatches(matcher, query string) bool {
	if matcher == "" || matcher == "*" || query == "" {
		return true
	}
	if simpleMatcherRe.MatchString(matcher) {
		for _, m := range strings.Split(matcher, "|") {
			if m == query {
				return true
			}
		}
		return false
	}
	re, err := regexp.Compile("^(?:" + matcher + ")$")
	return err == nil && re.MatchString(query)
}

// managedHookMarker is what the managed hooks print on success when repo_config is use, so that their stream-json
// hook events are told apart from the repo's (the CLI's hook events carry no command). suppressOutput keeps it out of
// the transcript.
const managedHookMarker = `{"suppressOutput":true}`

// hookTracker attributes the CLI's stream-json hook events (`--include-hook-events`: system/hook_started and
// system/hook_response with hook_id, hook_name "<event>:<query>", exit_code, outcome, stderr) to configured repo
// hooks. The CLI starts the matching hooks of one settings source as a burst, in configuration order, so a burst of
// k×n started events for n matching repo hooks maps by position. Bursts of the managed hooks are recognised by
// their output; anything else is reported unattributed (command_hash null).
type hookTracker struct {
	mu     sync.Mutex
	bursts []*hookBurst
	byID   map[string]*hookRun
	open   *hookBurst
	n      int
}

type hookBurst struct {
	name string
	runs []*hookRun
}

type hookRun struct {
	resp map[string]any
}

const maxTrackedHookRuns = 5000

func newHookTracker() *hookTracker { return &hookTracker{byID: map[string]*hookRun{}} }

// observe is called for every parsed stream-json event, in order.
func (t *hookTracker) observe(typ, sub string, ev map[string]any) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if typ != "system" || sub != "hook_started" {
		t.open = nil
		if typ == "system" && sub == "hook_response" {
			id, _ := ev["hook_id"].(string)
			if r := t.byID[id]; r != nil {
				r.resp = ev
			}
		}
		return
	}
	if t.n >= maxTrackedHookRuns {
		return
	}
	t.n++
	name, _ := ev["hook_name"].(string)
	id, _ := ev["hook_id"].(string)
	r := &hookRun{}
	t.byID[id] = r
	if t.open == nil || t.open.name != name {
		t.open = &hookBurst{name: name}
		t.bursts = append(t.bursts, t.open)
	}
	t.open.runs = append(t.open.runs, r)
}

func isManagedHookRun(r *hookRun) bool {
	if r.resp == nil {
		return false
	}
	stdout, _ := r.resp["stdout"].(string)
	stderr, _ := r.resp["stderr"].(string)
	return strings.TrimSpace(stdout) == managedHookMarker || strings.HasPrefix(stderr, "replicant: ")
}

// hookStat is one cli.repo_config.hooks entry.
type hookStat struct {
	Event       string  `json:"event"`
	Matcher     string  `json:"matcher,omitempty"`
	CommandHash *string `json:"command_hash"`
	Runs        int     `json:"runs"`
	Failures    int     `json:"failures"`
	Blocks      int     `json:"blocks"`
	Cause       *string `json:"cause"`
	Detail      string  `json:"detail,omitempty"`
}

func (s *hookStat) record(r *hookRun) {
	s.Runs++
	if r.resp == nil {
		return // killed before it answered
	}
	outcome, _ := r.resp["outcome"].(string)
	code := 0
	if f, ok := r.resp["exit_code"].(float64); ok {
		code = int(f)
	}
	if code == 2 {
		s.Blocks++ // exit 2 is the hook's own blocking/feedback signal
		return
	}
	if outcome == "success" {
		return
	}
	s.Failures++
	stderr, _ := r.resp["stderr"].(string)
	cause, detail := sandbox.ClassifyFailure(code, outcome == "cancelled", []byte(stderr))
	if cause == "" {
		cause = "non_zero_exit"
	}
	s.Cause, s.Detail = &cause, detail
}

func commandHash(c string) string {
	d := sha256.Sum256([]byte(c))
	return "sha256:" + hex.EncodeToString(d[:])
}

// hookStats renders the configured repo hooks with their runs and failures.
func (t *hookTracker) hookStats(hooks []repoHook) []hookStat {
	stats := make([]hookStat, len(hooks))
	for i, h := range hooks {
		ch := commandHash(h.Command)
		stats[i] = hookStat{Event: h.Event, Matcher: h.Matcher, CommandHash: &ch}
	}
	unattributed := map[string]*hookStat{}
	var order []string
	t.mu.Lock()
	defer t.mu.Unlock()
	for _, b := range t.bursts {
		managed := false
		for _, r := range b.runs {
			managed = managed || isManagedHookRun(r)
		}
		if managed {
			continue
		}
		event, query, _ := strings.Cut(b.name, ":")
		var cands []int
		for i, h := range hooks {
			if h.Event == event && hookMatches(h.Matcher, query) {
				cands = append(cands, i)
			}
		}
		for i, r := range b.runs {
			if len(cands) > 0 && len(b.runs)%len(cands) == 0 {
				stats[cands[i%len(cands)]].record(r)
				continue
			}
			u := unattributed[event]
			if u == nil {
				u = &hookStat{Event: event}
				unattributed[event] = u
				order = append(order, event)
			}
			u.record(r)
		}
	}
	for _, e := range order {
		stats = append(stats, *unattributed[e])
	}
	if len(stats) > 100 {
		stats = stats[:100]
	}
	return stats
}

// mcpServerSpec is one server of the repo's .mcp.json.
type mcpServerSpec struct {
	Type    string   `json:"type"`
	Command string   `json:"command"`
	Args    []string `json:"args"`
	URL     string   `json:"url"`
}

// mcpIdentity is the catalogue identity (onboarding-interfaces.md §3): stdio:<command>:<package> or url:<origin>.
func mcpIdentity(s mcpServerSpec) string {
	if s.URL != "" || s.Type == "http" || s.Type == "sse" || s.Type == "ws" {
		if u, err := url.Parse(s.URL); err == nil && u.Host != "" {
			return "url:" + u.Scheme + "://" + u.Host
		}
		return "url:" + s.URL
	}
	pkg := ""
	for _, a := range s.Args {
		if !strings.HasPrefix(a, "-") {
			pkg = a
			break
		}
	}
	return "stdio:" + s.Command + ":" + pkg
}

// mcpSkipped lists the repo's .mcp.json servers that this run does not start (all but the auto-allowed ones).
func mcpSkipped(repo string, allowed []sandbox.MCPAutoAllow) []map[string]any {
	out := []map[string]any{}
	b, err := os.ReadFile(filepath.Join(repo, ".mcp.json"))
	if err != nil {
		return out
	}
	var doc struct {
		MCPServers map[string]mcpServerSpec `json:"mcpServers"`
	}
	if json.Unmarshal(b, &doc) != nil {
		return append(out, map[string]any{"name": ".mcp.json", "identity": "file:.mcp.json", "reason": "invalid"})
	}
	var names []string
	for n := range doc.MCPServers {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, n := range names {
		s := doc.MCPServers[n]
		ok := false
		for _, a := range allowed {
			ok = ok || (a.Name == n && a.Command == s.Command && slicesEqual(a.Args, s.Args) && s.URL == "")
		}
		if !ok && len(out) < 32 {
			out = append(out, map[string]any{"name": truncate(n, 128), "identity": truncate(mcpIdentity(s), 512), "reason": "not_allowed"})
		}
	}
	return out
}

func slicesEqual(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// settingsHash is sha256 over the JCS-canonical managed settings followed by the project settings.json bytes.
func settingsHash(managed map[string]any, project []byte) string {
	b, _ := json.Marshal(managed)
	canon, err := jcsx.Canonical(b)
	if err != nil {
		canon = b
	}
	d := sha256.Sum256(append(append([]byte{}, canon...), project...))
	return "sha256:" + hex.EncodeToString(d[:])
}

// loadInstructions fetches the InstructionsLoaded reports kept by agentd into rc.instructions.
func (rc *runCtx) loadInstructions(ctx context.Context) {
	cctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	var got ipc.Instructions
	if err := rc.agentd.Do(cctx, http.MethodGet, "/v1/report/instructions", nil, nil, &got); err != nil {
		log.Warn("instructions report unavailable", "err", err)
		return
	}
	rc.mu.Lock()
	defer rc.mu.Unlock()
	rc.instructions = instructionPaths(got.Reports)
}

// instructionPaths is the distinct loaded paths, in load order, within the schema's limits (50 × 512).
func instructionPaths(reps []ipc.InstructionsReport) []string {
	var out []string
	seen := map[string]bool{}
	for _, r := range reps {
		if r.FilePath == "" || seen[r.FilePath] || len(out) >= 50 {
			continue
		}
		seen[r.FilePath] = true
		out = append(out, truncate(r.FilePath, 512))
	}
	return out
}

// repoConfigSection renders cli.repo_config (onboarding-interfaces.md §6); nil unless repo_config is use.
func (rc *runCtx) repoConfigSection() map[string]any {
	p := rc.plan
	if p == nil || !p.RepoConfigUse() {
		return nil
	}
	repo := sandbox.RepoDir
	if rc.repoDir != "" {
		repo = rc.repoDir
	}
	raw, ps := readProjectSettings(repo)
	platform := mcpServers(p, rc.bs)
	allowed := autoAllowedMCP(p, platform)
	rc.mu.Lock()
	instr := append([]string{}, rc.instructions...)
	initSkills := rc.initSkills
	rc.mu.Unlock()
	var rules, claudeMD []string
	for _, f := range instr {
		rel, ok := strings.CutPrefix(f, repo+"/")
		if !ok {
			continue
		}
		if i := strings.Index("/"+rel, "/.claude/rules/"); i >= 0 {
			rules = append(rules, ("/" + rel)[i+len("/.claude/rules/"):])
		} else if b := filepath.Base(rel); b == "CLAUDE.md" || b == "CLAUDE.local.md" {
			claudeMD = append(claudeMD, rel)
		}
	}
	skills := []string{}
	if ents, err := os.ReadDir(filepath.Join(repo, ".claude", "skills")); err == nil {
		for _, e := range ents {
			if _, err := os.Stat(filepath.Join(repo, ".claude", "skills", e.Name(), "SKILL.md")); err != nil {
				continue
			}
			if initSkills != nil && !contains(initSkills, e.Name()) {
				continue
			}
			skills = append(skills, truncate(e.Name(), 128))
		}
	}
	mcpAllowed := []string{}
	for _, a := range allowed {
		mcpAllowed = append(mcpAllowed, a.Name)
	}
	permIgnored := 0
	if ps != nil {
		permIgnored = len(ps.Permissions.Allow) + len(ps.Permissions.Deny) + len(ps.Permissions.Ask)
	}
	var base any
	if p.ConfigBaseSHA != "" {
		base = p.ConfigBaseSHA
	}
	hooks := rc.hooks
	if hooks == nil {
		hooks = newHookTracker()
	}
	return map[string]any{
		"mode": "use", "config_base_sha": base,
		"settings_hash":            settingsHash(managedSettings(p, platform), raw),
		"hooks":                    hooks.hookStats(ps.commandHooks()),
		"rules":                    nonNil(capStrings(rules, 100)),
		"skills":                   capStrings(skills, 100),
		"claude_md":                nonNil(capStrings(claudeMD, 100)),
		"mcp_skipped":              mcpSkipped(repo, allowed),
		"mcp_allowed":              mcpAllowed,
		"permission_rules_ignored": permIgnored,
		"hook_events_available":    true, // 2.1.310 emits hook_started/hook_response with --include-hook-events
	}
}

func contains(s []string, v string) bool {
	for _, x := range s {
		if x == v {
			return true
		}
	}
	return false
}

func capStrings(s []string, n int) []string {
	if len(s) > n {
		return s[:n]
	}
	return s
}

// managedHookMarked reports whether this run's managed hooks print managedHookMarker (repo_config use).
func managedHookMarked() bool {
	p, _, err := readPlan()
	return err == nil && p.RepoConfigUse()
}
