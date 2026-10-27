package shim

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"strings"
	"time"
)

// PassthroughBackend forwards to the provider's public API with a developer key (`--upstream-backend=passthrough`;
// gateway.md §8). The key comes from ANTHROPIC_API_KEY / OPENAI_API_KEY / VOYAGE_API_KEY, or from the file named by
// <NAME>_FILE (a compose secret). It is never logged.
type PassthroughBackend struct {
	cfg    *Config
	client *http.Client
	getenv func(string) string
}

// NewPassthroughBackend builds the backend; HTTPS_PROXY/NO_PROXY are honored.
func NewPassthroughBackend(cfg *Config) *PassthroughBackend {
	tr := &http.Transport{
		Proxy:                 http.ProxyFromEnvironment,
		DialContext:           (&net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		TLSHandshakeTimeout:   10 * time.Second,
		ResponseHeaderTimeout: 10 * time.Minute,
		MaxIdleConnsPerHost:   32,
		ForceAttemptHTTP2:     true,
	}
	return &PassthroughBackend{cfg: cfg, client: &http.Client{Transport: tr}, getenv: os.Getenv}
}

// Name is the backend name.
func (b *PassthroughBackend) Name() string { return "passthrough" }

func (b *PassthroughBackend) key(name string) string {
	if v := strings.TrimSpace(b.getenv(name)); v != "" {
		return v
	}
	if f := b.getenv(name + "_FILE"); f != "" {
		if raw, err := os.ReadFile(f); err == nil {
			return strings.TrimSpace(string(raw))
		}
	}
	return ""
}

// upstreamFor returns the base URL and key variable for a request.
func (b *PassthroughBackend) upstreamFor(d *DataRequest) (string, string) {
	base := d.Route.Upstream
	keyVar := "ANTHROPIC_API_KEY"
	switch d.Protocol {
	case "openai_responses":
		keyVar = "OPENAI_API_KEY"
	case "embeddings":
		provider, _, _ := strings.Cut(d.Model, "/")
		if u, ok := d.Route.UpstreamByModel[provider]; ok {
			base = u
		}
		keyVar = "OPENAI_API_KEY"
		if provider == "voyage" {
			keyVar = "VOYAGE_API_KEY"
		}
	}
	return strings.TrimRight(base, "/"), keyVar
}

// rewriteModel replaces the gateway model id with the upstream name; every other field is passed as is.
func rewriteModel(body []byte, upstreamModel string) ([]byte, error) {
	var m map[string]json.RawMessage
	if err := json.Unmarshal(body, &m); err != nil {
		return nil, err
	}
	mb, _ := json.Marshal(upstreamModel)
	m["model"] = mb
	return json.Marshal(m)
}

var forwardHeaders = []string{"anthropic-version", "anthropic-beta", "content-type", "accept", "traceparent", "tracestate", "user-agent", "x-stainless-retry-count", "openai-beta"}

// Serve proxies one request, streaming the response without buffering and teeing SSE frames to the usage parser.
func (b *PassthroughBackend) Serve(w http.ResponseWriter, r *http.Request, d *DataRequest) Result {
	base, keyVar := b.upstreamFor(d)
	key := b.key(keyVar)
	if key == "" {
		writeError(w, d.Protocol, 503, "upstream_unavailable", "passthrough backend has no "+keyVar+" (dev key missing; use --gateway replay or script)", nil)
		return Result{Status: 503, Faulted: true}
	}
	body := d.Body
	if d.Model != "" {
		nb, err := rewriteModel(body, b.cfg.UpstreamModel(d.Model))
		if err != nil {
			writeError(w, d.Protocol, 400, "upstream_unavailable", "request body is not a JSON object", nil)
			return Result{Status: 400, Faulted: true}
		}
		body = nb
	}
	upURL := base + d.UpstreamPath
	if d.Protocol == "embeddings" {
		upURL = base + "/v1/embeddings"
	}
	req, err := http.NewRequestWithContext(r.Context(), "POST", upURL, bytes.NewReader(body))
	if err != nil {
		writeError(w, d.Protocol, 502, "upstream_unavailable", "bad upstream url", nil)
		return Result{Status: 502, Faulted: true}
	}
	for _, h := range forwardHeaders {
		if v := r.Header.Get(h); v != "" {
			req.Header.Set(h, v)
		}
	}
	if req.Header.Get("content-type") == "" {
		req.Header.Set("content-type", "application/json")
	}
	if strings.HasPrefix(d.Protocol, "anthropic") {
		req.Header.Set("x-api-key", key)
		if req.Header.Get("anthropic-version") == "" {
			req.Header.Set("anthropic-version", "2023-06-01")
		}
	} else {
		req.Header.Set("Authorization", "Bearer "+key)
	}
	req.Header.Set("X-Replicant-Request-Id", d.RequestID)
	start := time.Now()
	resp, err := b.client.Do(req)
	if err != nil {
		var ne net.Error
		if errors.As(err, &ne) && ne.Timeout() {
			writeError(w, d.Protocol, 504, "upstream_timeout", "upstream timed out", nil)
			return Result{Status: 504, Faulted: true}
		}
		if r.Context().Err() != nil {
			return Result{Status: 499, ClientAborted: true}
		}
		writeError(w, d.Protocol, 502, "upstream_unavailable", "upstream unreachable", nil)
		return Result{Status: 502, Faulted: true}
	}
	defer resp.Body.Close()
	res := Result{Status: resp.StatusCode, TTFB: time.Since(start)}
	res.UpstreamRequestID = firstNonEmpty(resp.Header.Get("request-id"), resp.Header.Get("x-request-id"))
	for k, vs := range resp.Header {
		lk := strings.ToLower(k)
		if lk == "content-length" || lk == "connection" || lk == "transfer-encoding" || lk == "keep-alive" || strings.HasPrefix(lk, "x-replicant-") {
			continue
		}
		for _, v := range vs {
			w.Header().Add(k, v)
		}
	}
	isSSE := strings.HasPrefix(resp.Header.Get("Content-Type"), "text/event-stream")
	if isSSE {
		w.Header().Set("Trailer", "X-Replicant-Served-Model")
		w.WriteHeader(resp.StatusCode)
		p := &sseUsage{protocol: d.Protocol}
		fl, _ := w.(http.Flusher)
		br := bufio.NewReaderSize(resp.Body, 64*1024)
		for {
			line, err := br.ReadBytes('\n')
			if len(line) > 0 {
				p.line(line)
				if _, werr := w.Write(line); werr != nil {
					res.ClientAborted = true
					break
				}
				if fl != nil && (len(bytes.TrimSpace(line)) == 0) {
					fl.Flush()
				}
			}
			if err != nil {
				if err != io.EOF && r.Context().Err() != nil {
					res.ClientAborted = true
				}
				break
			}
		}
		if fl != nil {
			fl.Flush()
		}
		res.Usage, res.UsageParsed, res.ServedModel = p.u, p.complete(), p.model
		if res.ServedModel != "" {
			w.Header().Set("X-Replicant-Served-Model", res.ServedModel)
		}
		return res
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 32<<20))
	if err != nil && r.Context().Err() != nil {
		res.ClientAborted = true
	}
	res.Usage, res.UsageParsed, res.ServedModel = parseBodyUsage(d.Protocol, raw)
	if res.ServedModel != "" {
		w.Header().Set("X-Replicant-Served-Model", res.ServedModel)
	}
	w.WriteHeader(resp.StatusCode)
	_, _ = w.Write(raw)
	return res
}

