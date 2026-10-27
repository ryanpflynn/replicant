package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"time"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/s3x"
)

// Feed is the exec revocation feed (C-RUNNER 3.0.0 GET /v1/revocations, runner-service.md §7.3):
// an in-memory window backed by a local append log and hourly ndjson objects in the ops bucket.
type Feed struct {
	path   string
	s3     *s3x.Client
	bucket string
	prefix string
	retain time.Duration
	log    *slog.Logger

	mu      sync.Mutex
	entries []feedEntry
	byRun   map[string]bool
	seq     int64
}

type feedEntry struct {
	Seq int64 `json:"seq"`
	api.Revocation
}

func NewFeed(stateDir string, s3 *s3x.Client, bucket, prefix string, retain time.Duration, log *slog.Logger) *Feed {
	f := &Feed{path: filepath.Join(stateDir, "revocations.ndjson"), s3: s3, bucket: bucket, prefix: prefix, retain: retain, log: log, byRun: map[string]bool{}}
	if b, err := os.ReadFile(f.path); err == nil {
		sc := bufio.NewScanner(bytes.NewReader(b))
		for sc.Scan() {
			var e feedEntry
			if json.Unmarshal(sc.Bytes(), &e) == nil {
				f.entries = append(f.entries, e)
				f.byRun[e.RunID] = true
				if e.Seq > f.seq {
					f.seq = e.Seq
				}
			}
		}
	}
	return f
}

// Add appends a revocation (idempotent per run).
func (f *Feed) Add(ctx context.Context, runID, jobID, reason string) {
	f.mu.Lock()
	if f.byRun[runID] {
		f.mu.Unlock()
		return
	}
	f.seq++
	e := feedEntry{Seq: f.seq, Revocation: api.Revocation{RunID: runID, JobID: jobID, At: time.Now().UTC(), Reason: reason}}
	f.entries = append(f.entries, e)
	f.byRun[runID] = true
	line, _ := json.Marshal(e)
	if fh, err := os.OpenFile(f.path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600); err == nil {
		fh.Write(append(line, '\n'))
		fh.Close()
	}
	hour := e.At.Format("2006010215")
	var buf bytes.Buffer
	for _, x := range f.entries {
		if x.At.Format("2006010215") == hour {
			b, _ := json.Marshal(x.Revocation)
			buf.Write(append(b, '\n'))
		}
	}
	f.mu.Unlock()
	if f.s3 != nil && f.bucket != "" {
		key := f.prefix + hour + ".ndjson"
		if _, err := f.s3.Put(ctx, f.bucket, key, buf.Bytes(), s3x.PutOptions{ContentType: "application/x-ndjson",
			Tagging: s3x.Tagging("r90d", "exec_revocation_feed", "replicant-system")}); err != nil && f.log != nil {
			f.log.Warn("revocation feed object write failed", "key", key, "err", err)
		}
	}
}

// IsRevoked reports a run on the feed.
func (f *Feed) IsRevoked(runID string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.byRun[runID]
}

// Revoked implements revoke.Checker.
func (f *Feed) Revoked(_, runID string) (bool, string) {
	if f.IsRevoked(runID) {
		return true, "exec_feed"
	}
	return false, ""
}

// Since returns entries after the cursor (a decimal sequence number) within the retained window.
func (f *Feed) Since(cursor string) api.RevocationList {
	f.mu.Lock()
	defer f.mu.Unlock()
	after, _ := strconv.ParseInt(cursor, 10, 64)
	cut := time.Now().Add(-f.retain)
	out := api.RevocationList{Revoked: []api.Revocation{}, NextCursor: strconv.FormatInt(f.seq, 10)}
	for _, e := range f.entries {
		if e.Seq > after && e.At.After(cut) {
			out.Revoked = append(out.Revoked, e.Revocation)
		}
	}
	if cursor != "" && after > f.seq {
		out.NextCursor = cursor
	}
	return out
}
