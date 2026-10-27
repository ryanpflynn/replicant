package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/klauspost/compress/zstd"

	"example.com/replicant/internal/runner/httpx"
	"example.com/replicant/internal/runner/ipc"
	"example.com/replicant/internal/runner/s3x"
	"example.com/replicant/internal/runner/sandbox"
)

// ipcCreds is an aws.CredentialsProvider backed by agentd's uploader-ipc socket (credential_process JSON).
type ipcCreds struct{ c *ipc.Client }

func (p ipcCreds) Retrieve(ctx context.Context) (aws.Credentials, error) {
	var cp ipc.AWSCredentialProcess
	if err := p.c.Do(ctx, http.MethodGet, "/v1/aws-credentials", nil, nil, &cp); err != nil {
		return aws.Credentials{}, err
	}
	return aws.Credentials{AccessKeyID: cp.AccessKeyID, SecretAccessKey: cp.SecretAccessKey, SessionToken: cp.SessionToken,
		CanExpire: true, Expires: cp.Expiration.Add(-5 * time.Minute), Source: "replicant-agentd"}, nil
}

type uploader struct {
	log      *slog.Logger
	bs       *sandbox.Bootstrap
	s3       *s3x.Client
	agentd   *ipc.Client
	prefixes map[string][2]string // class -> bucket, key prefix
	tenant   string

	mu        sync.Mutex
	artifacts []manifestArtifact
	shipped   map[string]bool // spool file path -> uploaded
	chunkSeq  map[string]int
	truncated map[string]bool
	findings  int
	bytes     int64
	liveSize  map[string]int64 // open spool part -> size last shipped as a provisional copy
}

type manifestArtifact struct {
	Type            string         `json:"type"`
	BucketClass     string         `json:"bucket_class"`
	Key             string         `json:"key"`
	ContentType     string         `json:"content_type"`
	ContentEncoding string         `json:"content_encoding,omitempty"`
	SizeBytes       int64          `json:"size_bytes"`
	SHA256          string         `json:"sha256"`
	Redacted        *bool          `json:"redacted,omitempty"`
	Truncated       bool           `json:"truncated,omitempty"`
	Name            string         `json:"name,omitempty"`
	Attributes      map[string]any `json:"attributes,omitempty"`
}

func uploaderMain() int {
	log := httpx.Logger("uploader")
	bs, err := sandbox.ReadBootstrap("")
	if err != nil || bs.WS02 == nil {
		log.Error("bootstrap.json unreadable", "err", err)
		return 20
	}
	u := &uploader{log: log, bs: bs, agentd: ipc.New(httpx.Env("REPLICANT_UPLOADER_SOCKET", sandbox.UploaderSocket)),
		prefixes: map[string][2]string{}, tenant: bs.WS02.TenantID, shipped: map[string]bool{}, chunkSeq: map[string]int{}, truncated: map[string]bool{}}
	for class, uri := range map[string]string{"artifacts": bs.WS02.ArtifactPrefixes.Artifacts, "transcripts": bs.WS02.ArtifactPrefixes.Transcripts, "session": bs.WS02.ArtifactPrefixes.Session} {
		b, k, err := s3x.ParseURI(uri)
		if err != nil {
			log.Error("bad prefix", "class", class, "err", err)
			return 20
		}
		u.prefixes[class] = [2]string{b, k}
	}
	endpoint, region, sendSum := "", "us-east-1", false
	if bs.Local != nil {
		endpoint, sendSum = bs.Local.S3Endpoint, bs.Local.SendChecksum
		if bs.Local.S3Region != "" {
			region = bs.Local.S3Region
		}
	}
	u.s3 = s3x.New(s3x.Config{Endpoint: endpoint, Region: region, Provider: ipcCreds{u.agentd}, SendChecksum: sendSum})

	term := make(chan os.Signal, 1)
	signal.Notify(term, syscall.SIGTERM, syscall.SIGINT)
	ctx := context.Background()
	finalizeAfter := time.Duration(max(bs.WS02.Timeouts.FinalizeSeconds-30, 15)) * time.Second
	tick := time.NewTicker(10 * time.Second)
	defer tick.Stop()
	liveTick := time.NewTicker(3 * time.Second)
	defer liveTick.Stop()
	sigterm := false
	var termAt time.Time
	for {
		if exists(sandbox.MarkerFinished) || exists(sandbox.MarkerAgentExit) {
			break
		}
		if sigterm && time.Since(termAt) > min(finalizeAfter, 60*time.Second) {
			log.Warn("agent did not report exit after SIGTERM; finalizing")
			break
		}
		select {
		case <-term:
			if !sigterm {
				sigterm, termAt = true, time.Now()
				log.Info("SIGTERM: finalize now")
			}
		case <-tick.C:
			u.shipSpool(ctx, false)
			u.shipCheckpoints(ctx)
		case <-liveTick.C:
			u.shipLive(ctx)
		case <-time.After(time.Second):
		}
	}
	return u.finalize(ctx)
}

