package main

// Minimal OTLP/HTTP JSON trace exporter (WS-08 M2; no SDK dependency). Spans are batched and POSTed to
// OTEL_EXPORTER_OTLP_TRACES_ENDPOINT (default http://alloy:4318/v1/traces). Disabled unless
// OTEL_SDK_DISABLED is set to something other than "true". IDs follow C-IDS T-1 / T-5: a job's spans use
// trace_id = sha256("replicant.job.trace.v1:"+job_id)[:32] and parent under the root span
// "job" (sha256("replicant.job.span.v1:"+job_id)[:16]) unless the caller passes a parent.
// Keep this file identical (except the package line) in go/cmd/runner-local, go/cmd/replicant-gateway-shim/internal/shim,
// go/cmd/replicant-agentd and go/cmd/replicant-entrypoint. Enabled when OTEL_SDK_DISABLED is not "true" and either it is
// set to another value or OTEL_EXPORTER_OTLP_(TRACES_)ENDPOINT is set (sandboxes get OTEL_EXPORTER_OTLP_ENDPOINT =
// http://otlp.sbx.local, sbx-edge -> Alloy).

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

type otelSpan struct {
	TraceID, SpanID, ParentID, Name string
	Kind                            int // 1 internal, 2 server, 3 client
	Start, End                      time.Time
	Attrs                           map[string]string
	Error                           bool
}

type otelExporter struct {
	url, service string
	ch           chan otelSpan
	client       *http.Client
}

var (
	otelOnce sync.Once
	otelExp  *otelExporter
)

// otel returns the process exporter, or nil when tracing is disabled.
func otel(service string) *otelExporter {
	otelOnce.Do(func() {
		dis := strings.ToLower(os.Getenv("OTEL_SDK_DISABLED"))
		url := os.Getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
		if url == "" && os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") != "" {
			url = strings.TrimRight(os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT"), "/") + "/v1/traces"
		}
		if dis == "true" || (dis == "" && url == "") {
			return
		}
		if url == "" {
			url = "http://alloy:4318/v1/traces"
		}
		if s := os.Getenv("OTEL_SERVICE_NAME"); s != "" {
			service = s
		}
		otelExp = &otelExporter{url: url, service: service, ch: make(chan otelSpan, 4096), client: &http.Client{Timeout: 5 * time.Second}}
		go otelExp.loop()
	})
	return otelExp
}

func otelHex(seed string, n int) string {
	h := sha256.Sum256([]byte(seed))
	return hex.EncodeToString(h[:])[:n]
}

// otelJobTraceID and otelJobRootSpanID implement C-IDS rules T-1 and T-5a; otelRunSpanID T-5b.
func otelJobTraceID(jobID string) string    { return otelHex("replicant.job.trace.v1:"+jobID, 32) }
func otelJobRootSpanID(jobID string) string { return otelHex("replicant.job.span.v1:"+jobID, 16) }
func otelRunSpanID(runID string) string     { return otelHex("replicant.run.span.v1:"+runID, 16) }

func otelNewSpanID() string {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// emitJobSpan records a finished span in the job's trace. parent "" = the job root span.
func (e *otelExporter) emitJobSpan(jobID, parent, name string, kind int, start, end time.Time, attrs map[string]string, isErr bool) {
	if e == nil || !strings.HasPrefix(jobID, "job_") {
		return
	}
	if parent == "" {
		parent = otelJobRootSpanID(jobID)
	}
	if attrs == nil {
		attrs = map[string]string{}
	}
	attrs["replicant.job_id"] = jobID
	e.emit(otelSpan{TraceID: otelJobTraceID(jobID), SpanID: otelNewSpanID(), ParentID: parent, Name: name, Kind: kind,
		Start: start, End: end, Attrs: attrs, Error: isErr})
}

// emitSync posts one span immediately (short-lived processes that exit right after).
func (e *otelExporter) emitSync(s otelSpan) {
	if e == nil {
		return
	}
	e.post([]otelSpan{s})
}

func (e *otelExporter) emit(s otelSpan) {
	if e == nil {
		return
	}
	select {
	case e.ch <- s:
	default: // never block the request path; drop on overflow
	}
}

func (e *otelExporter) loop() {
	t := time.NewTicker(time.Second)
	defer t.Stop()
	var batch []otelSpan
	for {
		select {
		case s := <-e.ch:
			batch = append(batch, s)
			if len(batch) < 256 {
				continue
			}
		case <-t.C:
		}
		if len(batch) > 0 {
			e.post(batch)
			batch = nil
		}
	}
}

type otelKV struct {
	Key   string            `json:"key"`
	Value map[string]string `json:"value"`
}

func otelAttrs(m map[string]string) []otelKV {
	out := make([]otelKV, 0, len(m))
	for k, v := range m {
		if v != "" {
			out = append(out, otelKV{Key: k, Value: map[string]string{"stringValue": v}})
		}
	}
	return out
}

func (e *otelExporter) post(batch []otelSpan) {
	spans := make([]map[string]any, 0, len(batch))
	for _, s := range batch {
		code := 1
		if s.Error {
			code = 2
		}
		spans = append(spans, map[string]any{
			"traceId": s.TraceID, "spanId": s.SpanID, "parentSpanId": s.ParentID, "name": s.Name, "kind": s.Kind,
			"startTimeUnixNano": strconv.FormatInt(s.Start.UnixNano(), 10),
			"endTimeUnixNano":   strconv.FormatInt(s.End.UnixNano(), 10),
			"attributes":        otelAttrs(s.Attrs), "status": map[string]int{"code": code},
		})
	}
	env := os.Getenv("REPLICANT_ENV")
	body, _ := json.Marshal(map[string]any{"resourceSpans": []any{map[string]any{
		"resource":   map[string]any{"attributes": otelAttrs(map[string]string{"service.name": e.service, "replicant.env": env})},
		"scopeSpans": []any{map[string]any{"scope": map[string]string{"name": "replicant"}, "spans": spans}},
	}}})
	req, err := http.NewRequest(http.MethodPost, e.url, bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	if resp, err := e.client.Do(req); err == nil {
		resp.Body.Close()
	}
}
