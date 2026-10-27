package usage

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/klauspost/compress/zstd"
)

func ev(id string) Event {
	return Event{Schema: "replicant.gateway-usage/v1", EventID: id, OccurredAt: "2026-10-24T13:05:00Z", TenantID: "acme-payments",
		Caller: Caller{Kind: "run"}, GatewayModelID: "anthropic/claude-sonnet-5-5", Protocol: "anthropic_messages", Status: "ok",
		HTTPStatus: 200, Cost: Cost{MicrosUSD: 1, Source: "price_book"}, Purpose: "agent", UsageSource: "parsed"}
}

func decode(t *testing.T, b []byte) []Event {
	t.Helper()
	zr, _ := zstd.NewReader(nil)
	raw, err := zr.DecodeAll(b, nil)
	if err != nil {
		t.Fatal(err)
	}
	var out []Event
	for _, l := range bytes.Split(bytes.TrimSpace(raw), []byte("\n")) {
		var e Event
		if err := json.Unmarshal(l, &e); err != nil {
			t.Fatal(err)
		}
		out = append(out, e)
	}
	return out
}

func TestFileSinkSegmentLayout(t *testing.T) {
	dir := t.TempDir()
	l := NewLog(Options{Sink: FileSink{Dir: dir}, Prefix: "platform/gateway-usage/", Pod: "gw-1"})
	_ = l.Append(ev("gwu_a"))
	_ = l.Append(ev("gwu_b"))
	if err := l.Flush(context.Background()); err != nil {
		t.Fatal(err)
	}
	var files []string
	_ = filepath.Walk(dir, func(p string, i os.FileInfo, err error) error {
		if err == nil && !i.IsDir() {
			rel, _ := filepath.Rel(dir, p)
			files = append(files, rel)
		}
		return nil
	})
	if len(files) != 1 || !regexp.MustCompile(`^platform/gateway-usage/2026/10/24/13/gw-1-\d{10}\.ndjson\.zst$`).MatchString(files[0]) {
		t.Fatalf("files %v", files)
	}
	b, _ := os.ReadFile(filepath.Join(dir, files[0]))
	if got := decode(t, b); len(got) != 2 || got[1].EventID != "gwu_b" {
		t.Fatalf("%+v", got)
	}
	if l.Written() != 2 || !l.Healthy() {
		t.Fatal("state")
	}
}

type fakeS3 struct {
	mu        sync.Mutex
	objects   map[string][]byte
	rejectINM bool
	authz     []string
}

func (f *fakeS3) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.authz = append(f.authz, r.Header.Get("Authorization"))
	if r.Header.Get("If-None-Match") == "*" && f.rejectINM {
		w.WriteHeader(501)
		_, _ = io.WriteString(w, "<Error><Code>NotImplemented</Code></Error>")
		return
	}
	if !strings.HasPrefix(r.Header.Get("Authorization"), "AWS4-HMAC-SHA256 Credential=AKIDTEST/") || r.Header.Get("x-amz-content-sha256") == "" ||
		r.Header.Get("x-amz-tagging") != "replicant-retention=r7d&replicant-type=gateway_usage_log&replicant-tenant=replicant-system" ||
		!strings.Contains(r.Header.Get("Authorization"), "x-amz-tagging") || r.Header.Get("x-amz-checksum-sha256") == "" {
		w.WriteHeader(403)
		return
	}
	b, _ := io.ReadAll(r.Body)
	f.objects[r.URL.Path] = b
	w.WriteHeader(200)
}