func exists(p string) bool { _, err := os.Stat(p); return err == nil }

// redactKnown passes text through agentd's known-credential redaction (controller.md §9a stage 1).
func (u *uploader) redactKnown(ctx context.Context, text []byte) []byte {
	if len(text) == 0 || len(text) > 8<<20 {
		return text
	}
	var out struct {
		Text string `json:"text"`
		Hits int    `json:"hits"`
	}
	if err := u.agentd.Do(ctx, http.MethodPost, "/v1/redact-known", nil, map[string]string{"text": string(text)}, &out); err != nil {
		u.log.Warn("redact-known failed; redacting fail-closed", "err", err)
		return []byte(fmt.Sprintf("[REDACTION_FAILED:%d]", len(text)))
	}
	u.mu.Lock()
	u.findings += out.Hits
	u.mu.Unlock()
	return []byte(out.Text)
}

func (u *uploader) put(ctx context.Context, class, typ, name string, body []byte, contentType, encoding string, redacted *bool, attrs map[string]any, truncated bool) error {
	pk, ok := u.prefixes[class]
	if !ok {
		return fmt.Errorf("unknown bucket class %s", class)
	}
	t := sandbox.Types[typ]
	key := pk[1] + typ + "/" + name
	var err error
	for attempt := 0; attempt < 8; attempt++ {
		_, err = u.s3.Put(ctx, pk[0], key, body, s3x.PutOptions{ContentType: contentType, ContentEncoding: encoding,
			Tagging: s3x.Tagging(t.Retention, typ, u.tenant)})
		if err == nil {
			break
		}
		time.Sleep(time.Duration(200*(1<<attempt)) * time.Millisecond)
	}
	if err != nil {
		return err
	}
	if u.bs.Local != nil && u.bs.Local.VerifyReadback {
		got, _, gerr := u.s3.Get(ctx, pk[0], key)
		if gerr == nil && s3x.Sha256Hex(got) != s3x.Sha256Hex(body) {
			return fmt.Errorf("read-back digest mismatch for %s", key)
		}
	}
	u.mu.Lock()
	defer u.mu.Unlock()
	u.artifacts = append(u.artifacts, manifestArtifact{Type: typ, BucketClass: class, Key: key, ContentType: contentType,
		ContentEncoding: encoding, SizeBytes: int64(len(body)), SHA256: s3x.Sha256Hex(body), Redacted: redacted,
		Truncated: truncated, Attributes: attrs})
	u.bytes += int64(len(body))
	return nil
}

var zenc, _ = zstd.NewWriter(nil, zstd.WithEncoderLevel(zstd.SpeedDefault))

// shipSpool uploads spool chunks (C-ART §5.1: ≤ 30 s / 8 MiB). A chunk is final once a later part exists,
// or at finalize.
func (u *uploader) shipSpool(ctx context.Context, final bool) {
	for _, typ := range []string{"cli_events", "transcript", "sandbox_log"} {
		dir := filepath.Join(sandbox.SpoolDir, typ)
		ents, err := os.ReadDir(dir)
		if err != nil {
			continue
		}
		var parts []string
		for _, e := range ents {
			if strings.HasPrefix(e.Name(), "part-") && strings.HasSuffix(e.Name(), ".ndjson") {
				parts = append(parts, e.Name())
			}
		}
		sort.Strings(parts)
		for i, p := range parts {
			full := filepath.Join(dir, p)
			u.mu.Lock()
			done := u.shipped[full]
			u.mu.Unlock()
			if done || (!final && i == len(parts)-1) {
				continue
			}
			b, err := os.ReadFile(full)
			if err != nil || len(b) == 0 {
				continue
			}
			b = u.redactKnown(ctx, b)
			z := zenc.EncodeAll(b, nil)
			name := strings.TrimSuffix(p, ".ndjson") + ".ndjson.zst"
			ct := "application/x-ndjson"
			if typ == "sandbox_log" {
				ct = "text/plain"
			}
			t := true
			if err := u.put(ctx, "transcripts", typ, name, z, ct, "zstd", &t, nil, false); err != nil {
				u.log.Warn("spool chunk upload failed", "file", full, "err", err)
				continue
			}
			u.mu.Lock()
			u.shipped[full] = true
			u.mu.Unlock()
		}
	}
}

