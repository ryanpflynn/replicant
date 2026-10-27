#!/usr/bin/env python3
"""Validate generated S3 request bodies against botocore's service model (the same validator the
AWS SDK runs before sending), plus structural IAM checks. Usage: validate_s3_config.py <dir>."""
import json
import pathlib
import sys

import botocore.session
from botocore.validate import ParamValidator

s3 = botocore.session.get_session().get_service_model("s3")
SHAPES = {
    "lifecycle": ("PutBucketLifecycleConfiguration", "LifecycleConfiguration"),
    "encryption": ("PutBucketEncryption", "ServerSideEncryptionConfiguration"),
    "versioning": ("PutBucketVersioning", "VersioningConfiguration"),
    "object-lock": ("PutObjectLockConfiguration", "ObjectLockConfiguration"),
    "replication": ("PutBucketReplication", "ReplicationConfiguration"),
    "notification": ("PutBucketNotificationConfiguration", "NotificationConfiguration"),
}
errors = 0
for f in sorted(pathlib.Path(sys.argv[1]).glob("*.json")):
    kind = f.stem.rsplit(".", 1)[1]
    body = json.loads(f.read_text())
    if kind == "policy":
        assert body["Version"] == "2012-10-17" and all("Effect" in s for s in body["Statement"]), f
        assert len(json.dumps(body, separators=(",", ":"))) <= 20480, f"{f}: bucket policy > 20 KB"
        continue
    op, member = SHAPES[kind]
    shape = s3.operation_model(op).input_shape.members[member]
    report = ParamValidator().validate(body, shape)
    if report.has_errors():
        errors += 1
        print(f"{f.name}: {report.generate_report()}")
    if kind == "lifecycle":
        assert len(body["Rules"]) <= 1000
        for r in body["Rules"]:
            uses_tags = "Tag" in r["Filter"] or "Tags" in r["Filter"].get("And", {})
            assert not (uses_tags and ("AbortIncompleteMultipartUpload" in r or
                        r.get("Expiration", {}).get("ExpiredObjectDeleteMarker"))), f"{f}:{r['ID']} tag filter + forbidden action"
print("OK" if errors == 0 else f"{errors} invalid")
sys.exit(1 if errors else 0)
