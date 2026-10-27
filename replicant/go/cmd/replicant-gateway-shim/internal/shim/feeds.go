package shim

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const revocationTTL = 2 * time.Hour

// Feeds polls the core grant revocation list and the exec revocation feeds (gateway.md §5.3).
type Feeds struct {
	cfg    *Config
	st     *MemState
	client *http.Client
	log    *slog.Logger

	etag        string
	execCursors map[string]string
	Failures    atomic.Int64 // replicant_gateway_state_errors_total{reason="revocation_feed"}
}

// NewFeeds builds the pollers.
func NewFeeds(cfg *Config, st *MemState, log *slog.Logger) *Feeds {
	return &Feeds{cfg: cfg, st: st, client: &http.Client{Timeout: 5 * time.Second}, log: log, execCursors: map[string]string{}}
}

// Run polls until ctx ends.
func (f *Feeds) Run(ctx context.Context) {
	if f.cfg.GrantRevocationURL != "" {
		go f.loop(ctx, f.cfg.GrantRevocationPoll, f.pollGrants)
	}
	if len(f.cfg.ExecRevocationURLs) > 0 {
		go f.loop(ctx, f.cfg.ExecRevocationPoll, f.pollExec)
	}
}

func (f *Feeds) loop(ctx context.Context, every time.Duration, fn func(context.Context) error) {
	fails := 0
	for {
		if err := fn(ctx); err != nil {
			fails++
			f.Failures.Add(1)
			lvl := slog.LevelDebug
			if fails > 3 {
				lvl = slog.LevelWarn // ReplicantGatewayRevocationFeedStale
			}
			f.log.Log(ctx, lvl, "revocation feed poll failed", "err", err.Error(), "consecutive", fails)
		} else {
			fails = 0
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(every):
		}
	}
}

type grantRevocations struct {
	JobIDs []struct {
		ID string `json:"id"`
	} `json:"job_ids"`
	RunIDs []struct {
		ID string `json:"id"`
	} `json:"run_ids"`
}

// PollGrantsOnce is exported for tests.
func (f *Feeds) PollGrantsOnce(ctx context.Context) error { return f.pollGrants(ctx) }

func (f *Feeds) pollGrants(ctx context.Context) error {
	req, _ := http.NewRequestWithContext(ctx, "GET", f.cfg.GrantRevocationURL, nil)
	if f.etag != "" {
		req.Header.Set("If-None-Match", f.etag)
	}
	resp, err := f.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotModified {
		return nil
	}
	if resp.StatusCode != 200 {
		return fmt.Errorf("grants/revoked: http %d", resp.StatusCode)
	}
	var gr grantRevocations
	if err := json.NewDecoder(io.LimitReader(resp.Body, 16<<20)).Decode(&gr); err != nil {
		return err
	}
	var runs, jobs []string
	for _, r := range gr.RunIDs {
		runs = append(runs, r.ID)
	}
	for _, j := range gr.JobIDs {
		jobs = append(jobs, j.ID)
	}
	f.st.Revoke(runs, jobs, revocationTTL)
	f.etag = resp.Header.Get("ETag")
	return nil
}

type execRevocations struct {
	Revoked []struct {
		RunID string `json:"run_id"`
		JobID string `json:"job_id"`
	} `json:"revoked"`
	NextCursor string `json:"next_cursor"`
}

// PollExecOnce is exported for tests.
func (f *Feeds) PollExecOnce(ctx context.Context) error { return f.pollExec(ctx) }

