// Package jcsx computes RFC 8785 (JCS) digests, in particular the C-RUNNER spec_digest.
package jcsx

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"

	"github.com/gowebpki/jcs"
)

// PerAttemptFields are the RunSpec fields excluded from spec_digest (C-RUNNER 3.0.0, exhaustive).
var PerAttemptFields = []string{"run_grant", "trace_context"}

// Canonical returns the JCS form of a JSON document.
func Canonical(raw []byte) ([]byte, error) { return jcs.Transform(raw) }

// SpecDigest returns "sha256:<hex>" over the body as received minus the per-attempt fields.
func SpecDigest(body []byte) (string, error) {
	var m map[string]json.RawMessage
	if err := json.Unmarshal(body, &m); err != nil {
		return "", err
	}
	for _, f := range PerAttemptFields {
		delete(m, f)
	}
	b, err := json.Marshal(m)
	if err != nil {
		return "", err
	}
	c, err := jcs.Transform(b)
	if err != nil {
		return "", err
	}
	d := sha256.Sum256(c)
	return "sha256:" + hex.EncodeToString(d[:]), nil
}
