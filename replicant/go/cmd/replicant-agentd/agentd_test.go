package main

import (
	"encoding/base64"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"example.com/replicant/internal/runner/ipc"
)

func TestRedactKnown(t *testing.T) {
	a := &Agent{secretsSeen: map[string]bool{"eyJsecret-token-value": true}}
	in := "x eyJsecret-token-value y " + base64.StdEncoding.EncodeToString([]byte("eyJsecret-token-value"))
	out, hits := a.redactKnown(in)
	if hits != 2 || strings.Contains(out, "secret-token") || !strings.Contains(out, "[REDACTED:known-credential:") {
		t.Fatalf("%d %s", hits, out)
	}
}

func TestUsageParserSSE(t *testing.T) {
	a := &Agent{}
	p := &usageParser{a: a, sse: true}
	stream := "event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"usage\":{\"input_tokens\":100,\"cache_read_input_tokens\":5}}}\n\n" +
		"event: message_delta\ndata: {\"type\":\"message_delta\",\"usage\":{\"output_tokens\":7}}\n\n"
	for i := 0; i < len(stream); i += 13 { // arbitrary chunking
		p.feed([]byte(stream[i:min(i+13, len(stream))]))
	}
	p.done()
	if a.usage.ModelCalls != 1 || a.usage.InputTokens != 100 || a.usage.OutputTokens != 7 || a.usage.CacheReadTokens != 5 {
		t.Fatalf("%+v", a.usage)
	}
}

func TestSanitizeDropsCredentials(t *testing.T) {
	h := map[string][]string{"Authorization": {"Bearer x"}, "X-Api-Key": {"k"}, "X-Replicant-Run-Id": {"run_evil"}, "Anthropic-Version": {"2023-06-01"}}
	out := sanitize(h)
	if out.Get("Authorization") != "" || out.Get("X-Api-Key") != "" || out.Get("X-Replicant-Run-Id") != "" || out.Get("Anthropic-Version") == "" {
		t.Fatalf("%v", out)
	}
}

func TestInstructionsReportsKept(t *testing.T) {
	a := &Agent{log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	a.evCond = sync.NewCond(&a.evMu)
	h := a.ipcHandler()
	for _, body := range []string{`{"hook_event_name":"InstructionsLoaded","file_path":"/workspace/repo/CLAUDE.md","memory_type":"Project","load_reason":"session_start"}`,
		`not json`, `{"file_path":""}`,
		`{"file_path":"/workspace/repo/src/CLAUDE.md","load_reason":"nested_traversal","trigger_file_path":"/workspace/repo/src/a.go"}`} {
		rr := httptest.NewRecorder()
		h.ServeHTTP(rr, httptest.NewRequest(http.MethodPost, "/v1/report/instructions", strings.NewReader(body)))
		if rr.Code != 204 {
			t.Fatalf("POST %d", rr.Code)
		}
	}
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/v1/report/instructions", nil))
	var got ipc.Instructions
	if err := json.Unmarshal(rr.Body.Bytes(), &got); err != nil || len(got.Reports) != 2 ||
		got.Reports[0].FilePath != "/workspace/repo/CLAUDE.md" || got.Reports[1].LoadReason != "nested_traversal" {
		t.Fatalf("%v %s", err, rr.Body.String())
	}
}
