package main

import (
	"context"
	"encoding/json"
	"os"
	"strings"
	"testing"

	"example.com/replicant/internal/runner/s3x"
)

// TestRegistryCDEFVectors reads the shared C-DEF vectors (libs/python/replicant-cdef vectors.json, CONTROL-06).
func TestRegistryCDEFVectors(t *testing.T) {
	b, err := os.ReadFile("testdata/cdef-vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	var raw map[string]map[string]any
	if err := json.Unmarshal(b, &raw); err != nil {
		t.Fatal(err)
	}
	d, bd := raw["definition"], raw["bundle"]
	objs := map[string][]byte{
		d["blob_key"].(string):     []byte(d["body_utf8"].(string)),
		bd["blob_key"].(string):    []byte(bd["body_utf8"].(string)),
		bd["pointer_key"].(string): []byte(bd["pointer_body_utf8"].(string)),
	}
	r := &Registry{Get: func(_ context.Context, key string) ([]byte, error) {
		if o, ok := objs[key]; ok {
			return o, nil
		}
		return nil, s3x.ErrNotFound
	}}
	ctx := context.Background()
	spec, err := r.BundleSpec(ctx, bd["release_bundle_id"].(string))
	if err != nil {
		t.Fatalf("bundle: %v", err)
	}
	if len(spec.Environments) != 1 || spec.Environments[0].Digest != d["digest"] {
		t.Fatalf("bundle spec environments %+v", spec.Environments)
	}
	body, err := r.blob(ctx, d["digest"].(string))
	if err != nil || !strings.Contains(string(body), "go-service") {
		t.Fatalf("definition blob: %v", err)
	}
	// a wrong rb for the same pointer content is refused
	objs["bundles/rb_aaaaaaaaaaaaaaaaaaaaaaaaaa.json"] = []byte(`{"release_bundle_id":"rb_aaaaaaaaaaaaaaaaaaaaaaaaaa","digest":"` + bd["digest"].(string) + `"}`)
	if _, err := r.BundleSpec(ctx, "rb_aaaaaaaaaaaaaaaaaaaaaaaaaa"); err == nil {
		t.Fatal("rb/digest mismatch accepted")
	}
	// tampered blob is refused
	objs[d["blob_key"].(string)] = append([]byte(" "), objs[d["blob_key"].(string)]...)
	r2 := &Registry{Get: r.Get}
	if _, err := r2.blob(ctx, d["digest"].(string)); err == nil {
		t.Fatal("tampered blob accepted")
	}
}
