package main

import (
	"strconv"
	"strings"
	"time"
)

func atoiDef(s string, d int) int {
	if i, err := strconv.Atoi(strings.TrimSpace(s)); err == nil {
		return i
	}
	return d
}

func atofDef(s string, d float64) float64 {
	if f, err := strconv.ParseFloat(strings.TrimSpace(s), 64); err == nil {
		return f
	}
	return d
}

func fmtFloat(f float64) string { return strconv.FormatFloat(f, 'f', 6, 64) }

func parseDur(s string, d time.Duration) time.Duration {
	if v, err := time.ParseDuration(strings.TrimSpace(s)); err == nil {
		return v
	}
	return d
}

// parseBytes parses Kubernetes-style quantities (Ki, Mi, Gi, Ti, and plain bytes).
func parseBytes(s string) int64 {
	s = strings.TrimSpace(s)
	mult := int64(1)
	for _, u := range []struct {
		suf string
		m   int64
	}{{"Ki", 1 << 10}, {"Mi", 1 << 20}, {"Gi", 1 << 30}, {"Ti", 1 << 40}, {"K", 1000}, {"M", 1000 * 1000}, {"G", 1000 * 1000 * 1000}} {
		if strings.HasSuffix(s, u.suf) {
			s, mult = strings.TrimSuffix(s, u.suf), u.m
			break
		}
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return 0
	}
	return int64(f * float64(mult))
}

// parseCPU parses "2750m" or "6.5" into cores.
func parseCPU(s string) float64 {
	s = strings.TrimSpace(s)
	if strings.HasSuffix(s, "m") {
		return atofDef(strings.TrimSuffix(s, "m"), 0) / 1000
	}
	return atofDef(s, 0)
}

func ptr[T any](v T) *T { return &v }

func minI64(a, b int64) int64 {
	if a < b {
		return a
	}
	return b
}