func TestS3SinkPathStyleAndConditionalFallback(t *testing.T) {
	fs := &fakeS3{objects: map[string][]byte{}, rejectINM: true}
	srv := httptest.NewServer(fs)
	defer srv.Close()
	t.Setenv("AWS_ACCESS_KEY_ID", "")
	cred := filepath.Join(t.TempDir(), "creds")
	_ = os.WriteFile(cred, []byte("[default]\naws_access_key_id = AKIDTEST\naws_secret_access_key = secret\n"), 0o600)
	t.Setenv("AWS_SHARED_CREDENTIALS_FILE", cred)
	bucket, prefix, err := ParseS3URI("s3://local-replicant-local-artifacts/platform/gateway-usage/")
	if err != nil || bucket != "local-replicant-local-artifacts" || prefix != "platform/gateway-usage/" {
		t.Fatal(bucket, prefix, err)
	}
	sink := &S3Sink{Endpoint: srv.URL, Bucket: bucket, Region: "us-east-1", PathStyle: true, Creds: CredentialsFromEnv}
	l := NewLog(Options{Sink: sink, Prefix: prefix, Pod: "gw", Interval: time.Hour})
	_ = l.Append(ev("gwu_1"))
	if err := l.Flush(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(fs.objects) != 1 {
		t.Fatalf("objects %v", fs.objects)
	}
	for k, v := range fs.objects {
		if !strings.HasPrefix(k, "/local-replicant-local-artifacts/platform/gateway-usage/2026/10/24/13/gw-") {
			t.Fatal(k)
		}
		if decode(t, v)[0].EventID != "gwu_1" {
			t.Fatal("content")
		}
	}
	for _, a := range fs.authz {
		if strings.Contains(a, "secret") {
			t.Fatal("secret in header")
		}
	}
}

func TestFailClosedAfterSixtySeconds(t *testing.T) {
	l := NewLog(Options{Sink: FileSink{Dir: "/proc/definitely/not/writable"}, Pod: "gw"})
	_ = l.Append(ev("gwu_1"))
	if err := l.Flush(context.Background()); err == nil {
		t.Fatal("expected failure")
	}
	if !l.Healthy() {
		t.Fatal("must stay healthy within 60 s")
	}
	l.mu.Lock()
	l.failSince = time.Now().Add(-61 * time.Second)
	l.mu.Unlock()
	if l.Healthy() {
		t.Fatal("must fail closed after 60 s")
	}
}

func TestFastPathPoster(t *testing.T) {
	var got []map[string]any
	var mu sync.Mutex
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		mu.Lock()
		got = append(got, body)
		mu.Unlock()
		if r.Header.Get("Authorization") != "Bearer ingest" {
			w.WriteHeader(401)
		}
	}))
	defer srv.Close()
	tok := filepath.Join(t.TempDir(), "tok")
	_ = os.WriteFile(tok, []byte("ingest"), 0o600)
	l := NewLog(Options{Sink: FileSink{Dir: t.TempDir()}, Pod: "gw", IngestURL: srv.URL, IngestTokenFile: tok})
	_ = l.Append(ev("gwu_1"))
	l.fast.flush(context.Background())
	mu.Lock()
	defer mu.Unlock()
	if len(got) != 1 || len(got[0]["events"].([]any)) != 1 || len(l.fast.q) != 0 {
		t.Fatalf("%v", got)
	}
}

// AWS SigV4 documentation, "Example: PUT Object" (examplebucket, 20130524T000000Z).
func TestSignV4AWSVector(t *testing.T) {
	body := []byte("Welcome to Amazon S3.")
	req, _ := http.NewRequest("PUT", "https://examplebucket.s3.amazonaws.com/test$file.text", bytes.NewReader(body))
	req.Header.Set("Date", "Fri, 24 May 2013 00:00:00 GMT")
	req.Header.Set("x-amz-storage-class", "REDUCED_REDUNDANCY")
	ts, _ := time.Parse("20060102T150405Z", "20130524T000000Z")
	SignV4(req, "examplebucket.s3.amazonaws.com", body, Credentials{AccessKeyID: "AKIAIOSFODNN7EXAMPLE", SecretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}, "us-east-1", "s3", ts)
	want := "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, SignedHeaders=date;host;x-amz-content-sha256;x-amz-date;x-amz-storage-class, Signature=98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"
	if got := req.Header.Get("Authorization"); got != want {
		t.Fatalf("got  %s\nwant %s", got, want)
	}
}
