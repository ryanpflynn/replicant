// Command runner-local implements the Runner API (C-RUNNER 3.0.0) over the Docker/Podman Engine API
// for the compose profile (spec/02-execution/local-runner.md): admission, idempotency, per-audience run
// tokens, the sandbox listener, the pod-group executor, the in-process notifier and the revocation feed.
package main

import (
	"context"
	"crypto/rand"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/authz"
	"example.com/replicant/internal/runner/engine"
	"example.com/replicant/internal/runner/httpx"
	"example.com/replicant/internal/runner/jose"
	"example.com/replicant/internal/runner/revoke"
	"example.com/replicant/internal/runner/s3x"
)

func main() {
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case "healthcheck":
			os.Exit(httpx.Healthcheck(os.Args[2:]))
		case "version":
			fmt.Println("runner-local", version)
			return
		case "serve":
		default:
			fmt.Fprintln(os.Stderr, "usage: runner-local [serve|healthcheck --url <u>|version]")
			os.Exit(2)
		}
	}
	log := httpx.Logger("runner-local")
	if err := run(log); err != nil {
		log.Error("fatal", "err", err)
		os.Exit(1)
	}
}

var version = "0.1.0"

func run(log *slog.Logger) error {
	cfg, err := loadConfig()
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	signer, err := jose.LoadSigner(cfg.SigningKeyFile, cfg.SigningKid)
	if err != nil {
		return fmt.Errorf("signing key: %w", err)
	}
	workers, err := httpx.ReadTokenMap(cfg.WorkerTokensFile)
	if err != nil {
		return fmt.Errorf("worker tokens: %w", err)
	}
	readers, _ := httpx.ReadTokenMap(cfg.RevocationsReadersFile)
	if readers == nil {
		readers = map[string]string{}
	}
	eng, err := engine.New(cfg.ContainerHost)
	if err != nil {
		return err
	}
	if v, err := eng.Version(ctx); err == nil {
		log.Info("container engine", "engine", v.Engine(), "version", v.Version, "arch", v.Arch)
	} else {
		log.Warn("container engine not answering yet", "err", err)
	}
	if cfg.StateVolume == "" && cfg.StateHostDir == "" {
		cfg.StateVolume = detectStateVolume(ctx, eng, cfg.StateDir)
		log.Info("state volume", "name", cfg.StateVolume)
	}
	if cfg.StateVolume == "" && cfg.StateHostDir == "" {
		return errors.New("set RUNNER_STATE_VOLUME (or RUNNER_STATE_HOST_DIR): cannot mount per-run token directories")
	}
	ak, sk := cfg.S3AccessKey, ""
	if cfg.S3CredentialsFile != "" {
		ak, sk, _, err = s3x.LoadINI(cfg.S3CredentialsFile, "")
		if err != nil {
			return fmt.Errorf("s3 credentials: %w", err)
		}
	} else if b, err := httpx.ReadToken(cfg.S3SecretFile); err == nil {
		sk = b
	}
	s3c := s3x.New(s3x.Config{Endpoint: cfg.S3Endpoint, Region: cfg.S3Region, AccessKey: ak, SecretKey: sk, SendChecksum: cfg.S3SendChecksum})
	store, err := NewStore(cfg.StateDir)
	if err != nil {
		return err
	}
	core := &revoke.Core{URL: cfg.GrantsRevocationURL, Interval: cfg.GrantsPoll}
	go core.Run(ctx, log)
	grants := &authz.GrantVerifier{Issuer: cfg.GrantsIssuer, JWKS: &jose.RemoteJWKS{URLs: httpx.List(cfg.GrantsJWKSURL)}, Revoked: core, Skew: 30 * time.Second}
	feed := NewFeed(cfg.StateDir, s3c, cfg.RevocationsBucket, cfg.RevocationsPrefix, cfg.RevocationsRetain, log)
	// WORKER-01: no payload codec in M1 (plan §1.3), so the Signal payload is plain json/plain by default.
	// RUNNER_TEMPORAL_PAYLOAD_CODEC=local-v1 enables the encode-only tenant codec with the file key.
	var codecKey []byte
	if httpx.Env("RUNNER_TEMPORAL_PAYLOAD_CODEC", "none") == "local-v1" {
		if k, err := httpx.ReadKey32(cfg.CodecKeyFile); err == nil {
			codecKey = k
		} else {
			return fmt.Errorf("temporal codec key: %w", err)
		}
	}
	creds := &Creds{Mode: cfg.CredentialsMode, STSEndpoint: cfg.S3STSEndpoint, Region: cfg.S3Region,
		RoleArn:   httpx.Env("RUNNER_S3_STS_ROLE_ARN", "arn:seaweed:iam::role/replicant-sandbox-artifacts"),
		AccessKey: ak, SecretKey: sk, Log: log}
	if cfg.SandboxCredsFile != "" {
		creds.StaticAK, creds.StaticSK, _, err = s3x.LoadINI(cfg.SandboxCredsFile, "")
		if err != nil {
			return fmt.Errorf("sandbox credentials: %w", err)
		}
	} else {
		creds.StaticAK, creds.StaticSK = ak, sk
	}
	creds.Probe = func(ctx context.Context, cr api.AwsContainerCredentials, allowedKey string) error {
		pc := s3x.New(s3x.Config{Endpoint: cfg.S3Endpoint, Region: cfg.S3Region, AccessKey: cr.AccessKeyId, SecretKey: cr.SecretAccessKey, SessionToken: cr.Token})
		b := s3x.Bucket(cfg.Org, cfg.Env, "artifacts")
		if _, err := pc.Put(ctx, b, allowedKey, []byte("ok"), s3x.PutOptions{}); err != nil {
			return fmt.Errorf("write inside the probe prefix failed: %w", err)
		}
		if _, err := pc.Put(ctx, b, "probe/other/x", []byte("x"), s3x.PutOptions{}); err == nil {
			return errors.New("session policy not enforced (write outside the probe prefix succeeded)")
		}
		return nil
	}
	creds.Init(ctx, cfg.Org, cfg.Env)

	pageKey := make([]byte, 32)
	_, _ = rand.Read(pageKey)
	s := &Server{
		Cfg: cfg, Log: log, Store: store, Signer: signer, Engine: eng, S3: s3c,
		Registry: &Registry{S3: s3c, Bucket: cfg.RegistryBucket}, Grants: grants, Core: core, Feed: feed,
		Kill: &KillSwitches{Source: cfg.KillSwitchMirror, S3: s3c}, Creds: creds, Inbox: &Inbox{S3: s3c},
		Notifier: &Notifier{Address: cfg.TemporalAddress, Namespace: cfg.TemporalNamespace, CodecKey: codecKey, Log: log},
		workers:  workers, readers: readers, pageKey: pageKey, wake: make(chan string, 256),
	}
	s.Tokens = &authz.TokenVerifier{
		Audience: api.AudSandbox, Issuers: []string{cfg.Issuer},
		JWKS:   &jose.RemoteJWKS{Static: []jose.JWK{signer.PublicJWK()}},
		Grants: grants, Revoked: revoke.Multi{core},
	}
	s.SELinux = eng.SELinuxEnabled(ctx)
	log.Info("selinux labels per pod group", "enabled", s.SELinux)
	go s.runExecutor(ctx)

	orch := &http.Server{Addr: cfg.OrchestrationListen, Handler: s.OrchestrationHandler(), ReadHeaderTimeout: 10 * time.Second}
	sbx := &http.Server{Addr: cfg.SandboxListen, Handler: s.SandboxHandler(), ReadHeaderTimeout: 10 * time.Second}
	errc := make(chan error, 2)
	go func() { errc <- orch.ListenAndServe() }()
	go func() { errc <- sbx.ListenAndServe() }()
	log.Info("runner-local listening", "orchestration", cfg.OrchestrationListen, "sandbox", cfg.SandboxListen,
		"sandbox_network", cfg.SandboxNetwork, "credentials_mode", creds.Mode, "issuer", cfg.Issuer)
	select {
	case <-ctx.Done():
	case err := <-errc:
		return err
	}
	shCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = orch.Shutdown(shCtx)
	_ = sbx.Shutdown(shCtx)
	return nil
}

// detectStateVolume inspects runner-local's own container to find the named volume behind the state dir.
func detectStateVolume(ctx context.Context, eng *engine.Client, stateDir string) string {
	host, _ := os.Hostname()
	ins, err := eng.InspectContainer(ctx, host)
	if err != nil || ins == nil {
		return ""
	}
	for _, m := range ins.Mounts {
		if filepath.Clean(m.Destination) == filepath.Clean(stateDir) && m.Name != "" {
			return m.Name
		}
	}
	return ""
}

func touch(dir string) error {
	p := filepath.Join(dir, ".ready")
	if err := os.WriteFile(p, []byte(time.Now().UTC().Format(time.RFC3339)), 0o600); err != nil {
		return err
	}
	return nil
}
