package jose

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Golden material captured from the gateway shim's former jose package (SignES256/PublicJWK/KeySet) before it was
// folded into this one: a fixed test-only P-256 key, its JWKS, and tokens it signed.
const (
	goldenPEM = `-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgATbzsn15kID9sW//
9j2/ju74WFxh908HahWH5YsjyRyhRANCAATKCkFN4SEtoHOWzg9p0ne4ab5mRNe9
dQWQSBuEAuVda+kgqTUTxCgS4+upwNHis7g8YqcE0FhziA8EAf5BHl1S
-----END PRIVATE KEY-----
`
	goldenJWKS     = `{"keys":[{"kty":"EC","kid":"k1","use":"sig","alg":"ES256","crv":"P-256","x":"ygpBTeEhLaBzls4PadJ3uGm-ZkTXvXUFkEgbhALlXWs","y":"6SCpNRPEKBLj66nA0eKzuDxipwTQWHOIDwQB_kEeXVI"}]}`
	goldenToken    = "eyJhbGciOiJFUzI1NiIsImtpZCI6ImsxIiwidHlwIjoiSldUIn0.eyJhIjoxLCJydW5faWQiOiJydW5feCJ9.RweXL6fI5na4mGUuqQRxedgIZWh0Pb4AnRnz9ynWDou9Rm0tSp0OuswXBSHE1Vdtc1LknxPJoQ4kw2YpJY2ByQ"
	goldenNoTypTok = "eyJhbGciOiJFUzI1NiIsImtpZCI6ImsxIiwidHlwIjoiIn0.eyJhIjoxfQ.izt4iK1690sjSNS0lzqhGxDpI4PYSnkpUBfAYiR6vClcOkXdzA9Nyzrm9FHfFEBykgh361E9_RN5fWShc1RhfQ"
)

func goldenKeySet(t *testing.T) *KeySet {
	p := filepath.Join(t.TempDir(), "jwks.json")
	if err := os.WriteFile(p, []byte(goldenJWKS), 0o644); err != nil {
		t.Fatal(err)
	}
	return NewKeySet("file://" + p)
}

func TestKeySetGoldenKeyMaterial(t *testing.T) {
	k, err := ParsePrivateKeyPEM([]byte(goldenPEM))
	if err != nil {
		t.Fatal(err)
	}
	var set JWKS
	if err := json.Unmarshal([]byte(goldenJWKS), &set); err != nil {
		t.Fatal(err)
	}
	if got := PublicJWK(&k.PublicKey, "k1"); got != set.Keys[0] {
		t.Fatalf("PublicJWK = %+v, want %+v", got, set.Keys[0])
	}
	// Signer.Sign produces the same signing input (header + payload) the shim's SignES256 did.
	tok, err := (&Signer{Key: k, Kid: "k1"}).Sign("JWT", map[string]any{"run_id": "run_x", "a": 1})
	if err != nil {
		t.Fatal(err)
	}
	gp := strings.Split(goldenToken, ".")
	if np := strings.Split(tok, "."); np[0] != gp[0] || np[1] != gp[1] {
		t.Fatalf("signing input changed: %s.%s", np[0], np[1])
	}
	if _, payload, err := goldenKeySet(t).Verify(tok); err != nil || string(payload) != `{"a":1,"run_id":"run_x"}` {
		t.Fatal(err, string(payload))
	}
}

func TestKeySetGoldenVerify(t *testing.T) {
	ks := goldenKeySet(t)
	h, payload, err := ks.Verify(goldenToken)
	if err != nil || string(payload) != `{"a":1,"run_id":"run_x"}` || h != (Header{Alg: "ES256", Kid: "k1", Typ: "JWT"}) {
		t.Fatalf("golden token: %+v %s %v", h, payload, err)
	}
	if _, payload, err := ks.Verify(goldenNoTypTok); err != nil || string(payload) != `{"a":1}` {
		t.Fatalf("golden token without typ: %s %v", payload, err)
	}
	other := GenerateSigner("k1")
	wrongKey, _ := other.Sign("JWT", map[string]any{"a": 1})
	unknownKid, _ := (&Signer{Key: other.Key, Kid: "k9"}).Sign("", map[string]any{})
	parts := strings.Split(goldenToken, ".")
	// Error strings are surfaced in the shim's 401 messages ("run token signature: %v"), so they are pinned.
	for name, c := range map[string]struct{ tok, want string }{
		"malformed":  {"abc", "not a compact JWS"},
		"badhdr":     {"!!." + parts[1] + "." + parts[2], "header: illegal base64 data at input byte 0"},
		"badpayload": {parts[0] + ".!!." + parts[2], "payload: illegal base64 data at input byte 0"},
		"badsig":     {parts[0] + "." + parts[1] + ".!!", "signature: illegal base64 data at input byte 0"},
		"short":      {parts[0] + "." + parts[1] + ".AAAA", "bad signature"},
		"none":       {"eyJhbGciOiJub25lIn0." + parts[1] + "." + parts[2], `alg "none" not allowed`},
		"wrongkey":   {wrongKey, "bad signature"},
		"unknownkid": {unknownKid, `unknown kid "k9"`},
	} {
		if _, _, err := ks.Verify(c.tok); err == nil || err.Error() != c.want {
			t.Errorf("%s: err = %v, want %q", name, err, c.want)
		}
	}
	if ok, err := ks.Status(); !ok || err != nil {
		t.Fatalf("status %v %v", ok, err)
	}
}

func TestKeySetUnavailable(t *testing.T) {
	ks := NewKeySet("file:///nonexistent.json")
	if _, _, err := ks.Verify(goldenToken); err == nil || err.Error() != "jwks unavailable: open /nonexistent.json: no such file or directory" {
		t.Fatal(err)
	}
	if ok, err := ks.Status(); ok || err == nil {
		t.Fatalf("status %v %v", ok, err)
	}
}