func (f *Feeds) pollExec(ctx context.Context) error {
	var tok string
	if f.cfg.ExecRevocationTokenFile != "" {
		if b, err := os.ReadFile(f.cfg.ExecRevocationTokenFile); err == nil {
			tok = strings.TrimSpace(string(b))
		}
	}
	var firstErr error
	for _, u := range f.cfg.ExecRevocationURLs {
		full := u
		if c := f.execCursors[u]; c != "" {
			full = u + "?since=" + url.QueryEscape(c)
		}
		req, _ := http.NewRequestWithContext(ctx, "GET", full, nil)
		if tok != "" {
			req.Header.Set("Authorization", "Bearer "+tok)
		}
		resp, err := f.client.Do(req)
		if err != nil {
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		func() {
			defer resp.Body.Close()
			if resp.StatusCode != 200 {
				if firstErr == nil {
					firstErr = fmt.Errorf("%s: http %d", u, resp.StatusCode)
				}
				return
			}
			var er execRevocations
			if err := json.NewDecoder(io.LimitReader(resp.Body, 16<<20)).Decode(&er); err != nil {
				if firstErr == nil {
					firstErr = err
				}
				return
			}
			var runs []string
			for _, r := range er.Revoked {
				runs = append(runs, r.RunID) // keyed by run_id (C-RUNNER verifier obligation 5)
			}
			f.st.Revoke(runs, nil, revocationTTL)
			f.execCursors[u] = er.NextCursor
		}()
	}
	// one unreachable feed (e.g. the kind runner-api in the v0 profile) is not a failure if another answered
	if firstErr != nil && len(f.execCursors) == 0 {
		return firstErr
	}
	return nil
}

// QuotaClient reads GET /internal/v1/tenants/{tenant_id}/gateway-quota, cached (gateway.md §6.1).
type QuotaClient struct {
	cfg    *Config
	client *http.Client
	mu     sync.Mutex
	cache  map[string]quotaEntry
}

type quotaEntry struct {
	q  *TenantQuota // nil = 404 (unset)
	at time.Time
}

// ErrQuotaUnavailable means control-plane-api could not be asked and nothing is cached.
var ErrQuotaUnavailable = fmt.Errorf("tenant quota unavailable")

// NewQuotaClient builds the client.
func NewQuotaClient(cfg *Config) *QuotaClient {
	return &QuotaClient{cfg: cfg, client: &http.Client{Timeout: 3 * time.Second}, cache: map[string]quotaEntry{}}
}

// Get returns the tenant's quota, nil when unset (404).
func (q *QuotaClient) Get(ctx context.Context, tenant string) (*TenantQuota, error) {
	q.mu.Lock()
	e, ok := q.cache[tenant]
	q.mu.Unlock()
	if ok && time.Since(e.at) < q.cfg.QuotaRefresh {
		return e.q, nil
	}
	if q.cfg.ControlPlaneURL == "" {
		if q.cfg.LocalQuota != nil {
			lq := *q.cfg.LocalQuota
			lq.TenantID = tenant
			return &lq, nil
		}
		return nil, nil
	}
	req, _ := http.NewRequestWithContext(ctx, "GET", q.cfg.ControlPlaneURL+"/internal/v1/tenants/"+url.PathEscape(tenant)+"/gateway-quota", nil)
	if q.cfg.ControlPlaneTokenFile != "" {
		if b, err := os.ReadFile(q.cfg.ControlPlaneTokenFile); err == nil {
			req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(string(b)))
		}
	}
	resp, err := q.client.Do(req)
	if err == nil {
		defer resp.Body.Close()
	}
	switch {
	case err == nil && resp.StatusCode == 200:
		var tq TenantQuota
		if derr := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&tq); derr != nil {
			err = derr
			break
		}
		q.store(tenant, &tq)
		return &tq, nil
	case err == nil && resp.StatusCode == 404:
		q.store(tenant, nil)
		return nil, nil
	case err == nil:
		err = fmt.Errorf("gateway-quota: http %d", resp.StatusCode)
	}
	// unreachable or unexpected: keep a cached value; else local fallback if configured
	if ok {
		return e.q, nil
	}
	if q.cfg.LocalQuota != nil {
		lq := *q.cfg.LocalQuota
		lq.TenantID = tenant
		return &lq, nil
	}
	return nil, fmt.Errorf("%w: %v", ErrQuotaUnavailable, err)
}

func (q *QuotaClient) store(t string, tq *TenantQuota) {
	q.mu.Lock()
	q.cache[t] = quotaEntry{q: tq, at: time.Now()}
	q.mu.Unlock()
}
