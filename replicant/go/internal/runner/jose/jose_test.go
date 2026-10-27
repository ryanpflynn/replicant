package jose

import (
	"os"
	"path/filepath"
	"testing"
)

func TestSignVerifyAndPEM(t *testing.T) {
	s := GenerateSigner("")
	tok, err := s.Sign("JWT", map[string]any{"a": 1})
	if err != nil {
		t.Fatal(err)
	}
	p, err := Parse(tok)
	if err != nil {
		t.Fatal(err)
	}
	if err := p.VerifyWith(&JWKS{Keys: []JWK{s.PublicJWK()}}); err != nil {
		t.Fatal(err)
	}
	other := GenerateSigner(s.Kid)
	if p.VerifyWith(&JWKS{Keys: []JWK{other.PublicJWK()}}) == nil {
		t.Fatal("verified with the wrong key")
	}
	f := filepath.Join(t.TempDir(), "k.pem")
	_ = os.WriteFile(f, s.PEM(), 0o600)
	l, err := LoadSigner(f, "kid-1")
	if err != nil || l.Kid != "kid-1" || l.Key.D.Cmp(s.Key.D) != 0 {
		t.Fatalf("load: %v", err)
	}
}