func firstNonEmpty(xs ...string) string {
	for _, x := range xs {
		if x != "" {
			return x
		}
	}
	return ""
}

type anthropicUsage struct {
	InputTokens              int64 `json:"input_tokens"`
	OutputTokens             int64 `json:"output_tokens"`
	CacheReadInputTokens     int64 `json:"cache_read_input_tokens"`
	CacheCreationInputTokens int64 `json:"cache_creation_input_tokens"`
}

type openaiUsage struct {
	InputTokens        int64 `json:"input_tokens"`
	OutputTokens       int64 `json:"output_tokens"`
	TotalTokens        int64 `json:"total_tokens"`
	InputTokensDetails struct {
		CachedTokens int64 `json:"cached_tokens"`
	} `json:"input_tokens_details"`
	OutputTokensDetails struct {
		ReasoningTokens int64 `json:"reasoning_tokens"`
	} `json:"output_tokens_details"`
	PromptTokens int64 `json:"prompt_tokens"` // embeddings
}

func (u openaiUsage) toUsage() Usage {
	in := u.InputTokens
	if in == 0 && u.PromptTokens > 0 {
		in = u.PromptTokens
	}
	if in == 0 && u.OutputTokens == 0 && u.TotalTokens > 0 {
		in = u.TotalTokens
	}
	cached := u.InputTokensDetails.CachedTokens
	return Usage{InputTokens: in - cached, OutputTokens: u.OutputTokens, CacheReadTokens: cached, ReasoningTokens: u.OutputTokensDetails.ReasoningTokens}
}

