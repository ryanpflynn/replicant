package jose

import (
	"crypto/ecdsa"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

// KeySet caches one JWKS by kid (≤ MaxAge fresh; stale keys kept up to StaleMax on fetch failure). The URL may be
// http(s):// or file:// (local). Used by the gateway shim; RemoteJWKS is the multi-URL cache used by the runner.
type KeySet struct {
	URL      string
	MaxAge   time.Duration
	StaleMax time.Duration
	Client   *http.Client

	mu        sync.Mutex
	keys      map[string]*ecdsa.PublicKey
	fetchedAt time.Time
	lastTry   time.Time
	lastErr   error
}

// NewKeySet returns a KeySet with the C-RUNNER cache rules (10 min fresh, 2 h stale).
func NewKeySet(url string) *KeySet {
	return &KeySet{URL: url, MaxAge: 10 * time.Minute, StaleMax: 2 * time.Hour, Client: &http.Client{Timeout: 5 * time.Second}}
}

func (ks *KeySet) fetch() (map[string]*ecdsa.PublicKey, error) {
	var body []byte
	if strings.HasPrefix(ks.URL, "file://") {
		b, err := os.ReadFile(strings.TrimPrefix(ks.URL, "file://"))
		if err != nil {
			return nil, err
		}
		body = b
	} else {
		resp, err := ks.Client.Get(ks.URL)
		if err != nil {
			return nil, err
		}
		defer resp.Body.Close()
		if resp.StatusCode != 200 {
			return nil, fmt.Errorf("jwks %s: http %d", ks.URL, resp.StatusCode)
		}
		body, err = io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		if err != nil {
			return nil, err
		}
	}
	var set JWKS
	if err := json.Unmarshal(body, &set); err != nil {
		return nil, err
	}
	keys := map[string]*ecdsa.PublicKey{}
	for _, k := range set.Keys {
		if k.Alg != "" && k.Alg != "ES256" {
			continue
		}
		pk, err := k.Key()
		if err != nil {
			continue
		}
		keys[k.Kid] = pk
	}
	return keys, nil
}

// Key returns the key for kid, refreshing when stale or the kid is unknown (at most every 10 s on misses).
func (ks *KeySet) Key(kid string) (*ecdsa.PublicKey, error) {
	ks.mu.Lock()
	defer ks.mu.Unlock()
	now := time.Now()
	fresh := ks.keys != nil && now.Sub(ks.fetchedAt) < ks.MaxAge
	if k, ok := ks.keys[kid]; ok && fresh {
		return k, nil
	}
	if now.Sub(ks.lastTry) > 10*time.Second || ks.keys == nil || !fresh {
		ks.lastTry = now
		keys, err := ks.fetch()
		if err == nil {
			ks.keys, ks.fetchedAt, ks.lastErr = keys, now, nil
		} else {
			ks.lastErr = err
		}
	}
	if k, ok := ks.keys[kid]; ok && now.Sub(ks.fetchedAt) < ks.StaleMax {
		return k, nil
	}
	if ks.lastErr != nil {
		return nil, fmt.Errorf("jwks unavailable: %w", ks.lastErr)
	}
	return nil, fmt.Errorf("unknown kid %q", kid)
}

// Status reports whether keys are cached.
func (ks *KeySet) Status() (bool, error) {
	ks.mu.Lock()
	defer ks.mu.Unlock()
	return ks.keys != nil, ks.lastErr
}

// Verify verifies token against the key set and returns header and payload. The ES256 alg is checked before the
// key lookup, and every signature failure (wrong length or invalid) is reported as "bad signature".
func (ks *KeySet) Verify(token string) (Header, []byte, error) {
	if strings.Count(token, ".") != 2 {
		return Header{}, nil, errors.New("not a compact JWS")
	}
	p, err := Parse(token)
	if err != nil {
		return Header{}, nil, err
	}
	if p.Header.Alg != "ES256" {
		return p.Header, nil, fmt.Errorf("alg %q not allowed", p.Header.Alg)
	}
	pk, err := ks.Key(p.Header.Kid)
	if err != nil {
		return p.Header, nil, err
	}
	if p.Verify(pk) != nil {
		return p.Header, nil, errors.New("bad signature")
	}
	return p.Header, p.Payload, nil
}
