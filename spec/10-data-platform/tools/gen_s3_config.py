#!/usr/bin/env python3
"""Generate S3 request bodies for one environment: the seven C-ART bucket classes plus the
observability buckets (C-ART 1.7.0, CCR-08-05). Output: config/s3/<env>/<bucket>.<kind>.json.

Every file is the literal request body of the corresponding S3 API call:
  lifecycle     PutBucketLifecycleConfiguration  LifecycleConfiguration
  encryption    PutBucketEncryption              ServerSideEncryptionConfiguration
  versioning    PutBucketVersioning              VersioningConfiguration
  object-lock   PutObjectLockConfiguration       ObjectLockConfiguration
  replication   PutBucketReplication             ReplicationConfiguration
  notification  PutBucketNotificationConfiguration NotificationConfiguration
  policy        PutBucketPolicy                  Policy
tools/validate_s3_config.py validates them with botocore's parameter validator and checks
lifecycle-rule restrictions. Usage: gen_s3_config.py [out_dir] [--env prod|staging|dev|local].

Facts the rules depend on (verified 2026-10-24):
  * Overlapping expirations: "the shorter expiration policy is honored"
    https://docs.aws.amazon.com/AmazonS3/latest/userguide/lifecycle-conflicts.html
    => the whole-bucket backstop uses the bucket's LONGEST class (never shortens a tagged object).
  * AbortIncompleteMultipartUpload / ExpiredObjectDeleteMarker cannot sit in a tag-filtered rule
    https://docs.aws.amazon.com/AmazonS3/latest/userguide/intro-lifecycle-rules.html
  * Objects < 128 KB are not transitioned by default; IA/GIR minimum durations 30/90 d
    https://docs.aws.amazon.com/AmazonS3/latest/userguide/lifecycle-transition-general-considerations.html
  * Conditional-header enforcement must be scoped with s3:ObjectCreationOperation
    https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes-enforce.html
  * Compliance-mode locks "can't be overwritten or deleted by any user, including the root user";
    a legal hold blocks deletion independently of retention mode
    https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html
"""
from __future__ import annotations

import argparse
import json
import pathlib

DAYS = {"r7d": 7, "r30d": 30, "r90d": 90, "r1y": 365, "r2y": 730, "r7y": 2557, "job37d": 37}
# WS-11 parity P-05: local lifecycle is compressed so a laptop never accumulates months of data.
LOCAL_DAYS = {"r7d": 1, "r30d": 3, "r90d": 7, "r1y": 14, "r2y": 30, "r7y": 30, "job37d": 3}

# Retention classes allowed per bucket (C-ART 1.8.0 §4), transitions, Object Lock, key class.
# lock: None = Object Lock off (C-ART 1.9.0: tenant buckets have no Object Lock); dict = default retention.
# transcripts allows r7d/r30d as well: tenants may shorten transcript retention (Policy retention, R1-238).
BUCKETS: dict[str, dict] = {
    "artifacts":   {"classes": ["r7d", "r30d", "r90d", "r1y", "r2y", "r7y"], "lock": None,
                    "transitions": {"r1y": [(30, "STANDARD_IA")], "r2y": [(30, "STANDARD_IA"), (120, "GLACIER_IR")],
                                    "r7y": [(30, "STANDARD_IA"), (120, "GLACIER_IR")]},
                    "replicate": True},
    "transcripts": {"classes": ["r7d", "r30d", "r90d"], "lock": None, "transitions": {"r90d": [(30, "STANDARD_IA")]},
                    "replicate": True},
    "session":     {"classes": ["job37d", "r30d"], "lock": None, "transitions": {}, "replicate": False},
    # Compliance mode (ADR-WS10-009, R2-005): the audit bucket holds no free text and no secrets
    # (approval-record free text moved to artifacts; history export split), so nothing ever needs a bypass.
    "audit":       {"classes": ["r2y", "r7y"], "lock": {"Mode": "COMPLIANCE", "Days": 730},
                    "transitions": {"r2y": [(90, "GLACIER_IR")], "r7y": [(90, "GLACIER_IR")]}, "replicate": True},
    "memory":      {"classes": ["r1y", "r2y"], "lock": None,
                    "transitions": {"r1y": [(30, "STANDARD_IA")], "r2y": [(30, "STANDARD_IA")]}, "replicate": True},
    "eval":        {"classes": ["r1y", "perm"], "lock": None, "transitions": {"r1y": [(30, "STANDARD_IA")],
                    "perm": [(90, "STANDARD_IA")]}, "replicate": True},
    "registry":    {"classes": ["perm"], "lock": None, "transitions": {}, "replicate": True},
}

# Observability buckets (C-ART 1.7.0 CCR-08-05; owner WS-08, created by WS-10; WS-08 platform.md §1.3).
# Backends manage current-version retention themselves; S3 lifecycle only bounds residue.
OBS_BUCKETS: dict[str, dict] = {
    "obs-loki":               {"noncurrent": 7},
    "obs-loki-ruler":         {},
    "obs-tempo":              {"noncurrent": 7},
    "obs-mimir-blocks":       {"noncurrent": 7},
    "obs-mimir-ruler":        {"noncurrent": 7},
    "obs-mimir-alertmanager": {"noncurrent": 7},
    "obs-langfuse":           {"noncurrent": 7, "expire_prefix": ("events/", 30)},
    "obs-clickhouse-backup":  {"noncurrent": 7, "expire_all": 14},
}

