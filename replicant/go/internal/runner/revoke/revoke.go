// Package revoke polls revocation sources. Since C-RUNNER 4.0.0 (SIMPLIFY-D50) only the Runner polls core's
// revocation list (`GET /internal/v1/revocations` on core, every 60 s: Core) and folds it into its own exec
// revocation feed; every run-token verifier polls that feed (`GET /v1/revocations`, every 10 s: Exec) and
// nothing else.
package revoke

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"example.com/replicant/internal/runner/api"
)

// Checker answers whether a job or run is revoked.
type Checker interface {
	Revoked(jobID, runID string) (bool, string)
}

// Core polls core's revocation list ({job_ids, run_ids}); the Runner is its only reader.
type Core struct {
	URL      string
	Token    string // the Runner's caller token for core (Authorization: Bearer)
	Interval time.Duration
	Client   *http.Client
	// OnUpdate runs after every successful fetch (the Runner copies the list into its feed; runs created since
	// the last change are covered too).
	OnUpdate func(jobs, runs []string)

	mu      sync.RWMutex
	jobs    map[string]bool
	runs    map[string]bool
	etag    string
	fetched time.Time
}

type coreList struct {
	GeneratedAt string `json:"generated_at"`
	JobIDs      []struct {
		ID string `json:"id"`
	} `json:"job_ids"`
	RunIDs []struct {
		ID string `json:"id"`
	} `json:"run_ids"`
}

func (c *Core) Revoked(jobID, runID string) (bool, string) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if c.jobs[jobID] {
		return true, "job_revoked"
	}
	if c.runs[runID] {
		return true, "run_revoked"
	}
	return false, ""
}

// Snapshot returns the revoked job and run ids of the last fetch.
func (c *Core) Snapshot() (jobs, runs []string) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	for j := range c.jobs {
		jobs = append(jobs, j)
	}
	for r := range c.runs {
		runs = append(runs, r)
	}
	return jobs, runs
}

func (c *Core) notify() {
	if c.OnUpdate != nil {
		js, rs := c.Snapshot()
		c.OnUpdate(js, rs)
	}
}

// Fresh reports when the list was last fetched successfully.
func (c *Core) Fresh() time.Time {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.fetched
}

// Poll fetches once.
func (c *Core) Poll(ctx context.Context) error {
	if c.URL == "" {
		c.mu.Lock()
		c.fetched = time.Now()
		c.mu.Unlock()
		return nil
	}
	cl := c.Client
	if cl == nil {
		cl = &http.Client{Timeout: 5 * time.Second}
	}
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, c.URL, nil)
	if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}
	c.mu.RLock()
	if c.etag != "" {
		req.Header.Set("If-None-Match", c.etag)
	}
	c.mu.RUnlock()
	resp, err := cl.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotModified {
		c.mu.Lock()
		c.fetched = time.Now()
		c.mu.Unlock()
		c.notify()
		return nil
	}
	if resp.StatusCode != 200 {
		return fmt.Errorf("core revocations: HTTP %d", resp.StatusCode)
	}
	var l coreList
	if err := json.NewDecoder(io.LimitReader(resp.Body, 32<<20)).Decode(&l); err != nil {
		return err
	}
	jobs, runs := map[string]bool{}, map[string]bool{}
	for _, j := range l.JobIDs {
		jobs[j.ID] = true
	}
	for _, r := range l.RunIDs {
		runs[r.ID] = true
	}
	c.mu.Lock()
	c.jobs, c.runs, c.etag, c.fetched = jobs, runs, resp.Header.Get("ETag"), time.Now()
	c.mu.Unlock()
	c.notify()
	return nil
}

// Run polls until ctx is done.
func (c *Core) Run(ctx context.Context, log *slog.Logger) {
	iv := c.Interval
	if iv == 0 {
		iv = 60 * time.Second
	}
	for {
		if err := c.Poll(ctx); err != nil && log != nil {
			log.Warn("core revocation poll failed", "err", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(iv):
		}
	}
}

// Exec polls one or more exec revocation feeds with a static reader token.
type Exec struct {
	URLs     []string
	Token    string
	Interval time.Duration
	Client   *http.Client

	mu      sync.RWMutex
	runs    map[string]string
	cursors map[string]string
	fetched time.Time
}

func (e *Exec) Revoked(_, runID string) (bool, string) {
	e.mu.RLock()
	defer e.mu.RUnlock()
	if r, ok := e.runs[runID]; ok {
		return true, "exec_revoked:" + r
	}
	return false, ""
}

// Fresh reports the last time every URL answered.
func (e *Exec) Fresh() time.Time {
	e.mu.RLock()
	defer e.mu.RUnlock()
	return e.fetched
}

// Add records a revocation directly (tests, and the runner's own feed).
func (e *Exec) Add(runID, reason string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.runs == nil {
		e.runs = map[string]string{}
	}
	e.runs[runID] = reason
}

// Poll fetches every URL once. A URL that does not answer is skipped (the kind runner-api is optional locally).
func (e *Exec) Poll(ctx context.Context) error {
	cl := e.Client
	if cl == nil {
		cl = &http.Client{Timeout: 5 * time.Second}
	}
	e.mu.Lock()
	if e.runs == nil {
		e.runs = map[string]string{}
	}
	if e.cursors == nil {
		e.cursors = map[string]string{}
	}
	e.mu.Unlock()
	okAny := false
	var firstErr error
	for _, u := range e.URLs {
		e.mu.RLock()
		cur := e.cursors[u]
		e.mu.RUnlock()
		full := u
		if cur != "" {
			sep := "?"
			if strings.Contains(u, "?") {
				sep = "&"
			}
			full = u + sep + "since=" + url.QueryEscape(cur)
		}
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, full, nil)
		if e.Token != "" {
			req.Header.Set("Authorization", "Bearer "+e.Token)
		}
		resp, err := cl.Do(req)
		if err != nil {
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		var l api.RevocationList
		err = json.NewDecoder(io.LimitReader(resp.Body, 32<<20)).Decode(&l)
		resp.Body.Close()
		if resp.StatusCode != 200 || err != nil {
			if firstErr == nil {
				firstErr = fmt.Errorf("%s: HTTP %d %v", u, resp.StatusCode, err)
			}
			continue
		}
		okAny = true
		e.mu.Lock()
		for _, r := range l.Revoked {
			e.runs[r.RunID] = r.Reason
		}
		if l.NextCursor != "" {
			e.cursors[u] = l.NextCursor
		}
		e.mu.Unlock()
	}
	if okAny || len(e.URLs) == 0 {
		e.mu.Lock()
		e.fetched = time.Now()
		e.mu.Unlock()
		return nil
	}
	return firstErr
}

// Run polls until ctx is done.
func (e *Exec) Run(ctx context.Context, log *slog.Logger) {
	iv := e.Interval
	if iv == 0 {
		iv = 10 * time.Second
	}
	for {
		if err := e.Poll(ctx); err != nil && log != nil {
			log.Warn("exec revocation poll failed", "err", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(iv):
		}
	}
}

// Multi combines checkers.
type Multi []Checker

func (m Multi) Revoked(jobID, runID string) (bool, string) {
	for _, c := range m {
		if c == nil {
			continue
		}
		if ok, why := c.Revoked(jobID, runID); ok {
			return true, why
		}
	}
	return false, ""
}
