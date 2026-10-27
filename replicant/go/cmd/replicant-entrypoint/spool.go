package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	"example.com/replicant/internal/runner/sandbox"
)

// spool writes /replicant/spool/{cli_events,transcript,sandbox_log}/part-<seq:05d>.ndjson, rotated every 30 s
// or 8 MiB (§2.7); the uploader ships closed parts as they appear.
type spool struct {
	mu     sync.Mutex
	files  map[string]*os.File
	seq    map[string]int
	size   map[string]int64
	opened map[string]time.Time
	tseq   int
	runID  string
}

func newSpool() *spool {
	return &spool{files: map[string]*os.File{}, seq: map[string]int{}, size: map[string]int64{}, opened: map[string]time.Time{},
		runID: os.Getenv("REPLICANT_RUN_ID")}
}

func (s *spool) write(typ string, b []byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	f := s.files[typ]
	if f != nil && (s.size[typ]+int64(len(b)) > 8<<20 || time.Since(s.opened[typ]) > 30*time.Second) {
		f.Close()
		f = nil
	}
	if f == nil {
		dir := filepath.Join(sandbox.SpoolDir, typ)
		_ = os.MkdirAll(dir, 0o755)
		s.seq[typ]++
		var err error
		f, err = os.OpenFile(filepath.Join(dir, fmt.Sprintf("part-%05d.ndjson", s.seq[typ])), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
		if err != nil {
			return
		}
		s.files[typ], s.size[typ], s.opened[typ] = f, 0, time.Now()
	}
	n, _ := f.Write(b)
	s.size[typ] += int64(n)
}

// transcript appends a replicant.transcript/v1 record.
func (s *spool) transcript(rec map[string]any) {
	s.mu.Lock()
	s.tseq++
	seq := s.tseq
	s.mu.Unlock()
	rec["schema"] = "replicant.transcript/v1"
	rec["seq"] = seq
	rec["ts"] = time.Now().UTC().Format("2006-01-02T15:04:05.000Z")
	rec["run_id"] = s.runID
	b, _ := json.Marshal(rec)
	s.write("transcript", append(b, '\n'))
}

func (s *spool) close() {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, f := range s.files {
		f.Close()
	}
	s.files = map[string]*os.File{}
}
