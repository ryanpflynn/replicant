package main

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"replicant.example.com/go/cmd/replicant-gateway-shim/internal/ids"
	"replicant.example.com/go/cmd/replicant-gateway-shim/internal/jose"
)

// devtool is a local-test helper, never used by the running service:
//
//	devtool mint     --dir D [--tenant T --role R --budget USD --models a,b --issuer URL --grant-issuer ISS]
//	                 writes D/runner.key.pem, D/grant.key.pem (reused when present), D/runner-jwks.json,
//	                 D/grant-jwks.json, D/run-token (0600, never printed) and D/run.json (ids only).
//	devtool register --dir D --url http://127.0.0.1:8420 --service-token-file F [--stub root/set/key]
//	                 PUTs the run policy for D/run.json.
func devtool(args []string) int {
	if len(args) == 0 {
		fmt.Fprintln(os.Stderr, "usage: devtool mint|register ...")
		return 2
	}
	switch args[0] {
	case "mint":
		return devMint(args[1:])
	case "register":
		return devRegister(args[1:])
	}
	fmt.Fprintln(os.Stderr, "unknown devtool command", args[0])
	return 2
}

func loadOrCreateKey(path string) (*ecdsa.PrivateKey, error) {
	if b, err := os.ReadFile(path); err == nil {
		return jose.ParsePrivateKeyPEM(b)
	}
	k, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}
	pemb, err := jose.MarshalPrivateKeyPEM(k)
	if err != nil {
		return nil, err
	}
	return k, os.WriteFile(path, pemb, 0o600)
}

type runInfo struct {
	TenantID        string   `json:"tenant_id"`
	JobID           string   `json:"job_id"`
	RunID           string   `json:"run_id"`
	Role            string   `json:"role"`
	ReleaseBundleID string   `json:"release_bundle_id"`
	BudgetUSD       float64  `json:"budget_usd"`
	Models          []string `json:"models"`
	Issuer          string   `json:"issuer"`
	GrantIssuer     string   `json:"grant_issuer"`
}