NONCURRENT_DAYS = 7           # accidental-delete recovery window
MPU_ABORT_DAYS = 2
MIN_TRANSITION_BYTES = 131072  # 128 KiB


def role(env: str, acct: str, name: str) -> str:
    return f"arn:aws:iam::{acct}:role/replicant-{env}-{name}"


def lifecycle(cls: str, local: bool) -> dict:
    b = BUCKETS[cls]
    days = LOCAL_DAYS if local else DAYS
    rules: list[dict] = []
    for rc in b["classes"]:
        tag = {"Key": "replicant-retention", "Value": rc}
        if rc != "perm":
            rules.append({"ID": f"expire-{rc}", "Status": "Enabled", "Filter": {"Tag": tag},
                          "Expiration": {"Days": days[rc]},
                          "NoncurrentVersionExpiration": {"NoncurrentDays": 1 if local else NONCURRENT_DAYS}})
        if local:
            continue  # no storage tiers locally
        for i, (d, sc) in enumerate(b["transitions"].get(rc, [])):
            if rc != "perm" and d >= DAYS[rc]:
                continue
            rules.append({"ID": f"tier-{rc}-{i}-{sc.lower()}", "Status": "Enabled",
                          "Filter": {"And": {"Tags": [tag], "ObjectSizeGreaterThan": MIN_TRANSITION_BYTES}},
                          "Transitions": [{"Days": d, "StorageClass": sc}]})
    rules.append({"ID": "housekeeping", "Status": "Enabled", "Filter": {},
                  "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": MPU_ABORT_DAYS},
                  "Expiration": {"ExpiredObjectDeleteMarker": True}})
    finite = [days[c] for c in b["classes"] if c != "perm"]
    if "perm" not in b["classes"] and finite:
        rules.append({"ID": "backstop-untagged", "Status": "Enabled", "Filter": {"Prefix": ""},
                      "Expiration": {"Days": max(finite)},
                      "NoncurrentVersionExpiration": {"NoncurrentDays": 1 if local else NONCURRENT_DAYS}})
    else:
        rules.append({"ID": "noncurrent-cleanup", "Status": "Enabled", "Filter": {"Prefix": ""},
                      "NoncurrentVersionExpiration": {"NoncurrentDays": 30}})
    return {"Rules": rules}


def obs_lifecycle(name: str) -> dict:
    o = OBS_BUCKETS[name]
    rules: list[dict] = [{"ID": "housekeeping", "Status": "Enabled", "Filter": {},
                          "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": MPU_ABORT_DAYS},
                          "Expiration": {"ExpiredObjectDeleteMarker": True}}]
    if "noncurrent" in o:
        rules.append({"ID": "noncurrent", "Status": "Enabled", "Filter": {"Prefix": ""},
                      "NoncurrentVersionExpiration": {"NoncurrentDays": o["noncurrent"]}})
    if "expire_prefix" in o:
        prefix, d = o["expire_prefix"]
        rules.append({"ID": f"expire-{prefix.strip('/')}", "Status": "Enabled", "Filter": {"Prefix": prefix},
                      "Expiration": {"Days": d}})
    if "expire_all" in o:
        rules.append({"ID": "expire-all", "Status": "Enabled", "Filter": {"Prefix": ""},
                      "Expiration": {"Days": o["expire_all"]}})
    return {"Rules": rules}


def encryption(env: str, acct: str, region: str, local: bool, key: str = "quarantine") -> dict:
    if local:  # WS-11 parity P-05: SSE-S3 locally (no per-tenant KMS on the laptop)
        return {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}
    # Default encryption applies to any PUT or CreateMultipartUpload that omits SSE headers. For the
    # seven classes it is the QUARANTINE key (C-ART §5.3 alias/replicant/<env>/quarantine): services may
    # encrypt with it but only storage-security may decrypt, so a producer that forgets the tenant key
    # fails loudly at first read. Observability buckets default to the observability key.
    return {"Rules": [{"ApplyServerSideEncryptionByDefault": {
        "SSEAlgorithm": "aws:kms", "KMSMasterKeyID": f"arn:aws:kms:{region}:{acct}:alias/replicant/{env}/{key}"},
        "BucketKeyEnabled": True}]}


def object_lock(cls: str, local: bool) -> dict | None:
    lock = BUCKETS[cls]["lock"]
    if lock is None:
        return None
    if lock == "holds":
        return {"ObjectLockEnabled": "Enabled"}              # no default retention: legal holds only
    if local:  # WS-11 parity P-05: 1-day governance lock so laptops can be reset
        return {"ObjectLockEnabled": "Enabled", "Rule": {"DefaultRetention": {"Mode": "GOVERNANCE", "Days": 1}}}
    return {"ObjectLockEnabled": "Enabled", "Rule": {"DefaultRetention": lock}}


