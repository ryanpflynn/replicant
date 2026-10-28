package shim

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"example.com/replicant/internal/runner/ids"
	"gopkg.in/yaml.v3"
)

// Script is replicant.gateway-script/v1 (gateway.md §8.2).
type Script struct {
	Schema   string `yaml:"schema"`
	Name     string `yaml:"name"`
	Protocol string `yaml:"protocol"`
	// Finish is the text returned after the agent's StructuredOutput call is answered (default "Done.").
	Finish string `yaml:"finish"`
	Turns  []Turn `yaml:"turns"`

	dir string
}

// Turn is one scripted response.
type Turn struct {
	// Match (service callers only, e.g. worker_llm): a regexp tried against the request body; the first turn
	// whose Match hits (or that has no Match) answers. Stateless: no per-run cursor.
	Match   string   `yaml:"match"`
	Expect  *Expect  `yaml:"expect"`
	Respond *Respond `yaml:"respond"`
	Fault   *Fault   `yaml:"fault"`
}

// Expect is checked against the request; a mismatch is 599 cassette_miss.
type Expect struct {
	Model string   `yaml:"model"`
	Tools []string `yaml:"tools"` // tool names that must be offered
}

// ToolUse is one tool call.
type ToolUse struct {
	Name  string         `yaml:"name" json:"name"`
	Input map[string]any `yaml:"input" json:"input"`
}

// Respond is an assistant message.
type Respond struct {
	Text                 string    `yaml:"text"`
	ToolUse              *ToolUse  `yaml:"tool_use"`
	ToolUses             []ToolUse `yaml:"tool_uses"`
	Usage                *Usage    `yaml:"usage"`
	StopReason           string    `yaml:"stop_reason"`
	StructuredOutputFrom string    `yaml:"structured_output_from"`
	StructuredOutput     any       `yaml:"structured_output"`
}

// ScriptBackend serves scripted Anthropic Messages responses (`--upstream-backend=script`).
type ScriptBackend struct {
	cfg *Config
	st  *MemState

	mu    sync.Mutex
	cache map[string]cachedScript
}

type cachedScript struct {
	s     *Script
	mtime time.Time
}

// NewScriptBackend builds the backend.
func NewScriptBackend(cfg *Config, st *MemState) *ScriptBackend {
	return &ScriptBackend{cfg: cfg, st: st, cache: map[string]cachedScript{}}
}

// Name is the backend name.
func (b *ScriptBackend) Name() string { return "script" }

