package usage

import (
	"bufio"
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync/atomic"
	"time"
)

// S3Sink PUTs objects with SigV4 (path-style or virtual-host), `If-None-Match: *` for write-once keys.
type S3Sink struct {
	Endpoint    string // e.g. http://s3.replicant.localhost:8333 ; empty = https://s3.<region>.amazonaws.com
	Bucket      string
	Region      string
	PathStyle   bool
	SSEKMSKeyID string // optional (prod: the platform key); empty locally
	Creds       func() (Credentials, error)
	Client      *http.Client

	noConditional atomic.Bool // set when the server rejects If-None-Match (some S3 implementations)
}

// Credentials are static AWS credentials.
type Credentials struct {
	AccessKeyID, SecretAccessKey, SessionToken string
}

// ParseS3URI splits s3://bucket/prefix.
func ParseS3URI(u string) (bucket, prefix string, err error) {
	rest, ok := strings.CutPrefix(u, "s3://")
	if !ok {
		return "", "", fmt.Errorf("not an s3:// URI: %q", u)
	}
	bucket, prefix, _ = strings.Cut(rest, "/")
	if bucket == "" {
		return "", "", fmt.Errorf("no bucket in %q", u)
	}
	return bucket, prefix, nil
}

// CredentialsFromEnv reads AWS_ACCESS_KEY_ID/… or the shared credentials file (profile AWS_PROFILE or default).
func CredentialsFromEnv() (Credentials, error) {
	if id := os.Getenv("AWS_ACCESS_KEY_ID"); id != "" {
		return Credentials{id, os.Getenv("AWS_SECRET_ACCESS_KEY"), os.Getenv("AWS_SESSION_TOKEN")}, nil
	}
	path := os.Getenv("AWS_SHARED_CREDENTIALS_FILE")
	if path == "" {
		home, _ := os.UserHomeDir()
		path = home + "/.aws/credentials"
	}
	profile := os.Getenv("AWS_PROFILE")
	if profile == "" {
		profile = "default"
	}
	f, err := os.Open(path)
	if err != nil {
		return Credentials{}, fmt.Errorf("aws credentials: %w", err)
	}
	defer f.Close()
	var c Credentials
	in := false
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if strings.HasPrefix(line, "[") {
			in = strings.Trim(line, "[] ") == profile
			continue
		}
		if !in {
			continue
		}
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		switch strings.TrimSpace(k) {
		case "aws_access_key_id":
			c.AccessKeyID = strings.TrimSpace(v)
		case "aws_secret_access_key":
			c.SecretAccessKey = strings.TrimSpace(v)
		case "aws_session_token":
			c.SessionToken = strings.TrimSpace(v)
		}
	}
	if c.AccessKeyID == "" {
		return c, fmt.Errorf("aws credentials: profile %q not found", profile)
	}
	return c, nil
}

// Describe names the sink (no secrets).
func (s *S3Sink) Describe() string { return "s3://" + s.Bucket }

func (s *S3Sink) objectURL(key string) (*url.URL, string) {
	ep := s.Endpoint
	if ep == "" {
		ep = "https://s3." + s.Region + ".amazonaws.com"
	}
	u, _ := url.Parse(ep)
	path := "/" + strings.TrimLeft(key, "/")
	if s.PathStyle || s.Endpoint != "" && !strings.Contains(u.Host, "amazonaws.com") {
		u.Path = "/" + s.Bucket + path
	} else {
		u.Host = s.Bucket + "." + u.Host
		u.Path = path
	}
	return u, u.Host
}

// Put uploads one object.
func (s *S3Sink) Put(ctx context.Context, key string, body []byte) error {
	err := s.put(ctx, key, body, !s.noConditional.Load())
	if err == errConditionalUnsupported {
		s.noConditional.Store(true)
		err = s.put(ctx, key, body, false)
	}
	return err
}

var errConditionalUnsupported = fmt.Errorf("conditional PUT unsupported")