def write_once(sid: str, resources: list[str]) -> dict:
    # AWS's documented pattern: deny PutObject without If-None-Match, scoped with
    # s3:ObjectCreationOperation so UploadPart/UploadPartCopy are not affected.
    return {"Sid": sid, "Effect": "Deny", "Principal": "*", "Action": "s3:PutObject", "Resource": resources,
            "Condition": {"Null": {"s3:if-none-match": "true"}, "Bool": {"s3:ObjectCreationOperation": "true"}}}


def bucket_policy(cls: str, org: str, env: str, core: str, exec_: str, region: str, org_id: str, vpce: str,
                  suffix: str = "") -> dict:
    # suffix "-<dr_region>" renders the DR replica's policy (CCR-10-07): same deleters (offboarding and
    # leak purge act on primary and replica), same denies, keys of the DR region.
    b = f"{org}-replicant-{env}-{cls}{suffix}"
    arn, objs = f"arn:aws:s3:::{b}", f"arn:aws:s3:::{b}/*"
    allowed = BUCKETS[cls]["classes"]
    r = lambda acct, n: role(env, acct, n)  # noqa: E731
    deleters = {
        # The only principals that may delete before lifecycle expiry (C-ART rules 4a, 5; CCR-10-11).
        "artifacts":   [r(core, "tenant-offboarding"), r(core, "leak-purge"), r(core, "memory-service")],
        "transcripts": [r(core, "tenant-offboarding"), r(core, "leak-purge")],
        "session":     [r(core, "retention-sweeper"), r(core, "tenant-offboarding"), r(core, "leak-purge")],
        "memory":      [r(core, "tenant-offboarding"), r(core, "memory-service")],
        "eval":        [r(core, "tenant-offboarding")],
        "audit":       [],
        "registry":    [],
    }[cls]
    st: list[dict] = [
        {"Sid": "DenyNonTls", "Effect": "Deny", "Principal": "*", "Action": "s3:*", "Resource": [arn, objs],
         "Condition": {"Bool": {"aws:SecureTransport": "false"}}},
        {"Sid": "DenyOutsideOrganization", "Effect": "Deny", "Principal": "*", "Action": "s3:*", "Resource": [arn, objs],
         "Condition": {"StringNotEquals": {"aws:PrincipalOrgID": org_id}, "Bool": {"aws:PrincipalIsAWSService": "false"}}},
        # Header conditions only in IfExists form (multipart-safe, C-ART §5.3 / CCR-10-02).
        {"Sid": "DenyNonKmsEncryption", "Effect": "Deny", "Principal": "*", "Action": "s3:PutObject", "Resource": objs,
         "Condition": {"StringNotEqualsIfExists": {"s3:x-amz-server-side-encryption": "aws:kms"}}},
        {"Sid": "DenyKeysOutsideEnvironmentKeySet", "Effect": "Deny", "Principal": "*", "Action": "s3:PutObject",
         "Resource": objs,
         "Condition": {"ArnNotLikeIfExists": {"s3:x-amz-server-side-encryption-aws-kms-key-id": f"arn:aws:kms:{region}:{core}:key/*"}}},
        # A PutObject carrying x-amz-tagging is itself authorized against s3:PutObjectTagging,
        # so no Deny on PutObjectTagging beyond the value check.
        {"Sid": "DenyUnknownRetentionClass", "Effect": "Deny", "Principal": "*",
         "Action": ["s3:PutObject", "s3:PutObjectTagging"], "Resource": objs,
         "Condition": {"StringNotEqualsIfExists": {"s3:RequestObjectTag/replicant-retention": allowed}}},
        {"Sid": "NoBucketConfigChangesExceptPlatformPipeline", "Effect": "Deny", "Principal": "*",
         "Action": ["s3:PutBucketPolicy", "s3:DeleteBucketPolicy", "s3:PutBucketAcl", "s3:PutObjectAcl",
                    "s3:PutBucketOwnershipControls", "s3:PutLifecycleConfiguration", "s3:PutBucketVersioning",
                    "s3:PutEncryptionConfiguration", "s3:PutReplicationConfiguration",
                    "s3:PutBucketObjectLockConfiguration", "s3:PutBucketNotification"],
         "Resource": [arn, objs],
         "Condition": {"ArnNotLike": {"aws:PrincipalArn": r(core, "platform-deployer")}}},
        # R2-005 decision: nobody holds a governance bypass anywhere.
        {"Sid": "NobodyBypassesRetention", "Effect": "Deny", "Principal": "*",
         "Action": "s3:BypassGovernanceRetention", "Resource": objs},
    ]
    if deleters:
        st.append({"Sid": "DeletesOnlyByNamedRoles", "Effect": "Deny", "Principal": "*",
                   "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"], "Resource": objs,
                   "Condition": {"ArnNotLike": {"aws:PrincipalArn": deleters}}})
    else:
        st.append({"Sid": "NoDeletes", "Effect": "Deny", "Principal": "*",
                   "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"], "Resource": objs})
    if BUCKETS[cls]["lock"] is not None:
        st.append({"Sid": "LegalHoldsOnlyByLegalRole", "Effect": "Deny", "Principal": "*",
                   "Action": "s3:PutObjectLegalHold", "Resource": objs,
                   "Condition": {"ArnNotLike": {"aws:PrincipalArn": r(core, "legal-hold")}}})
    if cls in ("artifacts", "transcripts", "session"):
        # leak-purge: only tenant job prefixes, never anything else.
        st.append({"Sid": "LeakPurgeOnlyTenantJobKeys", "Effect": "Deny", "Principal": "*",
                   "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:PutObject", "s3:PutObjectTagging"],
                   "NotResource": f"{arn}/tenants/*/jobs/*",
                   "Condition": {"ArnLike": {"aws:PrincipalArn": r(core, "leak-purge")}}})
        # Superseding objects <original key>.p<n> are write-once (C-ART rule 4a). That is enforced in the
        # leak-purge identity policy (config/iam/leak-purge.json): a key pattern here would also match
        # ordinary keys such as screenshot.png.
    if cls in ("artifacts", "transcripts", "session", "audit"):
        st.append({"Sid": "PresignedGetsAtMost15Minutes", "Effect": "Deny", "Principal": "*", "Action": "s3:GetObject",
                   "Resource": objs,
                   "Condition": {"ArnLike": {"aws:PrincipalArn": r(core, "control-plane-api")},
                                 "NumericGreaterThan": {"s3:signatureAge": "900000"}}})
    if cls == "session":
        st.append({"Sid": "ControlPlaneReadsOnlyWorkspaceArchives", "Effect": "Deny", "Principal": "*",
                   "Action": "s3:GetObject", "NotResource": f"{arn}/tenants/*/jobs/*/runs/*/workspace_archive/*",
                   "Condition": {"ArnLike": {"aws:PrincipalArn": r(core, "control-plane-api")}}})
    if cls in ("artifacts", "transcripts", "session"):
        st.append({"Sid": "ExecSandboxRoleOnlyThroughSandboxEndpoint", "Effect": "Deny", "Principal": "*",
                   "Action": "s3:*", "Resource": [arn, objs],
                   "Condition": {"ArnLike": {"aws:PrincipalArn": r(exec_, "sandbox-artifacts")},
                                 "StringNotEquals": {"aws:SourceVpce": vpce}}})
        st.append({"Sid": "AllowExecSandboxRoleRunPrefixes", "Effect": "Allow",
                   "Principal": {"AWS": r(exec_, "sandbox-artifacts")},
                   "Action": ["s3:PutObject", "s3:PutObjectTagging", "s3:AbortMultipartUpload",
                              "s3:ListMultipartUploadParts", "s3:GetObject"],
                   "Resource": f"{arn}/tenants/*/jobs/*/runs/*/*"})
        # agentrun-controller reads every object under the run prefixes of all three run buckets
        # (C-ARCH 1.5 §4 via CCR-02-10; HeadObject is authorized as GetObject; XD-FIX-ORCH-03-iam, XD-FIX-R1-048).
        st.append({"Sid": "AllowExecControllerReadRunObjects", "Effect": "Allow",
                   "Principal": {"AWS": r(exec_, "agentrun-controller")}, "Action": "s3:GetObject",
                   "Resource": f"{arn}/tenants/*/jobs/*/runs/*/*"})
        # Keys the sandbox never writes: workflow-staged inputs (all three run buckets), and in
        # artifacts the runner-owned inbox and the controller-owned record.
        never = [f"{arn}/tenants/*/jobs/*/runs/*/inputs/*"]
        if cls == "artifacts":
            never += [f"{arn}/tenants/*/jobs/*/runs/*/inbox/*", f"{arn}/tenants/*/jobs/*/runs/*/agentrun_record/*"]
        st.append({"Sid": "SandboxNeverWritesStagedOrControlKeys", "Effect": "Deny", "Principal": "*",
                   "Action": ["s3:PutObject", "s3:PutObjectTagging"], "Resource": never,
                   "Condition": {"ArnLike": {"aws:PrincipalArn": r(exec_, "sandbox-artifacts")}}})
    if cls == "transcripts":
        # XD-W3-R1V-001: runner-api checks that a run produced transcript evidence (list, max 1 key).
        st.append({"Sid": "AllowExecRunnerListRunTranscripts", "Effect": "Allow",
                   "Principal": {"AWS": r(exec_, "runner-api")}, "Action": "s3:ListBucket", "Resource": arn,
                   "Condition": {"StringLike": {"s3:prefix": "tenants/*/jobs/*/runs/*"}}})
    if cls == "artifacts":
        st.append({"Sid": "MemoryServiceDeletesOnlyMemoryInjections", "Effect": "Deny", "Principal": "*",
                   "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"],  # DeleteObject denied everywhere below
                   "NotResource": f"{arn}/tenants/*/jobs/*/runs/*/inputs/memory-injection.json",
                   "Condition": {"ArnLike": {"aws:PrincipalArn": r(core, "memory-service")}}})
        st.append({"Sid": "AllowExecRunnerInboxControl", "Effect": "Allow",
                   "Principal": {"AWS": r(exec_, "runner-api")},
                   "Action": ["s3:PutObject", "s3:PutObjectTagging", "s3:GetObject"],
                   "Resource": f"{arn}/tenants/*/jobs/*/runs/*/inbox/*"})   # steering_message, steering_control
        # R1V-057: runner-api lists inbox/ (steering idempotency, sandbox inbox poll; runner-service.md §6).
        st.append({"Sid": "AllowExecRunnerListInbox", "Effect": "Allow",
                   "Principal": {"AWS": r(exec_, "runner-api")}, "Action": "s3:ListBucket", "Resource": arn,
                   "Condition": {"StringLike": {"s3:prefix": "tenants/*/jobs/*/runs/*/inbox/*"}}})
        st.append({"Sid": "AllowExecRunnerReadInputsManifestAndRecords", "Effect": "Allow",
                   "Principal": {"AWS": r(exec_, "runner-api")}, "Action": "s3:GetObject",
                   "Resource": [f"{arn}/tenants/*/jobs/*/runs/*/inputs/manifest.json",
                                f"{arn}/tenants/*/jobs/*/runs/*/manifest.json",   # run-root manifest (XD-W3-R1V-001)
                                f"{arn}/tenants/*/jobs/*/runs/*/agentrun_record/*"]})
        ctl_keys = [f"{arn}/tenants/*/jobs/*/runs/*/agentrun_record/*", f"{arn}/tenants/*/jobs/*/runs/*/inbox/_control.json"]
        st.append({"Sid": "AllowExecControllerWriteRecordsAndControl", "Effect": "Allow",
                   "Principal": {"AWS": r(exec_, "agentrun-controller")},
                   "Action": ["s3:PutObject", "s3:PutObjectTagging"], "Resource": ctl_keys})
        # agentrun_record is write-once; inbox/_control.json is write-once per C-ART (CCR-02-04, CCR-02-09).
        st.append(write_once("AgentRunRecordsAndControlWriteOnce", ctl_keys))
        st.append({"Sid": "ControllerWritesNothingElse", "Effect": "Deny", "Principal": "*",
                   "Action": ["s3:PutObject", "s3:PutObjectTagging", "s3:DeleteObject", "s3:DeleteObjectVersion"],
                   "NotResource": ctl_keys,
                   "Condition": {"ArnLike": {"aws:PrincipalArn": r(exec_, "agentrun-controller")}}})
    if cls == "memory":
        st.append({"Sid": "MemoryServiceDeletesOnlySnapshotAndMemoryExports", "Effect": "Deny", "Principal": "*",
                   "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"],
                   "NotResource": [f"{arn}/tenants/*/snapshots/*", f"{arn}/tenants/*/exports/*", f"{arn}/org/exports/*"],
                   "Condition": {"ArnLike": {"aws:PrincipalArn": r(core, "memory-service")}}})
        st.append(write_once("PurgedSnapshotReplacementsWriteOnce", [f"{arn}/tenants/*/snapshots/*.purged-*"]))
    if cls in ("artifacts", "memory"):
        # XD-FIX-R1-105: memory-service purges whole objects with DeleteObjectVersion only (never a delete
        # marker that would leave the content in a noncurrent version).
        st.append({"Sid": "MemoryServiceOnlyDeletesVersions", "Effect": "Deny", "Principal": "*",
                   "Action": "s3:DeleteObject", "Resource": objs,
                   "Condition": {"ArnLike": {"aws:PrincipalArn": r(core, "memory-service")}}})
    if cls == "registry":
        st.append({"Sid": "AllowExecReadBlobsAndBundlePointers", "Effect": "Allow",
                   "Principal": {"AWS": [r(exec_, "runner-api"), r(exec_, "agentrun-controller")]},
                   "Action": "s3:GetObject", "Resource": [f"{arn}/blobs/sha256/*", f"{arn}/bundles/*"]})
        st.append(write_once("RegistryObjectsWriteOnce", [f"{arn}/blobs/*", f"{arn}/bundles/*", f"{arn}/index/*"]))
    if cls == "audit":
        # Everything in the audit bucket is write-once (C-ART rule 3; CCR-10-06; CCR-08-04).
        st.append(write_once("AuditObjectsWriteOnce", [objs]))
    return {"Version": "2012-10-17", "Id": f"{b}-policy", "Statement": st}


