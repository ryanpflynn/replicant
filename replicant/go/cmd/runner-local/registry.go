package main

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/ids"
	"example.com/replicant/internal/runner/s3x"
)

// Registry resolves bundles and definitions from the registry bucket (RS-4, C-DEF §6), verifying
// sha256(body) == digest and caching blobs by digest forever.
type Registry struct {
	S3     *s3x.Client
	Bucket string
	// Get overrides object reads (tests).
	Get func(ctx context.Context, key string) ([]byte, error)

	mu    sync.Mutex
	blobs map[string][]byte
}

type envelope struct {
	APIVersion string          `json:"apiVersion"`
	Kind       string          `json:"kind"`
	Metadata   json.RawMessage `json:"metadata"`
	Spec       json.RawMessage `json:"spec"`
}

type pinned struct {
	Ref    string `json:"ref"`
	Digest string `json:"digest"`
}

type bundleSpec struct {
	Agents       []pinned        `json:"agents"`
	Environments []pinned        `json:"environments"`
	Policies     []pinned        `json:"policies"`
	TenantLayer  json.RawMessage `json:"tenant_layer,omitempty"`
}

// AgentDef is the subset of AgentDefinition.spec runner-local needs.
type AgentDef struct {
	Roles         []string `json:"roles"`
	WorkloadClass string   `json:"workloadClass"`
	CLI           string   `json:"cli"`
	CLIImage      struct {
		Image   string `json:"image"`
		Version string `json:"version"`
	} `json:"cliImage"`
	Steering struct {
		Live bool `json:"live"`
	} `json:"steering"`
}

// EnvDef is the subset of EnvironmentDefinition.spec runner-local needs.
type EnvDef struct {
	Image struct {
		Resolved struct {
			Image string `json:"image"`
		} `json:"resolved"`
	} `json:"image"`
	Repo struct {
		FetchDepth *int32 `json:"fetchDepth"`
	} `json:"repo"`
	Env              map[string]string          `json:"env"`
	ResourceProfiles map[string]ResourceProfile `json:"resourceProfiles"`
	ProfileByRole    map[string]string          `json:"profileByRole"`
}

type ResourceProfile struct {
	Requests  Quantities `json:"requests"`
	Limits    Quantities `json:"limits"`
	Workspace struct {
		SizeLimit string `json:"sizeLimit"`
	} `json:"workspace"`
}

type Quantities struct {
	CPU              string `json:"cpu"`
	Memory           string `json:"memory"`
	EphemeralStorage string `json:"ephemeralStorage"`
}

// Resolved is what admission returns.
type Resolved struct {
	Agent      AgentDef
	Env        EnvDef
	Profile    ResourceProfile
	Policies   map[string]json.RawMessage // policyType -> spec
	PolicyNote string
}

func (r *Registry) getObj(ctx context.Context, key string) ([]byte, error) {
	if r.Get != nil {
		return r.Get(ctx, key)
	}
	b, _, err := r.S3.Get(ctx, r.Bucket, key)
	return b, err
}

// blob reads blobs/sha256/<hex>.json and verifies sha256(raw bytes) == digest (C-DEF §6; the body is the JCS bytes
// of the hashed object, CONTROL-06).
func (r *Registry) blob(ctx context.Context, digest string) ([]byte, error) {
	if !strings.HasPrefix(digest, "sha256:") || len(digest) != 71 {
		return nil, fmt.Errorf("bad digest %q", digest)
	}
	r.mu.Lock()
	if b, ok := r.blobs[digest]; ok {
		r.mu.Unlock()
		return b, nil
	}
	r.mu.Unlock()
	hex := strings.TrimPrefix(digest, "sha256:")
	b, err := r.getObj(ctx, "blobs/sha256/"+hex+".json")
	if err != nil {
		return nil, err
	}
	if s3x.Sha256Hex(b) != hex {
		return nil, fmt.Errorf("blob %s digest mismatch", digest)
	}
	r.mu.Lock()
	if r.blobs == nil {
		r.blobs = map[string][]byte{}
	}
	r.blobs[digest] = b
	r.mu.Unlock()
	return b, nil
}