func (s *S3Sink) put(ctx context.Context, key string, body []byte, conditional bool) error {
	creds, err := s.Creds()
	if err != nil {
		return err
	}
	u, host := s.objectURL(key)
	req, err := http.NewRequestWithContext(ctx, "PUT", u.String(), bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.ContentLength = int64(len(body))
	req.Header.Set("Content-Type", "application/x-ndjson")
	req.Header.Set("Content-Encoding", "zstd")
	if conditional {
		req.Header.Set("If-None-Match", "*")
	}
	// C-ART §5.3: retention/type/tenant tags at upload (gateway_usage_log: r7d, platform key replicant-system) and a
	// SHA-256 checksum. SSE-KMS headers are omitted locally (SeaweedFS; prod sets them from S3SSEKMSKeyID).
	req.Header.Set("x-amz-tagging", "replicant-retention=r7d&replicant-type=gateway_usage_log&replicant-tenant=replicant-system")
	sum := sha256.Sum256(body)
	req.Header.Set("x-amz-checksum-sha256", base64.StdEncoding.EncodeToString(sum[:]))
	if s.SSEKMSKeyID != "" {
		req.Header.Set("x-amz-server-side-encryption", "aws:kms")
		req.Header.Set("x-amz-server-side-encryption-aws-kms-key-id", s.SSEKMSKeyID)
		req.Header.Set("x-amz-server-side-encryption-bucket-key-enabled", "true")
	}
	SignV4(req, host, body, creds, s.Region, "s3", time.Now().UTC())
	client := s.Client
	if client == nil {
		client = &http.Client{Timeout: 15 * time.Second}
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	msg, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
	switch {
	case resp.StatusCode/100 == 2:
		return nil
	case conditional && (resp.StatusCode == 501 || resp.StatusCode == 400 && bytes.Contains(msg, []byte("NotImplemented"))):
		return errConditionalUnsupported
	case resp.StatusCode == 412:
		return fmt.Errorf("object %s already exists", key)
	}
	code := ""
	if i := bytes.Index(msg, []byte("<Code>")); i >= 0 {
		if j := bytes.Index(msg[i:], []byte("</Code>")); j > 0 {
			code = string(msg[i+6 : i+j])
		}
	}
	return fmt.Errorf("s3 put %s: http %d %s", key, resp.StatusCode, code)
}

func hmacSHA256(k []byte, s string) []byte {
	h := hmac.New(sha256.New, k)
	h.Write([]byte(s))
	return h.Sum(nil)
}

func uriEncode(s string, encodeSlash bool) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c >= 'A' && c <= 'Z' || c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '-' || c == '_' || c == '.' || c == '~' || c == '/' && !encodeSlash {
			b.WriteByte(c)
		} else {
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

// SignV4 adds AWS Signature Version 4 headers to req.
func SignV4(req *http.Request, host string, body []byte, c Credentials, region, service string, now time.Time) {
	amzDate := now.Format("20060102T150405Z")
	date := now.Format("20060102")
	ph := sha256.Sum256(body)
	payloadHash := hex.EncodeToString(ph[:])
	req.Header.Set("x-amz-date", amzDate)
	req.Header.Set("x-amz-content-sha256", payloadHash)
	if c.SessionToken != "" {
		req.Header.Set("x-amz-security-token", c.SessionToken)
	}
	req.Host = host
	names := []string{"host"}
	vals := map[string]string{"host": host}
	for k, v := range req.Header {
		lk := strings.ToLower(k)
		if lk == "content-type" || lk == "content-encoding" || lk == "if-none-match" || lk == "date" || strings.HasPrefix(lk, "x-amz-") {
			names = append(names, lk)
			vals[lk] = strings.TrimSpace(strings.Join(v, ","))
		}
	}
	sortStrings(names)
	var ch strings.Builder
	for _, n := range names {
		ch.WriteString(n + ":" + vals[n] + "\n")
	}
	signed := strings.Join(names, ";")
	canonical := strings.Join([]string{req.Method, canonicalPath(req.URL.Path), req.URL.RawQuery, ch.String(), signed, payloadHash}, "\n")
	scope := date + "/" + region + "/" + service + "/aws4_request"
	cr := sha256.Sum256([]byte(canonical))
	sts := "AWS4-HMAC-SHA256\n" + amzDate + "\n" + scope + "\n" + hex.EncodeToString(cr[:])
	k := hmacSHA256([]byte("AWS4"+c.SecretAccessKey), date)
	k = hmacSHA256(k, region)
	k = hmacSHA256(k, service)
	k = hmacSHA256(k, "aws4_request")
	sig := hex.EncodeToString(hmacSHA256(k, sts))
	req.Header.Set("Authorization", fmt.Sprintf("AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s", c.AccessKeyID, scope, signed, sig))
}

func canonicalPath(p string) string {
	if p == "" {
		return "/"
	}
	return uriEncode(p, false)
}

func sortStrings(a []string) {
	for i := 1; i < len(a); i++ {
		for j := i; j > 0 && a[j] < a[j-1]; j-- {
			a[j], a[j-1] = a[j-1], a[j]
		}
	}
}
