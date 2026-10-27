// Package jose implements the small subset of JOSE Replicant needs: ES256 compact JWS
// signing and verification, P-256 keys from PEM, and JWK Sets (RFC 7517) with caching.
package jose

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"math/big"
	"os"
	"strings"
)

var b64 = base64.RawURLEncoding

// Header is the JOSE header.
type Header struct {
	Alg string `json:"alg"`
	Kid string `json:"kid,omitempty"`
	Typ string `json:"typ,omitempty"`
}

// Signer signs with one ES256 key.
type Signer struct {
	Key *ecdsa.PrivateKey
	Kid string
}

// LoadSigner reads a PKCS#8 or SEC1 PEM P-256 key. If kid is empty, it is the RFC 7638 thumbprint.
func LoadSigner(path, kid string) (*Signer, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	k, err := ParsePrivateKeyPEM(b)
	if err != nil {
		return nil, err
	}
	if kid == "" {
		kid = Thumbprint(&k.PublicKey)
	}
	return &Signer{Key: k, Kid: kid}, nil
}

// ParsePrivateKeyPEM parses a PKCS#8 or SEC1 EC private key.
func ParsePrivateKeyPEM(b []byte) (*ecdsa.PrivateKey, error) {
	blk, _ := pem.Decode(b)
	if blk == nil {
		return nil, errors.New("no PEM block")
	}
	if k, err := x509.ParsePKCS8PrivateKey(blk.Bytes); err == nil {
		ek, ok := k.(*ecdsa.PrivateKey)
		if !ok || ek.Curve != elliptic.P256() {
			return nil, errors.New("not a P-256 key")
		}
		return ek, nil
	}
	ek, err := x509.ParseECPrivateKey(blk.Bytes)
	if err != nil {
		return nil, err
	}
	if ek.Curve != elliptic.P256() {
		return nil, errors.New("not a P-256 key")
	}
	return ek, nil
}

// GenerateSigner creates a fresh key (tests, smoke tooling).
func GenerateSigner(kid string) *Signer {
	k, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		panic(err)
	}
	if kid == "" {
		kid = Thumbprint(&k.PublicKey)
	}
	return &Signer{Key: k, Kid: kid}
}

// PEM returns the PKCS#8 PEM of the key.
func (s *Signer) PEM() []byte {
	der, _ := x509.MarshalPKCS8PrivateKey(s.Key)
	return pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der})
}

// Sign produces a compact JWS over the JSON of claims.
func (s *Signer) Sign(typ string, claims any) (string, error) {
	hb, _ := json.Marshal(Header{Alg: "ES256", Kid: s.Kid, Typ: typ})
	cb, err := json.Marshal(claims)
	if err != nil {
		return "", err
	}
	input := b64.EncodeToString(hb) + "." + b64.EncodeToString(cb)
	d := sha256.Sum256([]byte(input))
	r, ss, err := ecdsa.Sign(rand.Reader, s.Key, d[:])
	if err != nil {
		return "", err
	}
	sig := make([]byte, 64)
	r.FillBytes(sig[:32])
	ss.FillBytes(sig[32:])
	return input + "." + b64.EncodeToString(sig), nil
}

// JWK is a public EC JWK.
type JWK struct {
	Kty string `json:"kty"`
	Crv string `json:"crv,omitempty"`
	Kid string `json:"kid"`
	Use string `json:"use"`
	Alg string `json:"alg"`
	X   string `json:"x,omitempty"`
	Y   string `json:"y,omitempty"`
}

// JWKS is a JWK Set.
type JWKS struct {
	Keys []JWK `json:"keys"`
}

// PublicJWK returns the JWK of the signer's public key.
func (s *Signer) PublicJWK() JWK { return PublicJWK(&s.Key.PublicKey, s.Kid) }

