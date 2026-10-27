package shim

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestFixtureScriptsLoad parses every committed replicant.gateway-script/v1 file under tests/fixtures/gateway-scripts
// (monorepo root, five levels up) and checks their structured outputs.
func TestFixtureScriptsLoad(t *testing.T) {
	root := filepath.Join("..", "..", "..", "..", "..", "tests", "fixtures", "gateway-scripts")
	if _, err := os.Stat(root); err != nil {
		t.Skip("fixtures not mounted:", err)
	}
	be := NewScriptBackend(&Config{}, NewMemState())
	n := 0
	_ = filepath.Walk(root, func(p string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() || !strings.HasSuffix(p, ".yaml") {
			return nil
		}
		n++
		s, err := be.load(p)
		if err != nil {
			t.Errorf("%s: %v", p, err)
			return nil
		}
		if len(s.Turns) == 0 || s.Name == "" {
			t.Errorf("%s: empty script", p)
		}
		for i, turn := range s.Turns {
			if (turn.Respond == nil) == (turn.Fault == nil) {
				t.Errorf("%s turn %d: exactly one of respond/fault", p, i)
			}
			if turn.Respond != nil {
				if _, err := structuredOutput(turn.Respond, s); err != nil {
					t.Errorf("%s turn %d: %v", p, i, err)
				}
				if turn.Respond.Usage != nil && turn.Respond.Usage.InputTokens == 0 {
					t.Errorf("%s turn %d: usage parsed as zero", p, i)
				}
			}
		}
		return nil
	})
	if n < 2 {
		t.Fatalf("found %d scripts", n)
	}
}
