package jose

import (
	"crypto/ecdsa"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
)

// SignES256 produces a compact JWS (used by tests and the local `devtool` only; the shim never signs tokens).
func SignES256(key *ecdsa.PrivateKey, h Header, claims any) (string, error) {
	hb, err := json.Marshal(h)
	if err != nil {
		return "", err
	}
	cb, err := json.Marshal(claims)
	if err != nil {
		return "", err
	}
	si := b64.EncodeToString(hb) + "." + b64.EncodeToString(cb)
	d := sha256.Sum256([]byte(si))
	r, s, err := ecdsa.Sign(rand.Reader, key, d[:])
	if err != nil {
		return "", err
	}
	sig := make([]byte, 64)
	r.FillBytes(sig[:32])
	s.FillBytes(sig[32:])
	return si + "." + b64.EncodeToString(sig), nil
}

// PublicJWK renders the public half of key as a JWK.
func PublicJWK(key *ecdsa.PublicKey, kid string) JWK {
	x := make([]byte, 32)
	y := make([]byte, 32)
	key.X.FillBytes(x)
	key.Y.FillBytes(y)
	return JWK{Kty: "EC", Kid: kid, Use: "sig", Alg: "ES256", Crv: "P-256", X: b64.EncodeToString(x), Y: b64.EncodeToString(y)}
}

// ParsePrivateKeyPEM reads a PKCS#8 or SEC1 P-256 private key.
func ParsePrivateKeyPEM(b []byte) (*ecdsa.PrivateKey, error) {
	blk, _ := pem.Decode(b)
	if blk == nil {
		return nil, errors.New("no PEM block")
	}
	if k, err := x509.ParsePKCS8PrivateKey(blk.Bytes); err == nil {
		if ek, ok := k.(*ecdsa.PrivateKey); ok {
			return ek, nil
		}
		return nil, errors.New("not an EC key")
	}
	return x509.ParseECPrivateKey(blk.Bytes)
}

// MarshalPrivateKeyPEM writes a PKCS#8 PEM.
func MarshalPrivateKeyPEM(k *ecdsa.PrivateKey) ([]byte, error) {
	der, err := x509.MarshalPKCS8PrivateKey(k)
	if err != nil {
		return nil, err
	}
	return pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}), nil
}