// PublicJWK renders a P-256 public key.
func PublicJWK(p *ecdsa.PublicKey, kid string) JWK {
	x := make([]byte, 32)
	y := make([]byte, 32)
	p.X.FillBytes(x)
	p.Y.FillBytes(y)
	return JWK{Kty: "EC", Crv: "P-256", Kid: kid, Use: "sig", Alg: "ES256", X: b64.EncodeToString(x), Y: b64.EncodeToString(y)}
}

// Thumbprint is the RFC 7638 SHA-256 thumbprint (base64url) of a P-256 key.
func Thumbprint(p *ecdsa.PublicKey) string {
	j := PublicJWK(p, "")
	s := fmt.Sprintf(`{"crv":"P-256","kty":"EC","x":"%s","y":"%s"}`, j.X, j.Y)
	d := sha256.Sum256([]byte(s))
	return b64.EncodeToString(d[:])
}

// Key converts a JWK into a public key.
func (j JWK) Key() (*ecdsa.PublicKey, error) {
	if j.Kty != "EC" || (j.Crv != "" && j.Crv != "P-256") {
		return nil, fmt.Errorf("unsupported jwk %s/%s", j.Kty, j.Crv)
	}
	x, err := b64.DecodeString(j.X)
	if err != nil {
		return nil, err
	}
	y, err := b64.DecodeString(j.Y)
	if err != nil {
		return nil, err
	}
	p := &ecdsa.PublicKey{Curve: elliptic.P256(), X: new(big.Int).SetBytes(x), Y: new(big.Int).SetBytes(y)}
	if !p.Curve.IsOnCurve(p.X, p.Y) {
		return nil, errors.New("point not on curve")
	}
	return p, nil
}

// Parsed is a decoded but not yet verified compact JWS.
type Parsed struct {
	Header       Header
	Payload      []byte
	signingInput string
	sig          []byte
}

// Parse splits and decodes a compact JWS.
func Parse(token string) (*Parsed, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return nil, errors.New("malformed jws")
	}
	hb, err := b64.DecodeString(parts[0])
	if err != nil {
		return nil, fmt.Errorf("header: %w", err)
	}
	var h Header
	if err := json.Unmarshal(hb, &h); err != nil {
		return nil, fmt.Errorf("header: %w", err)
	}
	pb, err := b64.DecodeString(parts[1])
	if err != nil {
		return nil, fmt.Errorf("payload: %w", err)
	}
	sig, err := b64.DecodeString(parts[2])
	if err != nil {
		return nil, fmt.Errorf("signature: %w", err)
	}
	return &Parsed{Header: h, Payload: pb, signingInput: parts[0] + "." + parts[1], sig: sig}, nil
}

// Verify checks the ES256 signature with the given key.
func (p *Parsed) Verify(k *ecdsa.PublicKey) error {
	if p.Header.Alg != "ES256" {
		return fmt.Errorf("alg %q not allowed", p.Header.Alg)
	}
	if len(p.sig) != 64 {
		return errors.New("bad signature length")
	}
	d := sha256.Sum256([]byte(p.signingInput))
	r := new(big.Int).SetBytes(p.sig[:32])
	s := new(big.Int).SetBytes(p.sig[32:])
	if !ecdsa.Verify(k, d[:], r, s) {
		return errors.New("signature invalid")
	}
	return nil
}

// VerifyWith verifies against the key in set with the header kid (or any key if kid is empty).
func (p *Parsed) VerifyWith(set *JWKS) error {
	var lastErr error = errors.New("no matching key")
	for _, j := range set.Keys {
		if p.Header.Kid != "" && j.Kid != p.Header.Kid {
			continue
		}
		k, err := j.Key()
		if err != nil {
			lastErr = err
			continue
		}
		if err := p.Verify(k); err == nil {
			return nil
		} else {
			lastErr = err
		}
	}
	return lastErr
}

// UnsafeClaims decodes the payload of a token without verification (for diagnostics only).
func UnsafeClaims(token string, v any) error {
	p, err := Parse(token)
	if err != nil {
		return err
	}
	return json.Unmarshal(p.Payload, v)
}
