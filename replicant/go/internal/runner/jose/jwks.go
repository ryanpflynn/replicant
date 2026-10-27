package jose

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sync"
	"time"
)

// RemoteJWKS fetches and caches one or more JWK Sets (cache ≤ TTL; refresh on unknown kid at most every 10 s).
type RemoteJWKS struct {
	URLs   []string
	TTL    time.Duration
	Client *http.Client
	// Static keys (tests, or a local issuer's own key) are always included.
	Static []JWK

	mu        sync.Mutex
	keys      []JWK
	fetchedAt time.Time
	lastTry   time.Time
	lastErr   error
}

// Keys returns the current key set, refreshing when stale.
func (r *RemoteJWKS) Keys(ctx context.Context, wantKid string) (*JWKS, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	ttl := r.TTL
	if ttl == 0 {
		ttl = 10 * time.Minute
	}
	stale := time.Since(r.fetchedAt) > ttl
	missing := wantKid != "" && !hasKid(r.keys, wantKid) && !hasKid(r.Static, wantKid)
	if (stale || missing) && (time.Since(r.lastTry) > 10*time.Second || r.fetchedAt.IsZero()) && len(r.URLs) > 0 {
		r.lastTry = time.Now()
		keys, err := r.fetch(ctx)
		if err == nil {
			r.keys, r.fetchedAt, r.lastErr = keys, time.Now(), nil
		} else {
			r.lastErr = err
		}
	}
	all := append(append([]JWK{}, r.Static...), r.keys...)
	if len(all) == 0 {
		if r.lastErr != nil {
			return nil, r.lastErr
		}
		return nil, errors.New("no keys")
	}
	return &JWKS{Keys: all}, nil
}

// Loaded reports whether at least one remote fetch succeeded (readiness).
func (r *RemoteJWKS) Loaded() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return !r.fetchedAt.IsZero() || len(r.URLs) == 0
}

func hasKid(keys []JWK, kid string) bool {
	for _, k := range keys {
		if k.Kid == kid {
			return true
		}
	}
	return false
}

func (r *RemoteJWKS) fetch(ctx context.Context) ([]JWK, error) {
	c := r.Client
	if c == nil {
		c = &http.Client{Timeout: 5 * time.Second}
	}
	var out []JWK
	var errs []error
	for _, u := range r.URLs {
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
		resp, err := c.Do(req)
		if err != nil {
			errs = append(errs, err)
			continue
		}
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		resp.Body.Close()
		if resp.StatusCode != 200 {
			errs = append(errs, fmt.Errorf("%s: HTTP %d", u, resp.StatusCode))
			continue
		}
		var s JWKS
		if err := json.Unmarshal(b, &s); err != nil {
			errs = append(errs, fmt.Errorf("%s: %w", u, err))
			continue
		}
		out = append(out, s.Keys...)
	}
	if len(out) == 0 {
		return nil, errors.Join(errs...)
	}
	return out, nil
}

// VerifyToken parses token and verifies it against the set.
func (r *RemoteJWKS) VerifyToken(ctx context.Context, token string) (*Parsed, error) {
	p, err := Parse(token)
	if err != nil {
		return nil, err
	}
	set, err := r.Keys(ctx, p.Header.Kid)
	if err != nil {
		return nil, err
	}
	if err := p.VerifyWith(set); err != nil {
		return nil, err
	}
	return p, nil
}
