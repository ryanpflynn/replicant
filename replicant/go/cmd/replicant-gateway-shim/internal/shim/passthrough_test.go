package shim

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

// fakeProvider imitates api.anthropic.com / api.openai.com and records what it received.
type fakeProvider struct {
	mu   sync.Mutex
	reqs []recorded
}

type recorded struct {
	path, apiKey, authz, replicantHdr, version, model string
}

func (f *fakeProvider) handler(w http.ResponseWriter, r *http.Request) {
	b, _ := io.ReadAll(r.Body)
	var body map[string]any
	_ = json.Unmarshal(b, &body)
	f.mu.Lock()
	model, _ := body["model"].(string)
	f.reqs = append(f.reqs, recorded{path: r.URL.String(), apiKey: r.Header.Get("x-api-key"), authz: r.Header.Get("Authorization"),
		replicantHdr: r.Header.Get("X-Replicant-Run-Id"), version: r.Header.Get("anthropic-version"), model: model})
	f.mu.Unlock()
	switch {
	case model == "rate-limited":
		w.Header().Set("retry-after", "7")
		w.Header().Set("anthropic-ratelimit-requests-remaining", "0")
		w.WriteHeader(429)
		_, _ = io.WriteString(w, `{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}`)
	case strings.HasSuffix(r.URL.Path, "/v1/responses"):
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "event: response.created\ndata: {\"type\":\"response.created\",\"response\":{\"model\":\"gpt-5-codex\"}}\n\n")
		fmt.Fprint(w, "event: response.completed\ndata: {\"type\":\"response.completed\",\"response\":{\"model\":\"gpt-5-codex\",\"usage\":{\"input_tokens\":100,\"input_tokens_details\":{\"cached_tokens\":40},\"output_tokens\":20,\"output_tokens_details\":{\"reasoning_tokens\":5}}}}\n\n")
	case body["stream"] == true:
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("request-id", "req_up_1")
		fmt.Fprint(w, "event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"id\":\"msg_1\",\"model\":\"claude-sonnet-5-5-20261001\",\"usage\":{\"input_tokens\":200,\"output_tokens\":1,\"cache_read_input_tokens\":50}}}\n\n")
		fmt.Fprint(w, "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"hi\"}}\n\n")
		fmt.Fprint(w, "event: message_delta\ndata: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"end_turn\"},\"usage\":{\"output_tokens\":30}}\n\n")
		fmt.Fprint(w, "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n")
	default:
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("request-id", "req_up_2")
		_, _ = io.WriteString(w, `{"id":"msg_2","type":"message","model":"claude-sonnet-5-5-20261001","content":[{"type":"text","text":"hi"}],"usage":{"input_tokens":1000,"output_tokens":100}}`)
	}
}

func passHarness(t *testing.T) (*harness, *fakeProvider, minted) {
	fp := &fakeProvider{}
	up := httptest.NewServer(http.HandlerFunc(fp.handler))
	t.Cleanup(up.Close)
	h := newHarness(t, "passthrough", up.URL, map[string]string{"ANTHROPIC_API_KEY": "sk-ant-test-key", "OPENAI_API_KEY": "sk-oa-test"})
	m := h.mint(runOpts{})
	mustStatus(t, h.policy(m, nil, "anthropic/claude-sonnet-5-5", "openai/gpt-5-codex-2025-09-15", "rate-limited"), 200)
	h.cfg.Prices["rate-limited"] = Price{Input: 1, Output: 1}
	return h, fp, m
}

func TestPassthroughNonStreaming(t *testing.T) {
	h, fp, m := passHarness(t)
	resp := h.do("POST", "/anthropic/v1/messages?beta=true", m.token, msgBody("anthropic/claude-sonnet-5-5", false, 1, true),
		map[string]string{"anthropic-version": "2023-06-01", "X-Replicant-Run-Id": m.runID})
	if resp.Header.Get("X-Replicant-Served-Model") != "claude-sonnet-5-5-20261001" || resp.Header.Get("X-Replicant-Budget-Run-Spent-Usd") != "0.004500" {
		t.Fatalf("headers %v", resp.Header)
	}
	out := mustStatus(t, resp, 200)
	if out["model"] != "claude-sonnet-5-5-20261001" {
		t.Fatal("body model must pass through unchanged (GW-14)")
	}
	r := fp.reqs[0]
	if r.path != "/v1/messages?beta=true" || r.apiKey != "sk-ant-test-key" || r.authz != "" || r.replicantHdr != "" || r.model != "claude-sonnet-5-5" || r.version != "2023-06-01" {
		t.Fatalf("upstream saw %+v", r)
	}
	ev := h.usage.all()
	if ev[0].UpstreamReqID != "req_up_2" || ev[0].Cost.MicrosUSD != 4500 || ev[0].ServedModel != "claude-sonnet-5-5-20261001" {
		t.Fatalf("%+v", ev[0])
	}
}

