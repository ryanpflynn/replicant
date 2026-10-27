package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"time"

	"example.com/replicant/internal/runner/s3x"
)

// KillSwitches reads the out-of-band kill-switch mirror (A-5; XD-FIX-R2-016; local-runner.md X-27):
// `s3://…/kill-switches/v1/state.json` (compose common env) or `file://…` (X-27). Cached ≤ 30 s; fails
// closed when unreadable and the cache is older than 5 minutes. A missing object means "no switches".
type KillSwitches struct {
	Source string
	S3     *s3x.Client

	mu      sync.Mutex
	state   *ksState
	fetched time.Time
}

type ksState struct {
	GeneratedAt string     `json:"generated_at"`
	Switches    []ksSwitch `json:"switches"`
}

type ksSwitch struct {
	Scope      string `json:"scope"`
	ScopeValue string `json:"scope_value"`
	Switch     string `json:"switch"`
	Mode       string `json:"mode,omitempty"`
	Engaged    bool   `json:"engaged"`
	IssuedAt   string `json:"issued_at"`
	NoticeID   string `json:"notice_id"`
}

// ErrMirrorUnreadable maps to 503 runner_unavailable.
var ErrMirrorUnreadable = errors.New("kill-switch mirror unreadable")

func (k *KillSwitches) load(ctx context.Context) (*ksState, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	if k.Source == "" {
		return &ksState{}, nil
	}
	if k.state != nil && time.Since(k.fetched) < 30*time.Second {
		return k.state, nil
	}
	var b []byte
	var err error
	switch {
	case strings.HasPrefix(k.Source, "file://"):
		b, err = os.ReadFile(strings.TrimPrefix(k.Source, "file://"))
		if os.IsNotExist(err) {
			b, err = []byte(`{"switches":[]}`), nil
		}
	case strings.HasPrefix(k.Source, "s3://"):
		bucket, key, perr := s3x.ParseURI(k.Source)
		if perr != nil {
			return nil, perr
		}
		b, _, err = k.S3.Get(ctx, bucket, key)
		if errors.Is(err, s3x.ErrNotFound) {
			b, err = []byte(`{"switches":[]}`), nil
		}
	default:
		err = fmt.Errorf("unsupported mirror %q", k.Source)
	}
	if err == nil {
		var st ksState
		if err = json.Unmarshal(b, &st); err == nil {
			k.state, k.fetched = &st, time.Now()
			return k.state, nil
		}
	}
	if k.state != nil && time.Since(k.fetched) < 5*time.Minute {
		return k.state, nil
	}
	return nil, fmt.Errorf("%w: %v", ErrMirrorUnreadable, err)
}

// Check returns the field refused (tenant_id | repo | release_bundle_id) or "".
func (k *KillSwitches) Check(ctx context.Context, tenant string, repos []string, bundle string) (string, error) {
	st, err := k.load(ctx)
	if err != nil {
		return "", err
	}
	// latest issued_at per (scope, scope_value, mode) wins
	latest := map[string]ksSwitch{}
	for _, s := range st.Switches {
		mode := s.Switch
		if s.Mode != "" {
			mode = s.Mode
		}
		key := s.Scope + "|" + s.ScopeValue + "|" + mode
		if cur, ok := latest[key]; !ok || s.IssuedAt > cur.IssuedAt {
			s.Switch = mode
			latest[key] = s
		}
	}
	for _, s := range latest {
		if !s.Engaged || s.Switch != "starts" {
			continue
		}
		switch s.Scope {
		case "global":
			return "tenant_id", nil
		case "tenant":
			if s.ScopeValue == tenant {
				return "tenant_id", nil
			}
		case "repo":
			for _, r := range repos {
				if r == s.ScopeValue {
					return "repo", nil
				}
			}
		case "bundle":
			if s.ScopeValue == bundle {
				return "release_bundle_id", nil
			}
		}
	}
	return "", nil
}