def obs_policy(name: str, org: str, env: str, core: str, org_id: str) -> dict:
    b = f"{org}-replicant-{env}-{name}"
    arn, objs = f"arn:aws:s3:::{b}", f"arn:aws:s3:::{b}/*"
    return {"Version": "2012-10-17", "Id": f"{b}-policy", "Statement": [
        {"Sid": "DenyNonTls", "Effect": "Deny", "Principal": "*", "Action": "s3:*", "Resource": [arn, objs],
         "Condition": {"Bool": {"aws:SecureTransport": "false"}}},
        {"Sid": "DenyOutsideOrganization", "Effect": "Deny", "Principal": "*", "Action": "s3:*", "Resource": [arn, objs],
         "Condition": {"StringNotEquals": {"aws:PrincipalOrgID": org_id}, "Bool": {"aws:PrincipalIsAWSService": "false"}}},
        {"Sid": "NoBucketConfigChangesExceptPlatformPipeline", "Effect": "Deny", "Principal": "*",
         "Action": ["s3:PutBucketPolicy", "s3:DeleteBucketPolicy", "s3:PutLifecycleConfiguration",
                    "s3:PutBucketVersioning", "s3:PutEncryptionConfiguration"],
         "Resource": [arn, objs], "Condition": {"ArnNotLike": {"aws:PrincipalArn": role(env, core, "platform-deployer")}}},
    ]}


