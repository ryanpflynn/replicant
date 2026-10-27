package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/sts"

	"example.com/replicant/internal/runner/api"
	"example.com/replicant/internal/runner/ids"
	"example.com/replicant/internal/runner/s3x"
)

// Creds vends per-run S3 credentials (local-runner.md §2). Modes: sts (AssumeRole with the session policy
// of runner-service.md §7.2 minus KMS), static (M1 fallback: a configured identity, not prefix-scoped;
// DEVIATIONS), auto (sts if the startup probe passes, else static).
type Creds struct {
	Mode        string
	STSEndpoint string
	Region      string
	RoleArn     string
	AccessKey   string
	SecretKey   string
	StaticAK    string
	StaticSK    string
	Probe       func(ctx context.Context, c api.AwsContainerCredentials, allowedKey string) error
	Log         *slog.Logger

	mu    sync.Mutex
	cache map[string]api.AwsContainerCredentials
}

func sessionPolicy(org, env string, prefixes api.ArtifactPrefixes) string {
	keyOf := func(u string) string {
		_, k, _ := s3x.ParseURI(u)
		return k
	}
	runKey := keyOf(prefixes.Artifacts)
	wild := fmt.Sprintf("arn:aws:s3:::%s-replicant-%s-*/%s", org, env, runKey)
	pol := map[string]any{
		"Version": "2012-10-17",
		"Statement": []map[string]any{
			{"Sid": "WriteRunPrefixes", "Effect": "Allow", "Action": []string{"s3:PutObject", "s3:PutObjectTagging", "s3:AbortMultipartUpload"},
				"Resource": []string{wild + "*"}},
			{"Sid": "NoWritesToInputsInboxOrRecord", "Effect": "Deny", "Action": []string{"s3:PutObject", "s3:PutObjectTagging"},
				"Resource": []string{wild + "inputs/*", wild + "inbox/*", wild + "agentrun_record/*"}},
			{"Sid": "ReadInputsAndOwnManifest", "Effect": "Allow", "Action": []string{"s3:GetObject"},
				"Resource": []string{wild + "inputs/*", fmt.Sprintf("arn:aws:s3:::%s-replicant-%s-artifacts/%smanifest.json", org, env, runKey)}},
		},
	}
	b, _ := json.Marshal(pol)
	return string(b)
}

func (c *Creds) assume(ctx context.Context, runID, policy string) (api.AwsContainerCredentials, error) {
	cl := sts.New(sts.Options{
		Region:       c.Region,
		Credentials:  credentials.NewStaticCredentialsProvider(c.AccessKey, c.SecretKey, ""),
		BaseEndpoint: aws.String(c.STSEndpoint),
	})
	out, err := cl.AssumeRole(ctx, &sts.AssumeRoleInput{
		RoleArn:         aws.String(c.RoleArn),
		RoleSessionName: aws.String(runID),
		Policy:          aws.String(policy),
		DurationSeconds: aws.Int32(3600),
	})
	if err != nil {
		return api.AwsContainerCredentials{}, err
	}
	return api.AwsContainerCredentials{
		AccessKeyId: aws.ToString(out.Credentials.AccessKeyId), SecretAccessKey: aws.ToString(out.Credentials.SecretAccessKey),
		Token: aws.ToString(out.Credentials.SessionToken), Expiration: aws.ToTime(out.Credentials.Expiration).UTC(),
	}, nil
}

// Init resolves mode auto with the RC-SBX-04-style probe.
func (c *Creds) Init(ctx context.Context, org, env string) {
	if c.Mode != "auto" {
		return
	}
	if c.RoleArn != "" && c.AccessKey != "" {
		probeRun := ids.ULID("run")
		key := fmt.Sprintf("tenants/probe/jobs/job_x/runs/%s/", probeRun)
		pref := api.ArtifactPrefixes{Artifacts: fmt.Sprintf("s3://%s/%s", s3x.Bucket(org, env, "artifacts"), key)}
		cr, err := c.assume(ctx, probeRun, sessionPolicy(org, env, pref))
		if err == nil && c.Probe != nil {
			err = c.Probe(ctx, cr, key+"probe/ok")
		}
		if err == nil {
			c.Mode = "sts"
			c.Log.Info("sandbox credentials mode sts")
			return
		}
		c.Log.Warn("sts session policies not enforced by this store; using static sandbox credentials", "err", err)
	}
	c.Mode = "static"
}

// For returns credentials for a run (cached 50 min).
func (c *Creds) For(ctx context.Context, org, env, runID string, prefixes api.ArtifactPrefixes) (api.AwsContainerCredentials, error) {
	c.mu.Lock()
	if cr, ok := c.cache[runID]; ok && time.Until(cr.Expiration) > 10*time.Minute {
		c.mu.Unlock()
		return cr, nil
	}
	c.mu.Unlock()
	var cr api.AwsContainerCredentials
	var err error
	switch c.Mode {
	case "sts":
		cr, err = c.assume(ctx, runID, sessionPolicy(org, env, prefixes))
	default:
		if c.StaticAK == "" {
			return cr, fmt.Errorf("no sandbox credentials configured")
		}
		cr = api.AwsContainerCredentials{AccessKeyId: c.StaticAK, SecretAccessKey: c.StaticSK, Token: "", Expiration: time.Now().Add(time.Hour).UTC().Truncate(time.Second)}
	}
	if err != nil {
		return cr, err
	}
	c.mu.Lock()
	if c.cache == nil {
		c.cache = map[string]api.AwsContainerCredentials{}
	}
	c.cache[runID] = cr
	c.mu.Unlock()
	return cr, nil
}

// Forget drops cached credentials at terminal.
func (c *Creds) Forget(runID string) {
	c.mu.Lock()
	delete(c.cache, runID)
	c.mu.Unlock()
}
