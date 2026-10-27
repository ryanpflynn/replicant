package shim

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// DataRequest is a data-plane request after authentication, policy and admission.
type DataRequest struct {
	Protocol     string // anthropic_messages | anthropic_count_tokens | openai_responses | embeddings
	Route        Route
	UpstreamPath string // path after the route prefix, with query, e.g. /v1/messages?beta=true
	Body         []byte
	Model        string
	Stream       bool
	MaxTokens    int
	RunKey       string // scripted-state key (run_id, or service principal + job)
	Role         string
	Policy       *Policy
	RequestID    string
	Header       http.Header
}

// Result is what a backend reports for settlement and the usage event.
type Result struct {
	Status            int
	Usage             Usage
	UsageParsed       bool
	ServedModel       string
	UpstreamRequestID string
	TTFB              time.Duration
	ClientAborted     bool
	Faulted           bool // a scripted or injected fault (never charged)
}

// Backend serves data-plane requests (`--upstream-backend`).
type Backend interface {
	Serve(w http.ResponseWriter, r *http.Request, d *DataRequest) Result
	Name() string
}

// ReplicantError is the gateway-interface ReplicantError body, in the route's protocol envelope.
type ReplicantError struct {
	Type          string          `json:"type,omitempty"`
	Error         *ErrorObj       `json:"error,omitempty"`
	ReplicantCode string          `json:"replicant_code"`
	Message       string          `json:"message"`
	Budget        *BudgetState    `json:"budget,omitempty"`
	Detail        string          `json:"detail,omitempty"`
	Keys          json.RawMessage `json:"keys,omitempty"`
}

// ErrorObj is the provider error object.
type ErrorObj struct {
	Type    string `json:"type"`
	Message string `json:"message"`
	Code    string `json:"code,omitempty"`
}

func anthropicErrorType(status int) string {
	switch status {
	case 400:
		return "invalid_request_error"
	case 401:
		return "authentication_error"
	case 402:
		return "billing_error"
	case 403:
		return "permission_error"
	case 404:
		return "not_found_error"
	case 409:
		return "invalid_request_error"
	case 429:
		return "rate_limit_error"
	case 529:
		return "overloaded_error"
	default:
		return "api_error"
	}
}

// writeError writes a replicant error in the envelope of protocol ("anthropic", "openai" or "").
func writeError(w http.ResponseWriter, protocol string, status int, code, msg string, extra func(*ReplicantError)) {
	if len(msg) > 1000 {
		msg = msg[:1000]
	}
	fe := &ReplicantError{ReplicantCode: code, Message: msg}
	switch {
	case strings.HasPrefix(protocol, "anthropic"):
		fe.Type = "error"
		fe.Error = &ErrorObj{Type: anthropicErrorType(status), Message: "replicant: " + msg}
	case strings.HasPrefix(protocol, "openai"), protocol == "embeddings":
		fe.Error = &ErrorObj{Type: anthropicErrorType(status), Message: "replicant: " + msg, Code: code}
	}
	if extra != nil {
		extra(fe)
	}
	b, _ := json.Marshal(fe)
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", strconv.Itoa(len(b)))
	w.WriteHeader(status)
	_, _ = w.Write(b)
}

// Fault is a scripted or injected failure.
type Fault struct {
	Status        int    `yaml:"status" json:"status"`
	RetryAfter    int    `yaml:"retry_after" json:"retry_after,omitempty"`
	ReplicantCode string `yaml:"replicant_code" json:"replicant_code,omitempty"`
	BudgetLevel   string `yaml:"budget_level" json:"budget_level,omitempty"`
	Message       string `yaml:"message" json:"message,omitempty"`
}

// ParseFaultHeader parses `X-Replicant-Stub-Fault: <status>[;retry_after=N][;budget_level=L][;code=C]`.
func ParseFaultHeader(v string) (*Fault, error) {
	parts := strings.Split(v, ";")
	st, err := strconv.Atoi(strings.TrimSpace(parts[0]))
	if err != nil || st < 400 || st > 599 {
		return nil, fmt.Errorf("bad X-Replicant-Stub-Fault %q", v)
	}
	f := &Fault{Status: st}
	for _, p := range parts[1:] {
		k, val, _ := strings.Cut(strings.TrimSpace(p), "=")
		switch k {
		case "retry_after":
			f.RetryAfter, _ = strconv.Atoi(val)
		case "budget_level":
			f.BudgetLevel = val
		case "code":
			f.ReplicantCode = val
		}
	}
	return f, nil
}

// writeFault renders a fault like the provider (429/5xx) or the shim (402) would.
func writeFault(w http.ResponseWriter, protocol string, f *Fault) {
	if f.RetryAfter > 0 {
		w.Header().Set("retry-after", strconv.Itoa(f.RetryAfter))
	}
	msg := f.Message
	switch {
	case f.Status == 402:
		lvl := f.BudgetLevel
		if lvl == "" {
			lvl = "run"
		}
		code := f.ReplicantCode
		if code == "" {
			code = "budget_exceeded"
		}
		if msg == "" {
			msg = lvl + " budget exhausted (injected fault)"
		}
		w.Header().Set("X-Replicant-Budget-Exceeded", lvl)
		writeError(w, protocol, 402, code, msg, nil)
		return
	case f.ReplicantCode != "":
		if msg == "" {
			msg = "injected fault"
		}
		writeError(w, protocol, f.Status, f.ReplicantCode, msg, nil)
		return
	}
	// provider-shaped error (passed through by a real shim without replicant_code)
	if msg == "" {
		msg = fmt.Sprintf("injected upstream fault %d", f.Status)
	}
	body := map[string]any{"type": "error", "error": map[string]any{"type": anthropicErrorType(f.Status), "message": msg}}
	if strings.HasPrefix(protocol, "openai") {
		body = map[string]any{"error": map[string]any{"type": anthropicErrorType(f.Status), "message": msg}}
	}
	b, _ := json.Marshal(body)
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(f.Status)
	_, _ = w.Write(b)
}

// usd6 formats micro-USD as a decimal string with 6 fractional digits.
func usd6(micros int64) string {
	if micros < 0 {
		micros = 0
	}
	return fmt.Sprintf("%d.%06d", micros/1_000_000, micros%1_000_000)
}