def ops_lifecycle(env: str) -> dict:
    # C-ART 1.10 ops bucket: exec_revocation_feed r90d on its prefix; kill_switch_state perm, noncurrent 400 d.
    return {"Rules": [
        {"ID": "exec-revocations-90d", "Status": "Enabled", "Filter": {"Prefix": f"exec-revocations/{env}/"},
         "Expiration": {"Days": 90}, "NoncurrentVersionExpiration": {"NoncurrentDays": 1},
         "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": MPU_ABORT_DAYS}},
        {"ID": "kill-switch-history-400d", "Status": "Enabled", "Filter": {"Prefix": "kill-switches/"},
         "NoncurrentVersionExpiration": {"NoncurrentDays": 400}},
        {"ID": "expired-delete-markers", "Status": "Enabled", "Filter": {},
         "Expiration": {"ExpiredObjectDeleteMarker": True},
         "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": MPU_ABORT_DAYS}},
    ]}


def ops_policy(org: str, env: str, core: str, exec_: str, org_id: str) -> dict:
    """WS-09's s3-bucket-policy.ops-kill-switch.json (XD-FIX-R2-016), adopted verbatim and parameterized,
    plus WS-10's organization boundary."""
    b = f"{org}-replicant-{env}-ops"
    arn, objs = f"arn:aws:s3:::{b}", f"arn:aws:s3:::{b}/*"
    ks, feed = f"{arn}/kill-switches/v1/state.json", f"{arn}/exec-revocations/{env}/*"
    c = lambda n: role(env, core, n)  # noqa: E731
    ks_writers = [c("control-plane-api"), c("killswitch-operator")]   # replicant-<env>-killswitch-operator (XD-W2-09-03)
    return {"Version": "2012-10-17", "Id": f"{b}-policy", "Statement": [
        {"Sid": "DenyNonTls", "Effect": "Deny", "Principal": "*", "Action": "s3:*", "Resource": [arn, objs],
         "Condition": {"Bool": {"aws:SecureTransport": "false"}}},
        {"Sid": "DenyOutsideOrganization", "Effect": "Deny", "Principal": "*", "Action": "s3:*", "Resource": [arn, objs],
         "Condition": {"StringNotEquals": {"aws:PrincipalOrgID": org_id}, "Bool": {"aws:PrincipalIsAWSService": "false"}}},
        {"Sid": "OnlyControlPlaneWritesAndOnlyWithCompareAndSwap", "Effect": "Deny", "Principal": "*",
         "Action": "s3:PutObject", "Resource": ks,
         "Condition": {"Null": {"s3:if-match": "true", "s3:if-none-match": "true"}}},
        {"Sid": "KillSwitchWritersOnly", "Effect": "Deny", "Principal": "*",
         "Action": ["s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"], "Resource": ks,
         "Condition": {"ArnNotLike": {"aws:PrincipalArn": ks_writers}}},
        {"Sid": "RevocationFeedWriterIsRunnerApiOnly", "Effect": "Deny", "Principal": "*", "Action": "s3:PutObject",
         "Resource": feed, "Condition": {"ArnNotLike": {"aws:PrincipalArn": role(env, exec_, "runner-api")}}},
        {"Sid": "NothingElseIsWritable", "Effect": "Deny", "Principal": "*", "Action": "s3:PutObject",
         "NotResource": [ks, feed]},
        {"Sid": "NoDeletesEver", "Effect": "Deny", "Principal": "*",
         "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"], "Resource": objs},
        {"Sid": "NobodyBypassesRetention", "Effect": "Deny", "Principal": "*",
         "Action": "s3:BypassGovernanceRetention", "Resource": objs},
        {"Sid": "WriterAllow", "Effect": "Allow", "Principal": {"AWS": ks_writers},
         "Action": ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion"], "Resource": ks},
        {"Sid": "CoreReadersAllow", "Effect": "Allow",
         "Principal": {"AWS": [c("replicant-worker"), c("publisher-worker"), c("eval-worker"), c("control-plane-api"),
                               c("github-integration"), c("slack-integration"), c("jira-integration")]},
         "Action": "s3:GetObject", "Resource": ks},
        {"Sid": "ExecRunnerReadsKillSwitches", "Effect": "Allow", "Principal": {"AWS": role(env, exec_, "runner-api")},
         "Action": "s3:GetObject", "Resource": ks},
        # C-ART 2.0.0: hourly NDJSON exec-revocations/<env>/<yyyymmddhh>.ndjson, appended by rewriting the hour's
        # object; runner-api is the only reader and writer (verifiers call GET /v1/revocations instead).
        {"Sid": "RunnerApiOwnsRevocationFeed", "Effect": "Allow", "Principal": {"AWS": role(env, exec_, "runner-api")},
         "Action": ["s3:PutObject", "s3:PutObjectTagging", "s3:GetObject"], "Resource": feed},
        {"Sid": "RunnerApiListsRevocationFeed", "Effect": "Allow", "Principal": {"AWS": role(env, exec_, "runner-api")},
         "Action": "s3:ListBucket", "Resource": arn, "Condition": {"StringLike": {"s3:prefix": f"exec-revocations/{env}/*"}}},
        {"Sid": "OnlyRunnerApiReadsRevocationFeed", "Effect": "Deny", "Principal": "*", "Action": ["s3:GetObject", "s3:GetObjectVersion"],
         "Resource": feed, "Condition": {"ArnNotLike": {"aws:PrincipalArn": role(env, exec_, "runner-api")}}},
        {"Sid": "NoBucketConfigChangesExceptPlatformPipeline", "Effect": "Deny", "Principal": "*",
         "Action": ["s3:PutBucketPolicy", "s3:DeleteBucketPolicy", "s3:PutBucketVersioning",
                    "s3:PutLifecycleConfiguration", "s3:PutEncryptionConfiguration"],
         "Resource": arn, "Condition": {"ArnNotLike": {"aws:PrincipalArn": c("platform-deployer")}}},
    ]}


