// Package s3x wraps the AWS SDK S3 client for Replicant's object-store contract (C-ART 2.0.0):
// logical↔physical references, write-once puts (If-None-Match: *), tags and checksums.
package s3x

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/s3/types"
	"github.com/aws/smithy-go"
)

// Config for a client.
type Config struct {
	Endpoint     string
	Region       string
	AccessKey    string
	SecretKey    string
	SessionToken string
	// Provider, when set, supplies (possibly refreshing) credentials instead of the static keys.
	Provider aws.CredentialsProvider
	// SendChecksum adds x-amz-checksum-sha256 to every PutObject (C-ART §5.3).
	SendChecksum bool
}

// Client is a thin wrapper.
type Client struct {
	S3           *s3.Client
	sendChecksum bool
}

// New builds a path-style client.
func New(c Config) *Client {
	region := c.Region
	if region == "" {
		region = "us-east-1"
	}
	prov := c.Provider
	if prov == nil {
		prov = credentials.NewStaticCredentialsProvider(c.AccessKey, c.SecretKey, c.SessionToken)
	}
	cl := s3.New(s3.Options{
		Region:                     region,
		Credentials:                aws.NewCredentialsCache(prov),
		BaseEndpoint:               aws.String(c.Endpoint),
		UsePathStyle:               true,
		RequestChecksumCalculation: aws.RequestChecksumCalculationWhenRequired,
		ResponseChecksumValidation: aws.ResponseChecksumValidationWhenRequired,
		RetryMaxAttempts:           5,
		HTTPClient:                 &http.Client{Timeout: 120 * time.Second},
	})
	return &Client{S3: cl, sendChecksum: c.SendChecksum}
}

// LoadINI reads an AWS shared-credentials file and returns the keys of profile (default "default").
func LoadINI(path, profile string) (ak, sk, st string, err error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return "", "", "", err
	}
	if profile == "" {
		profile = "default"
	}
	cur := ""
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, ";") {
			continue
		}
		if strings.HasPrefix(line, "[") && strings.HasSuffix(line, "]") {
			cur = strings.TrimSpace(strings.TrimPrefix(strings.Trim(line, "[]"), "profile "))
			continue
		}
		if cur != profile {
			continue
		}
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		switch strings.TrimSpace(k) {
		case "aws_access_key_id":
			ak = strings.TrimSpace(v)
		case "aws_secret_access_key":
			sk = strings.TrimSpace(v)
		case "aws_session_token":
			st = strings.TrimSpace(v)
		}
	}
	if ak == "" || sk == "" {
		return "", "", "", fmt.Errorf("no credentials for profile %q in %s", profile, path)
	}
	return ak, sk, st, nil
}

// ParseURI splits s3://bucket/key.
func ParseURI(u string) (bucket, key string, err error) {
	if !strings.HasPrefix(u, "s3://") {
		return "", "", fmt.Errorf("not an s3 uri: %q", u)
	}
	rest := strings.TrimPrefix(u, "s3://")
	bucket, key, ok := strings.Cut(rest, "/")
	if !ok || bucket == "" {
		return "", "", fmt.Errorf("bad s3 uri: %q", u)
	}
	return bucket, key, nil
}

// Bucket returns <org>-replicant-<env>-<class>.
func Bucket(org, env, class string) string { return org + "-replicant-" + env + "-" + class }

// ClassOf returns the bucket class of a physical bucket name for (org, env), or "".
func ClassOf(org, env, bucket string) string {
	p := org + "-replicant-" + env + "-"
	if strings.HasPrefix(bucket, p) {
		return strings.TrimPrefix(bucket, p)
	}
	return ""
}

// Tagging renders the C-ART §5.3 tag header value.
func Tagging(retention, typ, tenant string) string {
	v := url.Values{}
	v.Set("replicant-retention", retention)
	v.Set("replicant-type", typ)
	v.Set("replicant-tenant", tenant)
	return v.Encode()
}

// PutOptions for Put.
type PutOptions struct {
	ContentType     string
	ContentEncoding string
	Tagging         string
	WriteOnce       bool   // If-None-Match: *
	IfMatch         string // ETag for compare-and-swap
}

// ErrPrecondition is returned on 412.
var ErrPrecondition = errors.New("precondition failed")

// ErrNotFound is returned for missing keys.
var ErrNotFound = errors.New("not found")

