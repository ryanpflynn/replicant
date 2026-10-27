// Package jose verifies ES256 compact JWS tokens against JWK Sets fetched over HTTP (or file:// locally).
package jose

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

// Header is the JOSE header of a compact JWS.
type Header struct {
	Alg string `json:"alg"`
	Kid string `json:"kid"`
	Typ string `json:"typ"`
}

// JWK is the subset of an EC P-256 public JWK we need.
type JWK struct {
	Kty string `json:"kty"`
	Kid string `json:"kid"`
	Use string `json:"use"`
	Alg string `json:"alg"`
	Crv string `json:"crv"`
	X   string `json:"x"`
	Y   string `json:"y"`
}

// JWKSet is RFC 7517.
type JWKSet struct {
	Keys []JWK `json:"keys"`
}

var b64 = base64.RawURLEncoding

// Split parses a compact JWS without verifying it.
func Split(token string) (Header, []byte, []byte, string, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return Header{}, nil, nil, "", errors.New("not a compact JWS")
	}
	hb, err := b64.DecodeString(parts[0])
	if err != nil {
		return Header{}, nil, nil, "", fmt.Errorf("header: %w", err)
	}
	var h Header
	if err := json.Unmarshal(hb, &h); err != nil {
		return Header{}, nil, nil, "", fmt.Errorf("header: %w", err)
	}
	payload, err := b64.DecodeString(parts[1])
	if err != nil {
		return Header{}, nil, nil, "", fmt.Errorf("payload: %w", err)
	}
	sig, err := b64.DecodeString(parts[2])
	if err != nil {
		return Header{}, nil, nil, "", fmt.Errorf("signature: %w", err)
	}
	return h, payload, sig, parts[0] + "." + parts[1], nil
}

// PublicKey converts a JWK to an ECDSA public key.
func (k JWK) PublicKey() (*ecdsa.PublicKey, error) {
	if k.Kty != "EC" || (k.Crv != "" && k.Crv != "P-256") {
		return nil, fmt.Errorf("unsupported key kty=%s crv=%s", k.Kty, k.Crv)
	}
	xb, err := b64.DecodeString(k.X)
	if err != nil {
		return nil, err
	}
	yb, err := b64.DecodeString(k.Y)
	if err != nil {
		return nil, err
	}
	pk := &ecdsa.PublicKey{Curve: elliptic.P256(), X: new(big.Int).SetBytes(xb), Y: new(big.Int).SetBytes(yb)}
	if !pk.Curve.IsOnCurve(pk.X, pk.Y) {
		return nil, errors.New("point not on curve")
	}
	return pk, nil
}

// VerifyES256 checks an ES256 (r||s, 64 bytes) signature.
func VerifyES256(pk *ecdsa.PublicKey, signingInput string, sig []byte) bool {
	if len(sig) != 64 {
		return false
	}
	h := sha256.Sum256([]byte(signingInput))
	r := new(big.Int).SetBytes(sig[:32])
	s := new(big.Int).SetBytes(sig[32:])
	return ecdsa.Verify(pk, h[:], r, s)
}

// KeySet caches a remote JWKS (≤ maxAge fresh; stale keys kept up to staleMax on fetch failure).
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
	var set JWKSet
	if err := json.Unmarshal(body, &set); err != nil {
		return nil, err
	}
	keys := map[string]*ecdsa.PublicKey{}
	for _, k := range set.Keys {
		if k.Alg != "" && k.Alg != "ES256" {
			continue
		}
		pk, err := k.PublicKey()
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

// Verify verifies token against the key set and returns header and payload.
func (ks *KeySet) Verify(token string) (Header, []byte, error) {
	h, payload, sig, si, err := Split(token)
	if err != nil {
		return h, nil, err
	}
	if h.Alg != "ES256" {
		return h, nil, fmt.Errorf("alg %q not allowed", h.Alg)
	}
	pk, err := ks.Key(h.Kid)
	if err != nil {
		return h, nil, err
	}
	if !VerifyES256(pk, si, sig) {
		return h, nil, errors.New("bad signature")
	}
	return h, payload, nil
}