def legal_policy(org: str, env: str, core: str, org_id: str) -> dict:
    """Legal bucket (C-ART 1.10 CCR-10-10): litigation copies of purgeable content. Written and read only by
    replicant-<env>-legal-hold; legal holds only by that role; replicant-leak-purge may delete versions (a held
    version refuses the delete, and the purge records blocked_by_legal_hold); nobody bypasses retention."""
    b = f"{org}-replicant-{env}-legal"
    arn, objs = f"arn:aws:s3:::{b}", f"arn:aws:s3:::{b}/*"
    legal, purge = role(env, core, "legal-hold"), role(env, core, "leak-purge")
    return {"Version": "2012-10-17", "Id": f"{b}-policy", "Statement": [
        {"Sid": "DenyNonTls", "Effect": "Deny", "Principal": "*", "Action": "s3:*", "Resource": [arn, objs],
         "Condition": {"Bool": {"aws:SecureTransport": "false"}}},
        {"Sid": "DenyOutsideOrganization", "Effect": "Deny", "Principal": "*", "Action": "s3:*", "Resource": [arn, objs],
         "Condition": {"StringNotEquals": {"aws:PrincipalOrgID": org_id}, "Bool": {"aws:PrincipalIsAWSService": "false"}}},
        {"Sid": "OnlyLegalRoleAndLeakPurgeTouchObjects", "Effect": "Deny", "Principal": "*",
         "Action": ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion", "s3:PutObjectLegalHold", "s3:ListBucket",
                    "s3:ListBucketVersions"],
         "Resource": [arn, objs], "Condition": {"ArnNotLike": {"aws:PrincipalArn": [legal, purge]}}},
        # Leak purge (C-ART 1.10 rule 4a, CCR-10-10): may read, write the superseding <key>.p<n> write-once,
        # and delete versions. S3 itself refuses DeleteObjectVersion on a version under a legal hold, so only
        # unheld objects can be purged; a refusal is recorded as blocked_by_legal_hold and escalated.
        {"Sid": "LeakPurgeNeverHoldsNorPlainDeletes", "Effect": "Deny", "Principal": "*",
         "Action": ["s3:PutObjectLegalHold", "s3:DeleteObject"], "Resource": objs,
         "Condition": {"ArnLike": {"aws:PrincipalArn": purge}}},
        {"Sid": "LegalBucketWritesAreWriteOnce", "Effect": "Deny", "Principal": "*", "Action": "s3:PutObject",
         "Resource": objs, "Condition": {"Null": {"s3:if-none-match": "true"}, "Bool": {"s3:ObjectCreationOperation": "true"}}},
        {"Sid": "DeletesOnlyByLeakPurge", "Effect": "Deny", "Principal": "*",
         "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"], "Resource": objs,
         "Condition": {"ArnNotLike": {"aws:PrincipalArn": purge}}},
        {"Sid": "NobodyBypassesRetention", "Effect": "Deny", "Principal": "*",
         "Action": ["s3:BypassGovernanceRetention", "s3:PutObjectRetention"], "Resource": objs},
        {"Sid": "NoBucketConfigChangesExceptPlatformPipeline", "Effect": "Deny", "Principal": "*",
         "Action": ["s3:PutBucketPolicy", "s3:DeleteBucketPolicy", "s3:PutBucketVersioning", "s3:PutLifecycleConfiguration",
                    "s3:PutEncryptionConfiguration", "s3:PutBucketObjectLockConfiguration"],
         "Resource": arn, "Condition": {"ArnNotLike": {"aws:PrincipalArn": role(env, core, "platform-deployer")}}},
    ]}


