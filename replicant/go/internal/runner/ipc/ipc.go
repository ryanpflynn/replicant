// Package ipc defines replicant-agentd's IPC API (cli-invocation.md §5.3) and a client for it.
package ipc

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"time"
)

// Event kinds raised by agentd.
const (
	EvBudgetSoftLimit    = "budget_soft_limit"
	EvBudgetExceeded     = "budget_exceeded"
	EvGatewayUnavailable = "gateway_unavailable"
	EvGatewayRecovered   = "gateway_recovered"
	EvSteeringAvailable  = "steering_available"
	EvTokenRefreshFailed = "token_refresh_failed"
	EvCancelRequested    = "cancel_requested"
	EvHeartbeatLost      = "heartbeat_lost"
)

// Event is one IPC event.
type Event struct {
	Seq    int       `json:"seq"`
	Kind   string    `json:"kind"`
	Reason string    `json:"reason,omitempty"`
	Detail string    `json:"detail,omitempty"`
	At     time.Time `json:"at"`
}

// Events is the long-poll response.
type Events struct {
	Events []Event `json:"events"`
}

// Usage are agentd's trusted counters.
type Usage struct {
	ModelCalls          int     `json:"model_calls"`
	InputTokens         int64   `json:"input_tokens"`
	OutputTokens        int64   `json:"output_tokens"`
	CacheReadTokens     int64   `json:"cache_read_tokens"`
	CacheWriteTokens    int64   `json:"cache_write_tokens"`
	CostUSDReported     float64 `json:"cost_usd_reported"`
	BudgetLimitUSD      float64 `json:"budget_limit_usd"`
	RateLimitedSeconds  float64 `json:"rate_limited_seconds"`
	SoftLimitReached    bool    `json:"soft_limit_reached"`
	HardLimitReached    bool    `json:"hard_limit_reached"`
	BudgetExceededScope string  `json:"budget_exceeded_scope,omitempty"`
	JobRemainingUSD     float64 `json:"job_remaining_usd,omitempty"`
}

// SteeringMessage is a queued steering message.
type SteeringMessage struct {
	Seq       int       `json:"seq"`
	MessageID string    `json:"message_id"`
	Text      string    `json:"text"`
	Author    string    `json:"author"`
	SentAt    time.Time `json:"sent_at"`
}

// Consumed lists delivered steering message ids.
type Consumed struct {
	MessageIDs []string `json:"message_ids"`
}

// HeartbeatNow is the response of POST /v1/heartbeat:now.
type HeartbeatNow struct {
	CancelReason *string `json:"cancel_reason"`
}

// AWSCredentialProcess is the credential_process JSON served on uploader-ipc.
type AWSCredentialProcess struct {
	Version         int       `json:"Version"`
	AccessKeyID     string    `json:"AccessKeyId"`
	SecretAccessKey string    `json:"SecretAccessKey"`
	SessionToken    string    `json:"SessionToken"`
	Expiration      time.Time `json:"Expiration"`
}

// Client is an HTTP client over a unix socket.
type Client struct {
	hc *http.Client
}

// New returns a client for socket path.
func New(socket string) *Client {
	tr := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		var d net.Dialer
		return d.DialContext(ctx, "unix", socket)
	}}
	return &Client{hc: &http.Client{Transport: tr}}
}

// Do sends a JSON request; out may be nil.
func (c *Client) Do(ctx context.Context, method, path string, q url.Values, in, out any) error {
	var body io.Reader
	if in != nil {
		b, _ := json.Marshal(in)
		body = bytes.NewReader(b)
	}
	u := "http://agentd" + path
	if len(q) > 0 {
		u += "?" + q.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, method, u, body)
	if err != nil {
		return err
	}
	if in != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.hc.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("ipc %s %s: HTTP %d %s", method, path, resp.StatusCode, bytes.TrimSpace(b))
	}
	if out != nil {
		return json.NewDecoder(resp.Body).Decode(out)
	}
	return nil
}

// Status posts a phase report.
func (c *Client) Status(ctx context.Context, phase string) error {
	return c.Do(ctx, http.MethodPost, "/v1/status/"+phase, nil, map[string]any{}, nil)
}

// WaitReady polls GET /readyz until it answers 200 or ctx ends.
func (c *Client) WaitReady(ctx context.Context) error {
	for {
		if err := c.Do(ctx, http.MethodGet, "/readyz", nil, nil, nil); err == nil {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(250 * time.Millisecond):
		}
	}
}
