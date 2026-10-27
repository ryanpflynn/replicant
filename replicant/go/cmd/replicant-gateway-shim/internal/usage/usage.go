// Package usage writes replicant.gateway-usage/v1 events to the durable usage log (C-ART `gateway_usage_log`,
// gateway.md §9.1) and posts them to usage-ingestor (fast path, best effort).
package usage

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/klauspost/compress/zstd"
)

// Event is schemas/gateway-usage-event.schema.json. Optional fields are omitted when empty.
type Event struct {
	Schema          string       `json:"schema"`
	EventID         string       `json:"event_id"`
	OccurredAt      string       `json:"occurred_at"`
	TenantID        string       `json:"tenant_id"`
	JobID           string       `json:"job_id,omitempty"`
	RunID           string       `json:"run_id,omitempty"`
	Role            string       `json:"role,omitempty"`
	ReleaseBundleID string       `json:"release_bundle_id,omitempty"`
	Caller          Caller       `json:"caller"`
	GatewayModelID  string       `json:"gateway_model_id"`
	Protocol        string       `json:"protocol"`
	Streaming       bool         `json:"streaming"`
	Status          string       `json:"status"`
	HTTPStatus      int          `json:"http_status"`
	UpstreamReqID   string       `json:"upstream_request_id,omitempty"`
	Usage           Tokens       `json:"usage"`
	Cost            Cost         `json:"cost"`
	BudgetAfter     *BudgetAfter `json:"budget_after,omitempty"`
	LatencyMs       int64        `json:"latency_ms"`
	TTFBMs          *int64       `json:"ttfb_ms,omitempty"`
	RetryAttempt    int          `json:"retry_attempt"`
	TraceID         string       `json:"trace_id,omitempty"`
	SpanID          string       `json:"span_id,omitempty"`
	ActingFor       string       `json:"acting_for,omitempty"`
	Purpose         string       `json:"purpose"`
	UsageSource     string       `json:"usage_source"`
	ServedModel     string       `json:"served_model,omitempty"`
	PipelineID      string       `json:"pipeline_id,omitempty"`
	EvaluationMode  string       `json:"evaluation_mode,omitempty"`
	BudgetAccount   string       `json:"budget_account,omitempty"`
}

// Caller is the event's caller object.
type Caller struct {
	Kind             string `json:"kind"`
	TokenJti         string `json:"token_jti,omitempty"`
	ServicePrincipal string `json:"service_principal,omitempty"`
	GrantJti         string `json:"grant_jti,omitempty"`
}

// Tokens is the event usage object.
type Tokens struct {
	InputTokens      int64 `json:"input_tokens"`
	OutputTokens     int64 `json:"output_tokens"`
	CacheReadTokens  int64 `json:"cache_read_tokens,omitempty"`
	CacheWriteTokens int64 `json:"cache_write_tokens,omitempty"`
	ReasoningTokens  int64 `json:"reasoning_tokens,omitempty"`
}

// Cost is the event cost object.
type Cost struct {
	MicrosUSD        int64  `json:"micros_usd"`
	Source           string `json:"source"`
	PriceBookVersion int    `json:"price_book_version,omitempty"`
}

// BudgetAfter is the event budget_after object.
type BudgetAfter struct {
	RunSpentMicros          *int64 `json:"run_spent_micros,omitempty"`
	JobSpentMicros          *int64 `json:"job_spent_micros,omitempty"`
	TenantPeriodSpentMicros *int64 `json:"tenant_period_spent_micros,omitempty"`
	AccountSpentMicros      *int64 `json:"account_spent_micros,omitempty"`
}

// Sink receives finished segment bytes under a key.
type Sink interface {
	Put(ctx context.Context, key string, body []byte) error
	Describe() string
}

// FileSink writes segments below a directory (local mode without S3).
type FileSink struct{ Dir string }

// Put writes the object write-once.
func (f FileSink) Put(_ context.Context, key string, body []byte) error {
	p := filepath.Join(f.Dir, filepath.FromSlash(key))
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		return err
	}
	fh, err := os.OpenFile(p, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return err
	}
	if _, err := fh.Write(body); err != nil {
		fh.Close()
		return err
	}
	return fh.Close()
}

// Describe names the sink.
func (f FileSink) Describe() string { return "file://" + f.Dir }

// Log buffers events into segments and writes them before considering them delivered.
type Log struct {
	sink     Sink
	prefix   string // key prefix, e.g. platform/gateway-usage/
	pod      string
	interval time.Duration
	maxBuf   int
	log      *slog.Logger

	mu        sync.Mutex
	buf       []Event
	seq       int64
	failSince time.Time
	written   int64

	fast *poster
}

// Options configure a Log.
type Options struct {
	Sink            Sink
	Prefix          string
	Pod             string
	Interval        time.Duration
	IngestURL       string
	IngestTokenFile string
	Logger          *slog.Logger
}

// NewLog builds the usage log.
func NewLog(o Options) *Log {
	if o.Interval == 0 {
		o.Interval = 10 * time.Second
	}
	l := &Log{sink: o.Sink, prefix: strings.TrimLeft(o.Prefix, "/"), pod: sanitize(o.Pod), interval: o.Interval, maxBuf: 64 * 1024, log: o.Logger}
	if l.prefix != "" && !strings.HasSuffix(l.prefix, "/") {
		l.prefix += "/"
	}
	l.seq = time.Now().Unix() * 1000 % 1e10 // keeps keys unique across restarts of the same pod name
	if o.IngestURL != "" {
		l.fast = &poster{url: o.IngestURL, tokenFile: o.IngestTokenFile, client: &http.Client{Timeout: 5 * time.Second}, log: o.Logger}
	}
	return l
}