def notification(cls: str, env: str, core: str, region: str) -> dict | None:
    # leak-scanner input (WS-08 session-capture.md §7): ObjectCreated on transcripts and artifacts
    # -> SQS replicant-<env>-leak-scan. The scanner filters by artifact type itself (S3 filters are prefix/suffix only).
    if cls not in ("transcripts", "artifacts"):
        return None
    return {"QueueConfigurations": [{
        "Id": "leak-scan", "QueueArn": f"arn:aws:sqs:{region}:{core}:replicant-{env}-leak-scan",
        "Events": ["s3:ObjectCreated:*"],
        "Filter": {"Key": {"FilterRules": [{"Name": "prefix", "Value": "tenants/"}]}}}]}


def replication(cls: str, org: str, env: str, core: str, dr_region: str) -> dict | None:
    if not BUCKETS[cls]["replicate"]:
        return None
    return {
        "Role": role(env, core, "s3-replication"),
        "Rules": [{
            "ID": "dr-all", "Priority": 1, "Status": "Enabled", "Filter": {},
            "DeleteMarkerReplication": {"Status": "Disabled"},
            "SourceSelectionCriteria": {"SseKmsEncryptedObjects": {"Status": "Enabled"}},
            "Destination": {
                "Bucket": f"arn:aws:s3:::{org}-replicant-{env}-{cls}-{dr_region}",
                "StorageClass": "STANDARD_IA",
                "EncryptionConfiguration": {"ReplicaKmsKeyID": f"arn:aws:kms:{dr_region}:{core}:alias/replicant/{env}/dr/{cls}"},
                "ReplicationTime": {"Status": "Enabled", "Time": {"Minutes": 15}},
                "Metrics": {"Status": "Enabled", "EventThreshold": {"Minutes": 15}},
            },
        }],
    }


