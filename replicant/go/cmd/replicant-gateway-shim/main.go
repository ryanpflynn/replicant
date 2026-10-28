// Command replicant-gateway-shim is Replicant's AI gateway shim (WS-03 gateway.md, gateway-interface.openapi.yaml).
//
//	replicant-gateway-shim --config=<file> --upstream-backend=<enterprise|passthrough|script> [--replay-match=sequence] [--state=local]
//	replicant-gateway-shim healthcheck --url http://127.0.0.1:8420/readyz
//	replicant-gateway-shim devtool mint --dir <dir> ...   (local tests only: dev keys, JWKS files and a run token)
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"example.com/replicant/cmd/replicant-gateway-shim/internal/shim"
	"example.com/replicant/cmd/replicant-gateway-shim/internal/usage"
)

func main() {
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case "healthcheck":
			os.Exit(healthcheck(os.Args[2:]))
		case "devtool":
			os.Exit(devtool(os.Args[2:]))
		}
	}
	os.Exit(serve(os.Args[1:]))
}

func healthcheck(args []string) int {
	fs := flag.NewFlagSet("healthcheck", flag.ContinueOnError)
	url := fs.String("url", "http://127.0.0.1:8420/readyz", "readiness URL")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	c := &http.Client{Timeout: 3 * time.Second}
	resp, err := c.Get(*url)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	resp.Body.Close()
	if resp.StatusCode != 200 {
		fmt.Fprintln(os.Stderr, "status", resp.StatusCode)
		return 1
	}
	return 0
}

func serve(args []string) int {
	fs := flag.NewFlagSet("replicant-gateway-shim", flag.ContinueOnError)
	cfgPath := fs.String("config", "/etc/gateway-shim/gateway-shim.yaml", "gateway-shim.yaml")
	backend := fs.String("upstream-backend", "enterprise", "enterprise | passthrough | record | replay | script")
	replay := fs.String("replay-match", "sequence", "sequence | content (replay backend)")
	state := fs.String("state", "local", "local (v0) | valkey")
	listen := fs.String("listen", "", "override `listen` from the config")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	log := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: levelFromEnv()}))
	cfg, err := shim.LoadConfig(*cfgPath, *backend, *replay, *state)
	if err != nil {
		log.Error("config", "err", err.Error())
		return 2
	}
	if *listen != "" {
		cfg.File.Listen = *listen
	}

	st := shim.NewMemState()
	var be shim.Backend
	switch cfg.Backend {
	case "script":
		be = shim.NewScriptBackend(cfg, st)
	case "passthrough":
		be = shim.NewPassthroughBackend(cfg)
	case "enterprise":
		// The enterprise gateway speaks the provider protocols (E-1); v0 reuses the passthrough adapter with the
		// configured route upstreams and a service credential from the same *_API_KEY(_FILE) variables.
		be = shim.NewPassthroughBackend(cfg)
	}

	ulog, err := buildUsageLog(cfg, log)
	if err != nil {
		log.Error("usage log", "err", err.Error())
		return 2
	}
	feeds := shim.NewFeeds(cfg, st, log)
	srv := &shim.Server{Cfg: cfg, State: st, Auth: shim.NewAuthenticator(cfg), Quotas: shim.NewQuotaClient(cfg), Backend: be, Usage: ulog, Feeds: feeds, Log: log}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	ulogDone := make(chan struct{})
	go func() { ulog.Run(ctx); close(ulogDone) }()
	feeds.Run(ctx)
	srv.RunBackground(ctx)

	hs := &http.Server{Addr: cfg.File.Listen, Handler: srv.Handler(), ReadHeaderTimeout: 30 * time.Second}
	go func() {
		<-ctx.Done()
		sctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		_ = hs.Shutdown(sctx)
	}()
	log.Info("replicant-gateway-shim listening", "listen", cfg.File.Listen, "backend", cfg.Backend, "state", cfg.StateMode,
		"run_token_issuers", len(cfg.RunTokenIssuers), "grant_jwks", cfg.GrantJWKSURL != "", "usage_log", describeUsage(cfg),
		"script_roots", strings.Join(keys(cfg.CassetteRoots), ","), "quota_source", quotaSource(cfg))
	if err := hs.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Error("listen", "err", err.Error())
		return 1
	}
	<-ulogDone
	return 0
}

func levelFromEnv() slog.Level {
	switch strings.ToLower(os.Getenv("REPLICANT_LOG_LEVEL")) {
	case "debug":
		return slog.LevelDebug
	case "warn":
		return slog.LevelWarn
	}
	return slog.LevelInfo
}

func keys(m map[string]string) []string {
	var out []string
	for k := range m {
		out = append(out, k)
	}
	return out
}

func quotaSource(c *shim.Config) string {
	switch {
	case c.ControlPlaneURL != "" && c.LocalQuota != nil:
		return "control-plane-api (local fallback configured)"
	case c.ControlPlaneURL != "":
		return "control-plane-api"
	case c.LocalQuota != nil:
		return "local"
	}
	return "none (every tenant is tenant_quota_unset)"
}

func describeUsage(c *shim.Config) string {
	if strings.HasPrefix(c.UsageS3URI, "s3://") || strings.HasPrefix(c.UsageS3URI, "file://") {
		return c.UsageS3URI
	}
	if c.UsageLogDir != "" {
		return "file://" + c.UsageLogDir
	}
	return "file:///var/lib/gateway-shim/usage"
}

// buildUsageLog selects the durable usage log sink: S3 (REPLICANT_GATEWAY_USAGE_S3_URI) or a local directory
// (REPLICANT_GATEWAY_USAGE_LOG_DIR, or a file:// URI in the S3 variable).
func buildUsageLog(cfg *shim.Config, log *slog.Logger) (*usage.Log, error) {
	var sink usage.Sink
	prefix := "platform/gateway-usage/"
	switch {
	case strings.HasPrefix(cfg.UsageS3URI, "s3://"):
		bucket, p, err := usage.ParseS3URI(cfg.UsageS3URI)
		if err != nil {
			return nil, err
		}
		prefix = p
		region := os.Getenv("AWS_REGION")
		if region == "" {
			region = "us-east-1"
		}
		sink = &usage.S3Sink{Endpoint: cfg.S3Endpoint, Bucket: bucket, Region: region, PathStyle: cfg.S3PathStyle,
			SSEKMSKeyID: os.Getenv("REPLICANT_GATEWAY_USAGE_SSE_KMS_KEY_ID"), Creds: usage.CredentialsFromEnv}
	case strings.HasPrefix(cfg.UsageS3URI, "file://"):
		sink = usage.FileSink{Dir: strings.TrimPrefix(cfg.UsageS3URI, "file://")}
	default:
		dir := cfg.UsageLogDir
		if dir == "" {
			dir = "/var/lib/gateway-shim/usage"
		}
		sink = usage.FileSink{Dir: dir}
	}
	return usage.NewLog(usage.Options{Sink: sink, Prefix: prefix, Pod: cfg.PodName, IngestURL: cfg.UsageURL,
		IngestTokenFile: cfg.UsageTokenFile, Logger: log}), nil
}
