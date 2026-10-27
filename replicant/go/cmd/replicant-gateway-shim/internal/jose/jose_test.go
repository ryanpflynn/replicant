package jose

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"replicant.example.com/go/cmd/replicant-gateway-shim/internal/ids"
)

func TestSignVerifyRoundTrip(t *testing.T) {
	k, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	other, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	p := filepath.Join(t.TempDir(), "jwks.json")
	b, _ := json.Marshal(JWKSet{Keys: []JWK{PublicJWK(&k.PublicKey, "k1")}})
	_ = os.WriteFile(p, b, 0o644)
	ks := NewKeySet("file://" + p)
	tok, err := SignES256(k, Header{Alg: "ES256", Kid: "k1"}, map[string]any{"a": 1})
	if err != nil {
		t.Fatal(err)
	}
	if _, payload, err := ks.Verify(tok); err != nil || string(payload) != `{"a":1}` {
		t.Fatal(err, string(payload))
	}
	bad, _ := SignES256(other, Header{Alg: "ES256", Kid: "k1"}, map[string]any{"a": 1})
	if _, _, err := ks.Verify(bad); err == nil {
		t.Fatal("wrong key accepted")
	}
	unk, _ := SignES256(k, Header{Alg: "ES256", Kid: "k9"}, map[string]any{})
	if _, _, err := ks.Verify(unk); err == nil {
		t.Fatal("unknown kid accepted")
	}
	parts := strings.Split(tok, ".")
	if _, _, err := ks.Verify(parts[0] + "." + parts[1] + ".AAAA"); err == nil {
		t.Fatal("short sig accepted")
	}
	none := strings.Replace(tok, parts[0], "eyJhbGciOiJub25lIn0", 1)
	if _, _, err := ks.Verify(none); err == nil {
		t.Fatal("alg none accepted")
	}
}

func TestIDsPattern(t *testing.T) {
	seen := map[string]bool{}
	for i := 0; i < 1000; i++ {
		id := ids.New("gwu")
		if len(id) != 30 || strings.ContainsAny(id[4:], "ilou") || seen[id] {
			t.Fatal(id)
		}
		seen[id] = true
	}
}