// Put uploads body (≤ 5 GiB, single part) and returns the ETag.
func (c *Client) Put(ctx context.Context, bucket, key string, body []byte, o PutOptions) (string, error) {
	in := &s3.PutObjectInput{
		Bucket:        aws.String(bucket),
		Key:           aws.String(key),
		Body:          bytes.NewReader(body),
		ContentLength: aws.Int64(int64(len(body))),
	}
	if o.ContentType != "" {
		in.ContentType = aws.String(o.ContentType)
	}
	if o.ContentEncoding != "" && o.ContentEncoding != "identity" {
		in.ContentEncoding = aws.String(o.ContentEncoding)
	}
	if o.Tagging != "" {
		in.Tagging = aws.String(o.Tagging)
	}
	if o.WriteOnce {
		in.IfNoneMatch = aws.String("*")
	}
	if o.IfMatch != "" {
		in.IfMatch = aws.String(o.IfMatch)
	}
	if c.sendChecksum {
		d := sha256.Sum256(body)
		in.ChecksumSHA256 = aws.String(base64.StdEncoding.EncodeToString(d[:]))
	}
	out, err := c.S3.PutObject(ctx, in)
	if err != nil {
		if IsStatus(err, 412) || IsCode(err, "PreconditionFailed") {
			return "", ErrPrecondition
		}
		return "", err
	}
	return aws.ToString(out.ETag), nil
}

// PutWriteOnceIdempotent writes once; on 412 it compares the existing object's digest (C-ART §2 rule 3).
func (c *Client) PutWriteOnceIdempotent(ctx context.Context, bucket, key string, body []byte, o PutOptions) error {
	o.WriteOnce = true
	_, err := c.Put(ctx, bucket, key, body, o)
	if errors.Is(err, ErrPrecondition) {
		cur, _, gerr := c.Get(ctx, bucket, key)
		if gerr != nil {
			return fmt.Errorf("write-once conflict and read failed: %w", gerr)
		}
		if Sha256Hex(cur) == Sha256Hex(body) {
			return nil
		}
		return fmt.Errorf("write-once conflict on %s with different content", key)
	}
	return err
}

// Get reads an object and its ETag.
func (c *Client) Get(ctx context.Context, bucket, key string) ([]byte, string, error) {
	out, err := c.S3.GetObject(ctx, &s3.GetObjectInput{Bucket: aws.String(bucket), Key: aws.String(key)})
	if err != nil {
		if IsNotFound(err) {
			return nil, "", ErrNotFound
		}
		return nil, "", err
	}
	defer out.Body.Close()
	b, err := io.ReadAll(out.Body)
	return b, aws.ToString(out.ETag), err
}

// Head reports existence and size.
func (c *Client) Head(ctx context.Context, bucket, key string) (bool, int64, error) {
	out, err := c.S3.HeadObject(ctx, &s3.HeadObjectInput{Bucket: aws.String(bucket), Key: aws.String(key)})
	if err != nil {
		if IsNotFound(err) {
			return false, 0, nil
		}
		return false, 0, err
	}
	return true, aws.ToInt64(out.ContentLength), nil
}

// List returns keys under prefix (up to max, 0 = all).
func (c *Client) List(ctx context.Context, bucket, prefix string, max int) ([]string, error) {
	var keys []string
	var token *string
	for {
		in := &s3.ListObjectsV2Input{Bucket: aws.String(bucket), Prefix: aws.String(prefix), ContinuationToken: token}
		if max > 0 {
			in.MaxKeys = aws.Int32(int32(max))
		}
		out, err := c.S3.ListObjectsV2(ctx, in)
		if err != nil {
			return nil, err
		}
		for _, o := range out.Contents {
			keys = append(keys, aws.ToString(o.Key))
			if max > 0 && len(keys) >= max {
				return keys, nil
			}
		}
		if !aws.ToBool(out.IsTruncated) || out.NextContinuationToken == nil {
			return keys, nil
		}
		token = out.NextContinuationToken
	}
}

// HeadBucket checks the bucket answers.
func (c *Client) HeadBucket(ctx context.Context, bucket string) error {
	_, err := c.S3.HeadBucket(ctx, &s3.HeadBucketInput{Bucket: aws.String(bucket)})
	return err
}

// IsNotFound reports S3 404s.
func IsNotFound(err error) bool {
	var nsk *types.NoSuchKey
	var nf *types.NotFound
	if errors.As(err, &nsk) || errors.As(err, &nf) {
		return true
	}
	return IsStatus(err, 404) || IsCode(err, "NoSuchKey")
}

// IsStatus reports an HTTP status on an SDK error.
func IsStatus(err error, code int) bool {
	var re interface{ HTTPStatusCode() int }
	if errors.As(err, &re) {
		return re.HTTPStatusCode() == code
	}
	return false
}

// IsCode reports an API error code.
func IsCode(err error, code string) bool {
	var ae smithy.APIError
	if errors.As(err, &ae) {
		return ae.ErrorCode() == code
	}
	return false
}

// Sha256Hex of b.
func Sha256Hex(b []byte) string {
	d := sha256.Sum256(b)
	return hex.EncodeToString(d[:])
}