func devMint(args []string) int {
	fs := flag.NewFlagSet("mint", flag.ContinueOnError)
	dir := fs.String("dir", "", "output directory")
	tenant := fs.String("tenant", "acme-payments", "tenant_id")
	role := fs.String("role", "implementer", "role")
	budget := fs.Float64("budget", 15, "budget_usd")
	models := fs.String("models", "anthropic/claude-sonnet-5-5,anthropic/claude-opus-5-5,anthropic/claude-haiku-5-5", "policy models")
	issuer := fs.String("issuer", "http://runner-local:8480", "run token iss")
	grantIss := fs.String("grant-issuer", "replicant-local-core", "run grant iss")
	ttl := fs.Duration("ttl", time.Hour, "token lifetime")
	if err := fs.Parse(args); err != nil || *dir == "" {
		fmt.Fprintln(os.Stderr, "mint: --dir is required")
		return 2
	}
	if err := os.MkdirAll(*dir, 0o700); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	rk, err := loadOrCreateKey(filepath.Join(*dir, "runner.key.pem"))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	gk, err := loadOrCreateKey(filepath.Join(*dir, "grant.key.pem"))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	writeJSON := func(name string, v any, mode os.FileMode) error {
		b, _ := json.MarshalIndent(v, "", "  ")
		return os.WriteFile(filepath.Join(*dir, name), b, mode)
	}
	_ = writeJSON("runner-jwks.json", jose.JWKSet{Keys: []jose.JWK{jose.PublicJWK(&rk.PublicKey, "runner-dev-1")}}, 0o644)
	_ = writeJSON("grant-jwks.json", jose.JWKSet{Keys: []jose.JWK{jose.PublicJWK(&gk.PublicKey, "rg-dev-1")}}, 0o644)

	now := time.Now()
	ri := runInfo{TenantID: *tenant, JobID: ids.New("job"), RunID: ids.New("run"), Role: *role, ReleaseBundleID: ids.New("rb"),
		BudgetUSD: *budget, Models: strings.Split(*models, ","), Issuer: *issuer, GrantIssuer: *grantIss}
	grantJti := ids.New("rgr")
	actingFor := ids.New("usr")
	grant, err := jose.SignES256(gk, jose.Header{Alg: "ES256", Kid: "rg-dev-1", Typ: "replicant-run-grant+jwt"}, map[string]any{
		"iss": ri.GrantIssuer, "typ": "replicant-run-grant+jwt", "jti": grantJti, "iat": now.Unix(), "exp": now.Add(*ttl + time.Hour).Unix(),
		"tenant_id": ri.TenantID, "job_id": ri.JobID, "run_id": ri.RunID, "role": ri.Role, "repo_ids": []string{"repo-ledger-api"},
		"audiences": []string{"ai-gateway", "runner-sandbox"}, "scopes": []string{"gateway:invoke", "inbox:read"},
		"budget_usd": ri.BudgetUSD, "acting_for": actingFor, "spec_digest": "sha256:" + strings.Repeat("0", 64),
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	tok, err := jose.SignES256(rk, jose.Header{Alg: "ES256", Kid: "runner-dev-1", Typ: "JWT"}, map[string]any{
		"iss": ri.Issuer, "sub": "run:" + ri.RunID, "aud": "ai-gateway", "exp": now.Add(*ttl).Unix(), "iat": now.Unix(),
		"jti": ids.New("rtk"), "tenant_id": ri.TenantID, "job_id": ri.JobID, "run_id": ri.RunID, "role": ri.Role,
		"release_bundle_id": ri.ReleaseBundleID, "repo_ids": []string{"repo-ledger-api"}, "scopes": []string{"gateway:invoke"},
		"budget_usd": ri.BudgetUSD, "acting_for": actingFor, "grant": grant, "grant_jti": grantJti,
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	if err := os.WriteFile(filepath.Join(*dir, "run-token"), []byte(tok), 0o600); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	_ = writeJSON("run.json", ri, 0o644)
	fmt.Printf("minted run %s (job %s, role %s); token written to %s (not printed)\n", ri.RunID, ri.JobID, ri.Role, filepath.Join(*dir, "run-token"))
	return 0
}

func devRegister(args []string) int {
	fs := flag.NewFlagSet("register", flag.ContinueOnError)
	dir := fs.String("dir", "", "directory with run.json")
	url := fs.String("url", "http://127.0.0.1:8420", "shim base URL")
	tokFile := fs.String("service-token-file", "", "service token file")
	stub := fs.String("stub", "", "root/set/key")
	if err := fs.Parse(args); err != nil || *dir == "" || *tokFile == "" {
		fmt.Fprintln(os.Stderr, "register: --dir and --service-token-file are required")
		return 2
	}
	var ri runInfo
	b, err := os.ReadFile(filepath.Join(*dir, "run.json"))
	if err == nil {
		err = json.Unmarshal(b, &ri)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	tok, err := os.ReadFile(*tokFile)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	pol := map[string]any{"tenant_id": ri.TenantID, "job_id": ri.JobID, "role": ri.Role, "release_bundle_id": ri.ReleaseBundleID,
		"models": ri.Models, "budget_usd": ri.BudgetUSD, "max_concurrency": 4, "expires_at": time.Now().Add(6 * time.Hour).UTC().Format(time.RFC3339)}
	if *stub != "" {
		p := strings.SplitN(*stub, "/", 3)
		if len(p) != 3 {
			fmt.Fprintln(os.Stderr, "--stub must be root/set/key")
			return 2
		}
		pol["stub"] = map[string]string{"root": p[0], "set": p[1], "key": p[2]}
	}
	body, _ := json.Marshal(pol)
	req, _ := http.NewRequest("PUT", strings.TrimRight(*url, "/")+"/internal/v1/runs/"+ri.RunID+"/policy", bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(string(tok)))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	defer resp.Body.Close()
	out, _ := io.ReadAll(resp.Body)
	fmt.Printf("PUT policy %s: %d %s\n", ri.RunID, resp.StatusCode, strings.TrimSpace(string(out)))
	if resp.StatusCode != 200 {
		return 1
	}
	return 0
}
