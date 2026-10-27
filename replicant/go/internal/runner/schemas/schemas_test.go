package schemas_test

import (
	"encoding/json"
	"strings"
	"testing"

	"sigs.k8s.io/yaml"

	"example.com/replicant/internal/runner/jcsx"
	"example.com/replicant/internal/runner/schemas"
)

func exampleRunSpec(t *testing.T) map[string]any {
	t.Helper()
	b, err := schemas.File("runner-api.openapi.yaml")
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]any
	if err := yaml.Unmarshal(b, &doc); err != nil {
		t.Fatal(err)
	}
	ex := doc["components"].(map[string]any)["examples"].(map[string]any)["ImplementerRunSpec"].(map[string]any)["value"].(map[string]any)
	return ex
}

func TestSpecDigestVector(t *testing.T) {
	ex := exampleRunSpec(t)
	b, _ := json.Marshal(ex)
	d, err := jcsx.SpecDigest(b)
	if err != nil {
		t.Fatal(err)
	}
	if d != "sha256:a05ea6bc4aea3f59a4b8f7a20dea9badc8e8d1dcbb8f21b696224337bbabde06" {
		t.Fatalf("digest %s", d)
	}
	ex["trace_context"] = map[string]any{"traceparent": "00-fc0575170c8597ce9805552723c08d6e-7f3e9d2c1b0a4958-01"}
	ex["run_grant"] = "a.b.c"
	b, _ = json.Marshal(ex)
	d2, _ := jcsx.SpecDigest(b)
	if d2 != d {
		t.Fatalf("per-attempt fields changed digest")
	}
	ex["limits"] = map[string]any{"max_cost_usd": 14}
	b, _ = json.Marshal(ex)
	d3, _ := jcsx.SpecDigest(b)
	if d3 == d {
		t.Fatalf("decision field did not change digest")
	}
}

func TestRunSpecSchema(t *testing.T) {
	ex := exampleRunSpec(t)
	if err := schemas.Validate(schemas.RunSpec, ex); err != nil {
		t.Fatalf("example invalid: %v", err)
	}
	ex["bogus"] = 1
	delete(ex, "run_grant")
	err := schemas.Validate(schemas.RunSpec, ex)
	if err == nil {
		t.Fatal("expected violations")
	}
	if !strings.Contains(err.Error(), "bogus") && !strings.Contains(err.Error(), "run_grant") {
		t.Fatalf("unexpected: %v", err)
	}
}

func TestBadTraceparentLookahead(t *testing.T) {
	ex := exampleRunSpec(t)
	ex["trace_context"] = map[string]any{"traceparent": "00-00000000000000000000000000000000-7f3e9d2c1b0a4958-01"}
	if err := schemas.Validate(schemas.RunSpec, ex); err == nil {
		t.Fatal("all-zero trace id must be rejected")
	}
}

func TestInlinedAgentReport(t *testing.T) {
	s, err := schemas.Inlined(schemas.RoleImplementer + "#/$defs/agent_report")
	if err != nil {
		t.Fatal(err)
	}
	b, _ := json.Marshal(s)
	if strings.Contains(string(b), "$ref") {
		t.Fatalf("refs not inlined: %s", b)
	}
	if !strings.Contains(string(b), "tests_run") {
		t.Fatal("missing tests_run")
	}
}

func TestExamplesValidate(t *testing.T) {
	for _, c := range []struct{ loc, file string }{
		{schemas.RunResult, "run-result-implementer.json"},
	} {
		_ = c
	}
}