// shipLive uploads a provisional copy of each spool type's open part to `<type>/live/<part>.ndjson.zst` whenever it
// grew, so the management UI can follow a running agent (GRAPH-D, docs/v0/DEVIATIONS.md). The copy is redacted like
// a final chunk, is not listed in the manifest, and is superseded by the final part of the same name; readers use it
// only while no final part with that name exists.
func (u *uploader) shipLive(ctx context.Context) {
	pk, ok := u.prefixes["transcripts"]
	if !ok {
		return
	}
	for _, typ := range []string{"cli_events", "transcript", "sandbox_log"} {
		dir := filepath.Join(sandbox.SpoolDir, typ)
		ents, err := os.ReadDir(dir)
		if err != nil {
			continue
		}
		var parts []string
		for _, e := range ents {
			if strings.HasPrefix(e.Name(), "part-") && strings.HasSuffix(e.Name(), ".ndjson") {
				parts = append(parts, e.Name())
			}
		}
		if len(parts) == 0 {
			continue
		}
		sort.Strings(parts)
		p := parts[len(parts)-1]
		full := filepath.Join(dir, p)
		st, err := os.Stat(full)
		u.mu.Lock()
		done, prev := u.shipped[full], u.liveSize[full]
		u.mu.Unlock()
		if err != nil || done || st.Size() == 0 || st.Size() == prev {
			continue
		}
		b, err := os.ReadFile(full)
		if err != nil {
			continue
		}
		if i := bytes.LastIndexByte(b, '\n'); i >= 0 { // whole lines only
			b = b[:i+1]
		} else {
			continue
		}
		z := zenc.EncodeAll(u.redactKnown(ctx, b), nil)
		ct := "application/x-ndjson"
		if typ == "sandbox_log" {
			ct = "text/plain"
		}
		key := pk[1] + typ + "/live/" + strings.TrimSuffix(p, ".ndjson") + ".ndjson.zst"
		if _, err := u.s3.Put(ctx, pk[0], key, z, s3x.PutOptions{ContentType: ct, ContentEncoding: "zstd",
			Tagging: s3x.Tagging(sandbox.Types[typ].Retention, typ, u.tenant)}); err != nil {
			u.log.Debug("live chunk upload failed", "file", full, "err", err)
			continue
		}
		u.mu.Lock()
		if u.liveSize == nil {
			u.liveSize = map[string]int64{}
		}
		u.liveSize[full] = st.Size()
		u.mu.Unlock()
	}
}

// shipCheckpoints uploads QA checkpoints as they appear (controller.md §4.2a).
func (u *uploader) shipCheckpoints(ctx context.Context) {
	dir := filepath.Join(sandbox.OutDir, "qa-checkpoints")
	ents, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	for _, e := range ents {
		full := filepath.Join(dir, e.Name())
		u.mu.Lock()
		done := u.shipped[full]
		u.mu.Unlock()
		if done || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		b, err := os.ReadFile(full)
		if err != nil {
			continue
		}
		t := true
		if err := u.put(ctx, "artifacts", "qa_report", "checkpoints/"+e.Name(), u.redactKnown(ctx, b), "application/json", "", &t, nil, false); err == nil {
			u.mu.Lock()
			u.shipped[full] = true
			u.mu.Unlock()
		}
	}
}

// finalize uploads the outputs index entries, then writes manifest.json last (If-None-Match: *).
func (u *uploader) finalize(ctx context.Context) int {
	u.shipSpool(ctx, true)
	u.shipCheckpoints(ctx)
	role := u.bs.WS02.Role
	partial := false
	var outs sandbox.OutputsIndex
	var resultArt *manifestArtifact
	exitCode := 0
	b, err := os.ReadFile(filepath.Join(sandbox.OutDir, "outputs.json"))
	if err != nil || json.Unmarshal(b, &outs) != nil || outs.Schema != "replicant.run-outputs/v1" {
		u.log.Warn("outputs.json missing or invalid: partial manifest with streamed chunks only", "err", err)
		partial = true
	} else {
		exitCode = outs.ExitCode
		partial = outs.Partial
		var total int64
		for _, e := range outs.Entries {
			if err := u.uploadEntry(ctx, role, e, &total); err != nil {
				u.log.Warn("output upload failed", "type", e.Type, "path", e.Path, "err", err)
				if e.Required || isRequired(u.bs.WS02.RequiredOutputs, e.Type) {
					partial = true
				}
				continue
			}
		}
	}
	u.mu.Lock()
	var arts []manifestArtifact
	for i := range u.artifacts {
		a := u.artifacts[i]
		if a.Type == "run_result" {
			resultArt = &a
			continue
		}
		arts = append(arts, a)
	}
	var truncTypes []string
	for t := range u.truncated {
		truncTypes = append(truncTypes, t)
	}
	sort.Strings(truncTypes)
	findings, totalBytes := u.findings, u.bytes
	u.mu.Unlock()
	if arts == nil {
		arts = []manifestArtifact{}
	}
	if truncTypes == nil {
		truncTypes = []string{}
	}
	m := map[string]any{
		"schema":    "replicant.run-manifest/v1",
		"tenant_id": u.tenant,
		"job_id":    u.bs.WS02.JobID,
		"producer": map[string]any{"kind": "run", "run_id": u.bs.RunID, "agentrun": u.bs.WS02.AgentRun, "role": role,
			"release_bundle_id": u.bs.WS02.ReleaseBundleID, "exit_code": exitCode},
		"created_at": time.Now().UTC().Format(time.RFC3339),
		"partial":    partial,
		"artifacts":  arts,
		"redaction":  map[string]any{"ruleset_version": u.bs.WS02.Redaction.RulesetVersion, "findings_count": findings},
		"totals":     map[string]any{"objects": len(arts) + boolInt(resultArt != nil), "bytes": totalBytes, "truncated_types": truncTypes},
	}
	if p, ok := m["producer"].(map[string]any); ok {
		if t := readMarkerTime(sandbox.MarkerAgentStarted); t != "" {
			p["agent_started_at"] = t
		}
		if t := readMarkerTime(sandbox.MarkerAgentExit); t != "" {
			p["agent_exited_at"] = t
		}
	}
	if resultArt != nil {
		m["result"] = resultArt
	}
	body, _ := json.MarshalIndent(m, "", "  ")
	pk := u.prefixes["artifacts"]
	err = u.s3.PutWriteOnceIdempotent(ctx, pk[0], pk[1]+"manifest.json", body, s3x.PutOptions{ContentType: "application/json",
		Tagging: s3x.Tagging("r2y", "run_manifest", u.tenant)})
	if err != nil {
		u.log.Error("manifest write failed", "err", err)
		return 20
	}
	_ = u.agentd.Do(ctx, http.MethodPost, "/v1/manifest-written", nil, map[string]any{}, nil)
	u.log.Info("manifest committed", "partial", partial, "objects", len(arts), "bytes", totalBytes)
	if partial {
		return 10
	}
	return 0
}

