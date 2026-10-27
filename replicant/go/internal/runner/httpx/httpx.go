// Package httpx holds small helpers shared by the replicant Go services: env and secret-file loading,
// JSON logging, bearer-token parsing and a readiness/health command.
package httpx

import (
	"context"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"sigs.k8s.io/yaml"
)

// Env returns the variable or def.
func Env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// EnvInt returns an integer variable or def.
func EnvInt(k string, def int) int {
	if v := os.Getenv(k); v != "" {
		if i, err := strconv.Atoi(v); err == nil {
			return i
		}
	}
	return def
}

// EnvBool returns a boolean variable or def.
func EnvBool(k string, def bool) bool {
	if v := os.Getenv(k); v != "" {
		b, err := strconv.ParseBool(v)
		if err == nil {
			return b
		}
	}
	return def
}

// List splits a comma-separated variable.
func List(v string) []string {
	var out []string
	for _, p := range strings.Split(v, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// ReadToken reads a single-line secret file (trimmed). Values are never logged.
func ReadToken(path string) (string, error) {
	if path == "" {
		return "", errors.New("no token file configured")
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(b)), nil
}

// ReadTokenMap reads a `token: subject` YAML map, or `token subject` lines.
func ReadTokenMap(path string) (map[string]string, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	m := map[string]string{}
	if err := yaml.Unmarshal(b, &m); err == nil && len(m) > 0 {
		out := map[string]string{}
		for k, v := range m {
			out[strings.TrimSpace(k)] = strings.TrimSpace(v)
		}
		return out, nil
	}
	m = map[string]string{}
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		f := strings.Fields(line)
		if len(f) == 1 {
			m[f[0]] = f[0]
		} else {
			m[f[0]] = f[len(f)-1]
		}
	}
	return m, nil
}

// ReadKey32 reads a 32-byte key file: raw bytes, hex or base64 text.
func ReadKey32(path string) ([]byte, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	if len(b) == 32 {
		return b, nil
	}
	s := strings.TrimSpace(string(b))
	if k, err := hex.DecodeString(s); err == nil && len(k) == 32 {
		return k, nil
	}
	for _, enc := range []*base64.Encoding{base64.StdEncoding, base64.RawStdEncoding, base64.URLEncoding, base64.RawURLEncoding} {
		if k, err := enc.DecodeString(s); err == nil && len(k) == 32 {
			return k, nil
		}
	}
	return nil, fmt.Errorf("%s: not a 32-byte key", path)
}

// Bearer extracts a bearer token.
func Bearer(r *http.Request) string {
	h := r.Header.Get("Authorization")
	if len(h) > 7 && strings.EqualFold(h[:7], "bearer ") {
		return strings.TrimSpace(h[7:])
	}
	return ""
}

// Logger returns a JSON slog logger with service set.
func Logger(service string) *slog.Logger {
	lvl := slog.LevelInfo
	if os.Getenv("REPLICANT_LOG_LEVEL") == "debug" {
		lvl = slog.LevelDebug
	}
	return slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: lvl})).With("service", service)
}

// Healthcheck implements `<bin> healthcheck --url <u>`: exit 0 on 200 within 2 s.
func Healthcheck(args []string) int {
	u := ""
	for i := 0; i < len(args); i++ {
		if args[i] == "--url" && i+1 < len(args) {
			u = args[i+1]
		} else if strings.HasPrefix(args[i], "--url=") {
			u = strings.TrimPrefix(args[i], "--url=")
		}
	}
	if u == "" {
		fmt.Fprintln(os.Stderr, "healthcheck: --url required")
		return 2
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	resp, err := http.DefaultClient.Do(req)
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