// parseBodyUsage reads usage and model from a non-streaming body.
func parseBodyUsage(protocol string, raw []byte) (Usage, bool, string) {
	var env struct {
		Model string          `json:"model"`
		Usage json.RawMessage `json:"usage"`
	}
	if json.Unmarshal(raw, &env) != nil || len(env.Usage) == 0 {
		return Usage{}, false, env.Model
	}
	if strings.HasPrefix(protocol, "anthropic") {
		var u anthropicUsage
		if json.Unmarshal(env.Usage, &u) != nil {
			return Usage{}, false, env.Model
		}
		return Usage{InputTokens: u.InputTokens, OutputTokens: u.OutputTokens, CacheReadTokens: u.CacheReadInputTokens, CacheWriteTokens: u.CacheCreationInputTokens}, true, env.Model
	}
	var u openaiUsage
	if json.Unmarshal(env.Usage, &u) != nil {
		return Usage{}, false, env.Model
	}
	return u.toUsage(), true, env.Model
}

// sseUsage parses usage from SSE `data:` lines (Anthropic message_start/message_delta; OpenAI response.completed).
type sseUsage struct {
	protocol string
	u        Usage
	model    string
	sawStart bool
	sawFinal bool
}

func (p *sseUsage) complete() bool { return p.sawFinal }

func (p *sseUsage) line(l []byte) {
	d, ok := bytes.CutPrefix(bytes.TrimRight(l, "\r\n"), []byte("data:"))
	if !ok {
		return
	}
	d = bytes.TrimSpace(d)
	var ev struct {
		Type    string `json:"type"`
		Message struct {
			Model string         `json:"model"`
			Usage anthropicUsage `json:"usage"`
		} `json:"message"`
		Usage    *anthropicUsage `json:"usage"`
		Response struct {
			Model string      `json:"model"`
			Usage openaiUsage `json:"usage"`
		} `json:"response"`
	}
	if json.Unmarshal(d, &ev) != nil {
		return
	}
	switch ev.Type {
	case "message_start":
		p.sawStart = true
		p.model = ev.Message.Model
		mu := ev.Message.Usage
		p.u.InputTokens, p.u.CacheReadTokens, p.u.CacheWriteTokens = mu.InputTokens, mu.CacheReadInputTokens, mu.CacheCreationInputTokens
		p.u.OutputTokens = mu.OutputTokens
	case "message_delta":
		if ev.Usage != nil {
			// message_delta usage is cumulative
			if ev.Usage.OutputTokens > 0 {
				p.u.OutputTokens = ev.Usage.OutputTokens
			}
			if ev.Usage.InputTokens > 0 {
				p.u.InputTokens = ev.Usage.InputTokens
			}
			if ev.Usage.CacheReadInputTokens > 0 {
				p.u.CacheReadTokens = ev.Usage.CacheReadInputTokens
			}
			if ev.Usage.CacheCreationInputTokens > 0 {
				p.u.CacheWriteTokens = ev.Usage.CacheCreationInputTokens
			}
			p.sawFinal = p.sawStart
		}
	case "response.created":
		p.model = ev.Response.Model
	case "response.completed":
		p.model = firstNonEmpty(ev.Response.Model, p.model)
		p.u = ev.Response.Usage.toUsage()
		p.sawFinal = true
	}
}