func readMarkerTime(p string) string {
	b, err := os.ReadFile(p)
	if err != nil {
		return ""
	}
	s := strings.TrimSpace(string(b))
	if _, err := time.Parse(time.RFC3339, s); err == nil {
		return s
	}
	if fi, err := os.Stat(p); err == nil {
		return fi.ModTime().UTC().Format(time.RFC3339)
	}
	return ""
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

func isRequired(req []string, typ string) bool {
	for _, r := range req {
		if r == typ {
			return true
		}
	}
	return false
}

var errRejected = errors.New("rejected")

func (u *uploader) uploadEntry(ctx context.Context, role string, e sandbox.OutputEntry, total *int64) error {
	t, ok := sandbox.Types[e.Type]
	if !ok {
		return fmt.Errorf("%w: unregistered type %s", errRejected, e.Type)
	}
	if e.Type == "diff_bundle" && (role == "reviewer" || role == "planner") {
		return fmt.Errorf("%w: diff_bundle from a %s run (D-21)", errRejected, role)
	}
	if e.BucketClass != t.BucketClass {
		return fmt.Errorf("%w: %s belongs in %s", errRejected, e.Type, t.BucketClass)
	}
	clean := filepath.Clean(e.Path)
	if !strings.HasPrefix(clean, sandbox.OutDir+"/") {
		return fmt.Errorf("%w: path outside /replicant/out", errRejected)
	}
	fi, err := os.Lstat(clean)
	if err != nil || !fi.Mode().IsRegular() {
		return fmt.Errorf("%w: not a regular file: %v", errRejected, err)
	}
	b, err := os.ReadFile(clean)
	if err != nil {
		return err
	}
	truncated := false
	if int64(len(b)) > t.MaxBytes {
		if !t.Text {
			return fmt.Errorf("%w: %s exceeds %d bytes", errRejected, e.Type, t.MaxBytes)
		}
		b, truncated = b[:t.MaxBytes], true
		u.mu.Lock()
		u.truncated[e.Type] = true
		u.mu.Unlock()
	}
	if max := u.bs.WS02.MaxOutputBytes; max > 0 && *total+int64(len(b)) > max {
		return fmt.Errorf("%w: max_output_bytes exceeded", errRejected)
	}
	*total += int64(len(b))
	var redacted *bool
	switch e.Redaction {
	case "required":
		if e.ContentEncoding == "" || e.ContentEncoding == "identity" {
			b = u.redactKnown(ctx, b)
			v := true
			redacted = &v
		}
	case "raw-restricted":
		v := false
		redacted = &v
	}
	if e.SHA256 != "" && e.Redaction != "required" && s3x.Sha256Hex(b) != e.SHA256 && !truncated {
		return fmt.Errorf("digest mismatch for %s", e.Path)
	}
	return u.put(ctx, e.BucketClass, e.Type, e.Name, b, e.ContentType, e.ContentEncoding, redacted, e.Attributes, truncated)
}