// BundleSpec reads the pointer bundles/<rb>.json and the bundle blob (which is the JCS of the bundle spec),
// checking rb == "rb_" + b32_130(digest).
func (r *Registry) BundleSpec(ctx context.Context, rb string) (*bundleSpec, error) {
	ptr, err := r.getObj(ctx, "bundles/"+rb+".json")
	if err != nil {
		if errors.Is(err, s3x.ErrNotFound) {
			return nil, &ErrNotResolvable{"release_bundle_id", "bundle pointer not found"}
		}
		return nil, fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	var p struct {
		ReleaseBundleID string `json:"release_bundle_id"`
		Digest          string `json:"digest"`
	}
	if err := json.Unmarshal(ptr, &p); err != nil || p.Digest == "" || p.ReleaseBundleID != rb {
		return nil, &ErrNotResolvable{"release_bundle_id", "bad bundle pointer"}
	}
	raw, err := hexDecode(strings.TrimPrefix(p.Digest, "sha256:"))
	if err != nil || "rb_"+ids.B32_130(raw) != rb {
		return nil, &ErrNotResolvable{"release_bundle_id", "bundle digest does not match release_bundle_id"}
	}
	bb, err := r.blob(ctx, p.Digest)
	if err != nil {
		return nil, &ErrNotResolvable{"release_bundle_id", err.Error()}
	}
	var spec bundleSpec
	if err := json.Unmarshal(bb, &spec); err != nil {
		return nil, &ErrNotResolvable{"release_bundle_id", "bundle blob: " + err.Error()}
	}
	return &spec, nil
}

func hexDecode(s string) ([]byte, error) { return hex.DecodeString(s) }

// ErrNotResolvable maps to 422 definition_not_resolvable.
type ErrNotResolvable struct{ Field, Msg string }

func (e *ErrNotResolvable) Error() string { return e.Field + ": " + e.Msg }

// ErrUnavailable maps to 503 runner_unavailable (registry unreachable on a cold cache).
var ErrUnavailable = errors.New("registry unavailable")

// Resolve implements A-6, A-7 and the profile part of A-8.
func (r *Registry) Resolve(ctx context.Context, rs *api.RunSpec) (*Resolved, error) {
	be, err := r.BundleSpec(ctx, rs.ReleaseBundleID)
	if err != nil {
		return nil, err
	}
	out := &Resolved{Policies: map[string]json.RawMessage{}}
	agentRef := fmt.Sprintf("agent/%s@%d", rs.AgentDefinition.Name, rs.AgentDefinition.Version)
	envRef := fmt.Sprintf("environment/%s@%d", rs.EnvironmentDefinition.Name, rs.EnvironmentDefinition.Version)
	if !member(be.Agents, agentRef, rs.AgentDefinition.Digest) {
		return nil, &ErrNotResolvable{"agent_definition", agentRef + " not in bundle closure"}
	}
	if !member(be.Environments, envRef, rs.EnvironmentDefinition.Digest) {
		return nil, &ErrNotResolvable{"environment_definition", envRef + " not in bundle closure"}
	}
	ab, err := r.blob(ctx, rs.AgentDefinition.Digest)
	if err != nil {
		return nil, &ErrNotResolvable{"agent_definition", err.Error()}
	}
	var ae envelope
	if err := json.Unmarshal(ab, &ae); err != nil || json.Unmarshal(ae.Spec, &out.Agent) != nil {
		return nil, &ErrNotResolvable{"agent_definition", "bad agent definition blob"}
	}
	eb, err := r.blob(ctx, rs.EnvironmentDefinition.Digest)
	if err != nil {
		return nil, &ErrNotResolvable{"environment_definition", err.Error()}
	}
	var ee envelope
	if err := json.Unmarshal(eb, &ee); err != nil || json.Unmarshal(ee.Spec, &out.Env) != nil {
		return nil, &ErrNotResolvable{"environment_definition", "bad environment definition blob"}
	}
	for _, pol := range be.Policies {
		pb, err := r.blob(ctx, pol.Digest)
		if err != nil {
			continue
		}
		var pe struct {
			Spec map[string]json.RawMessage `json:"spec"`
		}
		if json.Unmarshal(pb, &pe) != nil {
			continue
		}
		var typ string
		_ = json.Unmarshal(pe.Spec["policyType"], &typ)
		if typ != "" {
			raw, _ := json.Marshal(pe.Spec)
			out.Policies[typ] = raw
		}
	}
	prof := rs.ResourceProfileOrDefault()
	rp, ok := out.Env.ResourceProfiles[prof]
	if !ok {
		return nil, &AdmissionErr{Field: "resource_profile", Msg: "profile " + prof + " not in environment definition"}
	}
	out.Profile = rp
	return out, nil
}

func member(list []pinned, ref, digest string) bool {
	for _, p := range list {
		if p.Ref == ref && p.Digest == digest {
			return true
		}
	}
	return false
}

// AdmissionErr maps to 422 admission_denied.
type AdmissionErr struct{ Field, Msg string }

func (e *AdmissionErr) Error() string { return e.Field + ": " + e.Msg }