var reSafe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`)

// candidates lists the script files tried for a stub, most specific first:
// <root>/<set>/<key>.yaml, then the key with trailing ".segment"s removed (implementer.c0.n1 -> implementer.c0
// -> implementer), then <set>/<role>.yaml.
func (b *ScriptBackend) candidates(stub *Stub, role string) []string {
	var out []string
	roots := []string{}
	if stub.Root != "" {
		if d, ok := b.cfg.CassetteRoots[stub.Root]; ok {
			roots = append(roots, d)
		}
	}
	if d, ok := b.cfg.CassetteRoots["scripts"]; ok && stub.Root != "scripts" {
		roots = append(roots, d) // the script backend reads scripts even when the worker selected root "tests"
	}
	if !reSafe.MatchString(stub.Set) {
		return nil
	}
	for _, root := range roots {
		key := stub.Key
		for key != "" && reSafe.MatchString(key) {
			out = append(out, filepath.Join(root, stub.Set, key+".yaml"))
			i := strings.LastIndex(key, ".")
			if i <= 0 {
				break
			}
			key = key[:i]
		}
		if role != "" && reSafe.MatchString(role) {
			out = append(out, filepath.Join(root, stub.Set, role+".yaml"))
		}
	}
	return out
}

func (b *ScriptBackend) load(path string) (*Script, error) {
	st, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	b.mu.Lock()
	c, ok := b.cache[path]
	b.mu.Unlock()
	if ok && c.mtime.Equal(st.ModTime()) {
		return c.s, nil
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var s Script
	if err := yaml.Unmarshal(raw, &s); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	if s.Schema != "replicant.gateway-script/v1" {
		return nil, fmt.Errorf("%s: schema %q is not replicant.gateway-script/v1", path, s.Schema)
	}
	s.dir = filepath.Dir(path)
	b.mu.Lock()
	b.cache[path] = cachedScript{s: &s, mtime: st.ModTime()}
	b.mu.Unlock()
	return &s, nil
}

// Resolve finds the script for a request.
func (b *ScriptBackend) Resolve(d *DataRequest) (*Script, []string, error) {
	var stub *Stub
	if d.Policy != nil && d.Policy.Stub != nil {
		stub = d.Policy.Stub
	} else if b.cfg.ScriptDefaultSet != "" {
		key := d.Role
		if d.Header != nil {
			if k := d.Header.Get("X-Replicant-Script-Key"); k != "" && reSafe.MatchString(k) {
				key = k // service callers name their script (feedback_triage, memory_extraction); falls back to the purpose
			}
		}
		stub = &Stub{Root: "scripts", Set: b.cfg.ScriptDefaultSet, Key: key}
	}
	if stub == nil {
		return nil, nil, fmt.Errorf("no stub in the run policy and REPLICANT_GATEWAY_SCRIPT_DEFAULT_SET unset")
	}
	cands := b.candidates(stub, d.Role)
	for _, p := range cands {
		if s, err := b.load(p); err == nil {
			return s, cands, nil
		} else if !os.IsNotExist(err) {
			return nil, cands, err
		}
	}
	return nil, cands, fmt.Errorf("no script for stub %s/%s/%s", stub.Root, stub.Set, stub.Key)
}

// --- request inspection -------------------------------------------------------------------------------

type msgBlock struct {
	Type      string          `json:"type"`
	ID        string          `json:"id"`
	Name      string          `json:"name"`
	ToolUseID string          `json:"tool_use_id"`
	Text      string          `json:"text"`
	Input     json.RawMessage `json:"input"`
}

type message struct {
	Role    string          `json:"role"`
	Content json.RawMessage `json:"content"`
}

func (m message) blocks() []msgBlock {
	var bs []msgBlock
	if json.Unmarshal(m.Content, &bs) == nil {
		return bs
	}
	var s string
	if json.Unmarshal(m.Content, &s) == nil {
		return []msgBlock{{Type: "text", Text: s}}
	}
	return nil
}

type anthropicRequest struct {
	Model    string          `json:"model"`
	Messages []message       `json:"messages"`
	System   json.RawMessage `json:"system"`
	Tools    []struct {
		Name string `json:"name"`
	} `json:"tools"`
}

var reCwd = regexp.MustCompile(`Primary working directory: (\S+)`)

func (r *anthropicRequest) cwd() string {
	var s string
	if json.Unmarshal(r.System, &s) != nil {
		var bs []msgBlock
		if json.Unmarshal(r.System, &bs) == nil {
			for _, b := range bs {
				s += b.Text + "\n"
			}
		}
	}
	if m := reCwd.FindStringSubmatch(s); m != nil {
		return m[1]
	}
	// Claude Code 2.1.3xx sends the environment block as a `role: "system"` entry inside `messages`.
	for _, m := range r.Messages {
		if m.Role != "system" && m.Role != "user" {
			continue
		}
		for _, b := range m.blocks() {
			if mm := reCwd.FindStringSubmatch(b.Text); mm != nil {
				return mm[1]
			}
		}
	}
	return ""
}

func (r *anthropicRequest) hasTool(name string) bool {
	for _, t := range r.Tools {
		if t.Name == name {
			return true
		}
	}
	return false
}

// answeredStructuredOutput reports whether the last user message answers a StructuredOutput call.
func (r *anthropicRequest) answeredStructuredOutput() bool {
	n := len(r.Messages)
	if n < 2 || r.Messages[n-1].Role != "user" {
		return false
	}
	so := map[string]bool{}
	for i := n - 2; i >= 0; i-- {
		if r.Messages[i].Role == "assistant" {
			for _, b := range r.Messages[i].blocks() {
				if b.Type == "tool_use" && b.Name == structuredOutputTool {
					so[b.ID] = true
				}
			}
			break
		}
	}
	for _, b := range r.Messages[n-1].blocks() {
		if b.Type == "tool_result" && so[b.ToolUseID] {
			return true
		}
	}
	return false
}

const structuredOutputTool = "StructuredOutput"

// --- serving ------------------------------------------------------------------------------------------

func (b *ScriptBackend) miss(w http.ResponseWriter, d *DataRequest, msg string, keys any) Result {
	kb, _ := json.Marshal(keys)
	writeError(w, d.Protocol, 599, "cassette_miss", msg, func(fe *ReplicantError) { fe.Keys = kb })
	return Result{Status: 599, Faulted: true}
}

// Serve picks the turn and renders it.
func (b *ScriptBackend) Serve(w http.ResponseWriter, r *http.Request, d *DataRequest) Result {
	switch d.Protocol {
	case "anthropic_count_tokens":
		body := fmt.Sprintf(`{"input_tokens":%d}`, len(d.Body)/4)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		_, _ = w.Write([]byte(body))
		return Result{Status: 200, UsageParsed: true}
	case "anthropic_messages":
	default:
		return b.miss(w, d, "the script backend serves anthropic_messages only in v0", map[string]string{"protocol": d.Protocol})
	}
	var req anthropicRequest
	if err := json.Unmarshal(d.Body, &req); err != nil {
		writeError(w, d.Protocol, 400, "cassette_miss", "request body is not JSON", nil)
		return Result{Status: 400, Faulted: true}
	}
	script, cands, err := b.Resolve(d)
	if err != nil {
		return b.miss(w, d, err.Error(), map[string]any{"candidates": relPaths(cands, b.cfg.CassetteRoots)})
	}
	vars := map[string]string{"cwd": req.cwd()}
	if vars["cwd"] == "" {
		vars["cwd"] = "/workspace/repo"
	}
	vars["repo"] = vars["cwd"]

	// Service callers (no run policy: worker_llm, eval_judge) are tool-less by rule: answer with the first turn whose
	// Match regexp hits the request body (a turn without Match is the default). Stateless and deterministic.
	if d.Policy == nil && len(req.Tools) == 0 {
		for i := range script.Turns {
			t := &script.Turns[i]
			if t.Respond == nil {
				continue
			}
			if t.Match != "" {
				re, err := regexp.Compile(t.Match)
				if err != nil {
					return b.miss(w, d, fmt.Sprintf("turn %d: bad match regexp: %v", i, err), nil)
				}
				if !re.Match(d.Body) {
					continue
				}
			}
			return b.render(w, d, &req, t.Respond, "svc"+strconv.Itoa(i), vars, script)
		}
		return b.miss(w, d, fmt.Sprintf("script %s: no turn matches the request", script.Name),
			map[string]any{"script": script.Name})
	}
	// Auxiliary requests (no tools: titles, summaries, classifiers) never consume a turn.
	if len(req.Tools) == 0 {
		return b.render(w, d, &req, &Respond{Text: "OK."}, "aux", vars, script)
	}
	// The CLI answers our StructuredOutput call with a tool_result and asks once more: finish the turn.
	if req.answeredStructuredOutput() {
		fin := script.Finish
		if fin == "" {
			fin = "Done."
		}
		return b.render(w, d, &req, &Respond{Text: fin}, "finish", vars, script)
	}

	// Turn selection: a per-run cursor over main-loop requests. A retried request (same number of messages)
	// replays the turn it was served; a fault turn is consumed once, so the retry after it gets the next turn.
	var turn *Turn
	var idx int
	nmsg := len(req.Messages)
	b.st.WithScriptCursor(d.RunKey+"|"+script.Name, func(c *scriptCursor) {
		if i, ok := c.served[nmsg]; ok {
			idx = i
			turn = &script.Turns[i]
			return
		}
		idx = c.next
		if idx >= len(script.Turns) {
			return
		}
		turn = &script.Turns[idx]
		c.next++
		if turn.Fault == nil {
			c.served[nmsg] = idx
		}
	})
	if turn == nil {
		return b.miss(w, d, fmt.Sprintf("script %s exhausted after %d turns", script.Name, len(script.Turns)),
			map[string]any{"script": script.Name, "request_messages": nmsg})
	}
	if turn.Expect != nil {
		if turn.Expect.Model != "" && turn.Expect.Model != d.Model {
			return b.miss(w, d, fmt.Sprintf("turn %d expects model %s, got %s", idx, turn.Expect.Model, d.Model),
				map[string]any{"script": script.Name, "turn": idx, "model": d.Model})
		}
		for _, t := range turn.Expect.Tools {
			if !req.hasTool(t) {
				return b.miss(w, d, fmt.Sprintf("turn %d expects tool %s to be offered", idx, t),
					map[string]any{"script": script.Name, "turn": idx})
			}
		}
	}
	if turn.Fault != nil {
		writeFault(w, d.Protocol, turn.Fault)
		return Result{Status: turn.Fault.Status, Faulted: true}
	}
	if turn.Respond == nil {
		return b.miss(w, d, fmt.Sprintf("turn %d has neither respond nor fault", idx), nil)
	}
	return b.render(w, d, &req, turn.Respond, strconv.Itoa(idx), vars, script)
}

func relPaths(ps []string, roots map[string]string) []string {
	out := make([]string, 0, len(ps))
	for _, p := range ps {
		for name, dir := range roots {
			if rel, err := filepath.Rel(dir, p); err == nil && !strings.HasPrefix(rel, "..") {
				p = name + "/" + rel
				break
			}
		}
		out = append(out, p)
	}
	return out
}

// substitute replaces ${cwd} / ${repo} in every string of v.
func substitute(v any, vars map[string]string) any {
	switch t := v.(type) {
	case string:
		for k, val := range vars {
			t = strings.ReplaceAll(t, "${"+k+"}", val)
		}
		return t
	case map[string]any:
		out := make(map[string]any, len(t))
		for k, x := range t {
			out[k] = substitute(x, vars)
		}
		return out
	case []any:
		out := make([]any, len(t))
		for i, x := range t {
			out[i] = substitute(x, vars)
		}
		return out
	}
	return v
}

type contentBlock struct {
	Type  string `json:"type"`
	Text  string `json:"text,omitempty"`
	ID    string `json:"id,omitempty"`
	Name  string `json:"name,omitempty"`
	Input any    `json:"input,omitempty"`
}

func (b *ScriptBackend) render(w http.ResponseWriter, d *DataRequest, req *anthropicRequest, resp *Respond, turnTag string, vars map[string]string, script *Script) Result {
	var blocks []contentBlock
	if resp.Text != "" {
		blocks = append(blocks, contentBlock{Type: "text", Text: substitute(resp.Text, vars).(string)})
	}
	tus := append([]ToolUse(nil), resp.ToolUses...)
	if resp.ToolUse != nil {
		tus = append(tus, *resp.ToolUse)
	}
	seed := d.RunKey + "|" + script.Name + "|" + turnTag + "|" + strconv.Itoa(len(req.Messages))
	for i, tu := range tus {
		in := substitute(map[string]any(tu.Input), vars)
		if in == nil {
			in = map[string]any{}
		}
		blocks = append(blocks, contentBlock{Type: "tool_use", ID: "toolu_" + ids.ULIDFromSeed(seed+"|"+strconv.Itoa(i)), Name: tu.Name, Input: in})
	}
	if so, err := structuredOutput(resp, script); err != nil {
		return b.miss(w, d, err.Error(), nil)
	} else if so != nil {
		so = substitute(so, vars)
		if req.hasTool(structuredOutputTool) {
			blocks = append(blocks, contentBlock{Type: "tool_use", ID: "toolu_" + ids.ULIDFromSeed(seed+"|so"), Name: structuredOutputTool, Input: so})
		} else {
			j, _ := json.Marshal(so)
			blocks = append(blocks, contentBlock{Type: "text", Text: string(j)})
		}
	}
	stop := resp.StopReason
	if stop == "" {
		stop = "end_turn"
		for _, bl := range blocks {
			if bl.Type == "tool_use" {
				stop = "tool_use"
			}
		}
	}
	u := Usage{InputTokens: int64(len(d.Body)/4 + 1), OutputTokens: 1}
	for _, bl := range blocks {
		j, _ := json.Marshal(bl)
		u.OutputTokens += int64(len(j) / 4)
	}
	if resp.Usage != nil {
		u = *resp.Usage
	}
	served := b.cfg.UpstreamModel(d.Model)
	msgID := "msg_" + ids.ULIDFromSeed(seed+"|msg")
	if d.Stream {
		writeSSE(w, msgID, served, blocks, stop, u)
	} else {
		body := map[string]any{
			"id": msgID, "type": "message", "role": "assistant", "model": served, "content": blocks,
			"stop_reason": stop, "stop_sequence": nil,
			"usage": map[string]any{"input_tokens": u.InputTokens, "output_tokens": u.OutputTokens,
				"cache_read_input_tokens": u.CacheReadTokens, "cache_creation_input_tokens": u.CacheWriteTokens},
		}
		j, _ := json.Marshal(body)
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("X-Replicant-Served-Model", served)
		w.Header().Set("request-id", "req_script_"+ids.ULIDFromSeed(seed))
		w.WriteHeader(200)
		_, _ = w.Write(j)
	}
	return Result{Status: 200, Usage: u, UsageParsed: true, ServedModel: served, UpstreamRequestID: "req_script_" + ids.ULIDFromSeed(seed)}
}

func structuredOutput(resp *Respond, script *Script) (any, error) {
	if resp.StructuredOutput != nil {
		return normalizeYAML(resp.StructuredOutput), nil
	}
	if resp.StructuredOutputFrom == "" {
		return nil, nil
	}
	name := resp.StructuredOutputFrom
	if filepath.IsAbs(name) || strings.Contains(name, "..") {
		return nil, fmt.Errorf("structured_output_from must be a file next to the script")
	}
	raw, err := os.ReadFile(filepath.Join(script.dir, name))
	if err != nil {
		return nil, fmt.Errorf("structured_output_from: %w", err)
	}
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		return nil, fmt.Errorf("structured_output_from %s: %w", name, err)
	}
	return v, nil
}

// normalizeYAML converts map[any]any trees (not produced by yaml.v3, but be safe) to JSON-able values.
func normalizeYAML(v any) any {
	switch t := v.(type) {
	case map[string]any:
		for k, x := range t {
			t[k] = normalizeYAML(x)
		}
		return t
	case map[any]any:
		out := map[string]any{}
		for k, x := range t {
			out[fmt.Sprint(k)] = normalizeYAML(x)
		}
		return out
	case []any:
		for i, x := range t {
			t[i] = normalizeYAML(x)
		}
	}
	return v
}

// writeSSE renders an Anthropic Messages stream in provider order.
func writeSSE(w http.ResponseWriter, msgID, model string, blocks []contentBlock, stop string, u Usage) {
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Trailer", "X-Replicant-Served-Model")
	w.WriteHeader(200)
	fl, _ := w.(http.Flusher)
	ev := func(name string, data any) {
		j, _ := json.Marshal(data)
		fmt.Fprintf(w, "event: %s\ndata: %s\n\n", name, j)
		if fl != nil {
			fl.Flush()
		}
	}
	ev("message_start", map[string]any{"type": "message_start", "message": map[string]any{
		"id": msgID, "type": "message", "role": "assistant", "model": model, "content": []any{},
		"stop_reason": nil, "stop_sequence": nil,
		"usage": map[string]any{"input_tokens": u.InputTokens, "output_tokens": 1,
			"cache_read_input_tokens": u.CacheReadTokens, "cache_creation_input_tokens": u.CacheWriteTokens}}})
	ev("ping", map[string]any{"type": "ping"})
	for i, bl := range blocks {
		switch bl.Type {
		case "text":
			ev("content_block_start", map[string]any{"type": "content_block_start", "index": i, "content_block": map[string]any{"type": "text", "text": ""}})
			ev("content_block_delta", map[string]any{"type": "content_block_delta", "index": i, "delta": map[string]any{"type": "text_delta", "text": bl.Text}})
		case "tool_use":
			ev("content_block_start", map[string]any{"type": "content_block_start", "index": i, "content_block": map[string]any{"type": "tool_use", "id": bl.ID, "name": bl.Name, "input": map[string]any{}}})
			j, _ := json.Marshal(bl.Input)
			ev("content_block_delta", map[string]any{"type": "content_block_delta", "index": i, "delta": map[string]any{"type": "input_json_delta", "partial_json": string(j)}})
		}
		ev("content_block_stop", map[string]any{"type": "content_block_stop", "index": i})
	}
	ev("message_delta", map[string]any{"type": "message_delta", "delta": map[string]any{"stop_reason": stop, "stop_sequence": nil},
		"usage": map[string]any{"output_tokens": u.OutputTokens}})
	ev("message_stop", map[string]any{"type": "message_stop"})
	w.Header().Set("X-Replicant-Served-Model", model)
}
