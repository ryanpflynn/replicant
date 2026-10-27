"""S3 access (boto3 default credential chain; locally AWS_SHARED_CREDENTIALS_FILE, identity replicant-services)."""
from __future__ import annotations

import asyncio
import logging
from functools import cached_property

from control_plane_api.config import Settings

log = logging.getLogger(__name__)


class S3:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @cached_property
    def client(self):  # noqa: ANN201
        import boto3
        from botocore.config import Config
        return boto3.client("s3", endpoint_url=self.settings.s3_endpoint, region_name=self.settings.s3_region,
                            config=Config(s3={"addressing_style": "path" if self.settings.s3_force_path_style else "auto"},
                                          retries={"max_attempts": 3}, connect_timeout=5, read_timeout=10))

    async def put_once(self, bucket: str, key: str, body: bytes, content_type: str = "application/json") -> bool:
        """Write-once (If-None-Match: *, C-DEF §4.1 rule 5). Returns False when the object already exists."""
        def _put() -> bool:
            from botocore.exceptions import ClientError
            try:
                self.client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type, IfNoneMatch="*")
                return True
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code in ("PreconditionFailed", "412", "ConditionalRequestConflict"):
                    return False
                if code in ("NotImplemented", "InvalidArgument"):
                    # local S3 servers without conditional writes (parity P-05): check, then write
                    try:
                        self.client.head_object(Bucket=bucket, Key=key)
                        return False
                    except ClientError:
                        self.client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
                        return True
                raise
        return await asyncio.to_thread(_put)

    async def put(self, bucket: str, key: str, body: bytes, content_type: str = "application/json") -> None:
        await asyncio.to_thread(self.client.put_object, Bucket=bucket, Key=key, Body=body, ContentType=content_type)

    def overwriter(self, bucket_class: str):  # noqa: ANN201
        bucket = self.settings.bucket(bucket_class)

        async def _w(key: str, body: bytes) -> None:
            await self.put(bucket, key, body)
        return _w

    def writer(self, bucket_class: str):  # noqa: ANN201
        bucket = self.settings.bucket(bucket_class)

        async def _w(key: str, body: bytes) -> None:
            await self.put_once(bucket, key, body)
        return _w