def main(out: pathlib.Path, env: str = "prod", org: str = "acme", core: str = "111111111111",
         exec_: str = "222222222222", region: str = "us-east-1", dr_region: str = "us-west-2",
         org_id: str = "o-acmereplicant1", vpce: str = "vpce-0e1c5ab0d3f4a7b21") -> None:
    local = env == "local"
    d = out / env
    d.mkdir(parents=True, exist_ok=True)
    for cls in BUCKETS:
        files = {
            "lifecycle": lifecycle(cls, local),
            "encryption": encryption(env, core, region, local),
            "versioning": {"Status": "Enabled"},
            "object-lock": object_lock(cls, local),
            # Local stores do not enforce bucket policies, replication, or notifications (WS-11 P-05).
            "policy": None if local else bucket_policy(cls, org, env, core, exec_, region, org_id, vpce),
            "replication": None if local else replication(cls, org, env, core, dr_region),
            "notification": None if local else notification(cls, env, core, region),
        }
        for kind, body in files.items():
            if body is not None:
                (d / f"{cls}.{kind}.json").write_text(json.dumps(body, indent=2) + "\n")
        if not local and BUCKETS[cls]["replicate"]:
            dr = bucket_policy(cls, org, env, core, exec_, dr_region, org_id, vpce, suffix=f"-{dr_region}")
            (d / f"{cls}-{dr_region}.policy.json").write_text(json.dumps(dr, indent=2) + "\n")
    # ops bucket (C-ART 1.10 CCR-09-10, CCR-02-09): outside the seven classes, versioned, no Object Lock.
    ops_files = {"lifecycle": ops_lifecycle(env), "versioning": {"Status": "Enabled"},
                 "encryption": encryption(env, core, region, local, key="ops"),
                 "policy": None if local else ops_policy(org, env, core, exec_, org_id)}
    for kind, body in ops_files.items():
        if body is not None:
            (d / f"ops.{kind}.json").write_text(json.dumps(body, indent=2) + "\n")
    # legal bucket (C-ART 1.10 CCR-10-10): Object Lock on, no default retention (legal holds only).
    legal_files = {"versioning": {"Status": "Enabled"}, "object-lock": {"ObjectLockEnabled": "Enabled"},
                   "encryption": encryption(env, core, region, local, key="legal"),
                   "lifecycle": {"Rules": [{"ID": "housekeeping", "Status": "Enabled", "Filter": {},
                                            "Expiration": {"ExpiredObjectDeleteMarker": True},
                                            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": MPU_ABORT_DAYS}}]},
                   "policy": None if local else legal_policy(org, env, core, org_id)}
    for kind, body in legal_files.items():
        if body is not None and not local:   # no local legal bucket (WS-11 parity P-42)
            (d / f"legal.{kind}.json").write_text(json.dumps(body, indent=2) + "\n")
    if not local:
        for name in OBS_BUCKETS:
            files = {"lifecycle": obs_lifecycle(name), "versioning": {"Status": "Enabled"},
                     "encryption": encryption(env, core, region, False, key="observability"),
                     "policy": obs_policy(name, org, env, core, org_id)}
            for kind, body in files.items():
                (d / f"{name}.{kind}.json").write_text(json.dumps(body, indent=2) + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default=str(pathlib.Path(__file__).resolve().parents[1] / "config" / "s3"))
    ap.add_argument("--env", default="prod")
    a = ap.parse_args()
    if a.env == "local":
        main(pathlib.Path(a.out), env="local", org="local", core="000000000000", exec_="000000000000",
             org_id="o-local", vpce="vpce-local")
    else:
        main(pathlib.Path(a.out), env=a.env)