func TestPassthroughStreamingUsage(t *testing.T) {
	h, _, m := passHarness(t)
	resp := h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-sonnet-5-5", true, 1, true), nil)
	b, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if !strings.Contains(string(b), "message_stop") || resp.Trailer.Get("X-Replicant-Served-Model") != "claude-sonnet-5-5-20261001" {
		t.Fatalf("stream %q trailer %v", b, resp.Trailer)
	}
	ev := h.usage.all()
	u := ev[0].Usage
	// 200 in × 3 + 30 out × 15 + 50 cache_read × 0.3 = 600 + 450 + 15
	if u.InputTokens != 200 || u.OutputTokens != 30 || u.CacheReadTokens != 50 || ev[0].Cost.MicrosUSD != 1065 || ev[0].UsageSource != "parsed" {
		t.Fatalf("%+v", ev[0])
	}
}

// GW-09: upstream 429 with retry-after is passed through without X-Replicant-Limit.
func TestGW09UpstreamRateLimitPassthrough(t *testing.T) {
	h, _, m := passHarness(t)
	resp := h.do("POST", "/anthropic/v1/messages", m.token, msgBody("rate-limited", false, 1, true), nil)
	if resp.Header.Get("retry-after") != "7" || resp.Header.Get("X-Replicant-Limit") != "" || resp.Header.Get("anthropic-ratelimit-requests-remaining") != "0" {
		t.Fatalf("headers %v", resp.Header)
	}
	out := mustStatus(t, resp, 429)
	if out["replicant_code"] != nil {
		t.Fatal("upstream body must be unchanged")
	}
	if ev := h.usage.all(); ev[0].Status != "rate_limited" || ev[0].Cost.MicrosUSD != 0 {
		t.Fatalf("%+v", ev[0])
	}
}

func TestPassthroughOpenAIResponses(t *testing.T) {
	h, fp, m := passHarness(t)
	body := map[string]any{"model": "openai/gpt-5-codex-2025-09-15", "stream": true, "input": "x"}
	resp := h.do("POST", "/openai/v1/responses", m.token, body, nil)
	_, _ = io.ReadAll(resp.Body)
	resp.Body.Close()
	if fp.reqs[0].authz != "Bearer sk-oa-test" || fp.reqs[0].model != "gpt-5-codex" {
		t.Fatalf("%+v", fp.reqs[0])
	}
	u := h.usage.all()[0].Usage
	if u.InputTokens != 60 || u.CacheReadTokens != 40 || u.OutputTokens != 20 || u.ReasoningTokens != 5 {
		t.Fatalf("%+v", u)
	}
}

func TestPassthroughMissingKey(t *testing.T) {
	h := newHarness(t, "passthrough", "", nil)
	m := h.mint(runOpts{})
	mustStatus(t, h.policy(m, nil), 200)
	out := mustStatus(t, h.do("POST", "/anthropic/v1/messages", m.token, msgBody("anthropic/claude-sonnet-5-5", false, 1, true), nil), 503)
	if out["replicant_code"] != "upstream_unavailable" {
		t.Fatal(out)
	}
	if b, _ := json.Marshal(out); strings.Contains(string(b), "sk-") {
		t.Fatal("key leaked")
	}
}

func TestPassthroughKeyFromFile(t *testing.T) {
	h := newHarness(t, "passthrough", "", map[string]string{"ANTHROPIC_API_KEY_FILE": ""})
	writeFile(t, h.dir+"/key", "sk-from-file\n")
	t.Setenv("ANTHROPIC_API_KEY_FILE", h.dir+"/key")
	pb := h.srv.Backend.(*PassthroughBackend)
	if pb.key("ANTHROPIC_API_KEY") != "sk-from-file" {
		t.Fatal("key file not read")
	}
}