func sanitize(s string) string {
	return strings.Map(func(r rune) rune {
		if r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-' || r == '_' || r == '.' {
			return r
		}
		return '-'
	}, s)
}

// ErrBackpressure is returned when too many events are unwritten (fail closed).
var ErrBackpressure = errors.New("usage log backlog full")

// Append queues an event. It returns ErrBackpressure when the backlog is full.
func (l *Log) Append(e Event) error {
	l.mu.Lock()
	if len(l.buf) >= l.maxBuf {
		l.mu.Unlock()
		return ErrBackpressure
	}
	l.buf = append(l.buf, e)
	n := len(l.buf)
	l.mu.Unlock()
	if l.fast != nil {
		l.fast.enqueue(e)
	}
	if n >= 1000 {
		go l.Flush(context.Background())
	}
	return nil
}

// Healthy is false when segment writes have failed for more than 60 s (gateway.md §9.1: fail closed).
func (l *Log) Healthy() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.failSince.IsZero() || time.Since(l.failSince) < 60*time.Second
}

// Written returns the number of events durably written.
func (l *Log) Written() int64 {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.written
}

// Flush writes the current buffer as one segment.
func (l *Log) Flush(ctx context.Context) error {
	l.mu.Lock()
	if len(l.buf) == 0 {
		l.mu.Unlock()
		return nil
	}
	n := len(l.buf)
	if n > 1000 {
		n = 1000
	}
	batch := append([]Event(nil), l.buf[:n]...)
	l.seq++
	seq := l.seq
	l.mu.Unlock()

	var raw bytes.Buffer
	enc := json.NewEncoder(&raw)
	for _, e := range batch {
		_ = enc.Encode(e)
	}
	zw, _ := zstd.NewWriter(nil)
	body := zw.EncodeAll(raw.Bytes(), nil)
	_ = zw.Close()
	t, _ := time.Parse(time.RFC3339Nano, batch[0].OccurredAt)
	if t.IsZero() {
		t = time.Now().UTC()
	}
	key := fmt.Sprintf("%s%04d/%02d/%02d/%02d/%s-%010d.ndjson.zst", l.prefix, t.Year(), t.Month(), t.Day(), t.Hour(), l.pod, seq)
	err := l.sink.Put(ctx, key, body)
	l.mu.Lock()
	defer l.mu.Unlock()
	if err != nil {
		if l.failSince.IsZero() {
			l.failSince = time.Now()
		}
		if l.log != nil {
			l.log.Warn("usage log segment write failed", "key", key, "err", err.Error())
		}
		return err
	}
	l.failSince = time.Time{}
	l.buf = l.buf[n:]
	l.written += int64(n)
	if l.log != nil {
		l.log.Info("usage log segment written", "sink", l.sink.Describe(), "key", key, "events", n)
	}
	return nil
}

// Run flushes every interval until ctx ends, then flushes the remainder.
func (l *Log) Run(ctx context.Context) {
	if l.fast != nil {
		go l.fast.run(ctx)
	}
	t := time.NewTicker(l.interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			fctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			for i := 0; i < 70; i++ {
				if l.Flush(fctx) != nil {
					break
				}
				l.mu.Lock()
				empty := len(l.buf) == 0
				l.mu.Unlock()
				if empty {
					break
				}
			}
			cancel()
			return
		case <-t.C:
			_ = l.Flush(ctx)
		}
	}
}

// poster is the usage-ingestor fast path (POST batches of ≤ 500 every second, idempotent by event_id).
type poster struct {
	url       string
	tokenFile string
	client    *http.Client
	log       *slog.Logger
	mu        sync.Mutex
	q         []Event
	fails     int
}

func (p *poster) enqueue(e Event) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if len(p.q) >= 10000 { // the durable log is authoritative; the fast path drops oldest
		p.q = p.q[1:]
	}
	p.q = append(p.q, e)
}

func (p *poster) run(ctx context.Context) {
	t := time.NewTicker(time.Second)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			p.flush(ctx)
		}
	}
}

func (p *poster) flush(ctx context.Context) {
	p.mu.Lock()
	n := len(p.q)
	if n == 0 {
		p.mu.Unlock()
		return
	}
	if n > 500 {
		n = 500
	}
	batch := append([]Event(nil), p.q[:n]...)
	p.mu.Unlock()
	b, _ := json.Marshal(map[string]any{"events": batch})
	req, _ := http.NewRequestWithContext(ctx, "POST", p.url, bytes.NewReader(b))
	req.Header.Set("Content-Type", "application/json")
	if p.tokenFile != "" {
		if t, err := os.ReadFile(p.tokenFile); err == nil {
			req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(string(t)))
		}
	}
	resp, err := p.client.Do(req)
	if err == nil {
		resp.Body.Close()
		if resp.StatusCode/100 != 2 {
			err = fmt.Errorf("http %d", resp.StatusCode)
		}
	}
	if err != nil {
		p.fails++
		if p.log != nil && (p.fails == 1 || p.fails%60 == 0) {
			p.log.Debug("usage fast path unavailable (events stay in the durable log)", "err", err.Error())
		}
		return
	}
	p.fails = 0
	p.mu.Lock()
	if len(p.q) >= n {
		p.q = p.q[n:]
	}
	p.mu.Unlock()
}
