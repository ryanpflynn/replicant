"""S3 (boto3 default chain; locally AWS_SHARED_CREDENTIALS_FILE, identity replicant-services)."""
from __future__ import annotations

import asyncio
import hashlib
from functools import cached_property

from memory_service.config import Settings


class S3:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.disabled = False  # tests without S3

    @cached_property
    def client(self):  # noqa: ANN201
        import boto3
        from botocore.config import Config
        return boto3.client("s3", endpoint_url=self.settings.s3_endpoint, region_name=self.settings.s3_region,
                            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3},
                                          connect_timeout=5, read_timeout=15))

    async def put(self, bucket_class: str, key: str, body: bytes, content_type: str = "application/json") -> str:
        sha = hashlib.sha256(body).hexdigest()
        if not self.disabled:
            await asyncio.to_thread(self.client.put_object, Bucket=self.settings.bucket(bucket_class), Key=key,
                                    Body=body, ContentType=content_type)
        return sha

    async def delete(self, bucket_class: str, key: str) -> None:
        if not self.disabled:
            await asyncio.to_thread(self.client.delete_object, Bucket=self.settings.bucket(bucket_class), Key=key)
