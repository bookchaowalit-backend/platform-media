"""Object bytes never pass through the API, except a bounded leading range read for the
content-type check at completion. Adapters must bound network waits."""

from typing import Protocol
from urllib.parse import urlsplit


class ObjectStore(Protocol):
    def presign_put(
        self, key: str, content_type: str, size: int, checksum: str, expires: int
    ) -> dict: ...
    def head(self, key: str) -> dict | None: ...
    def read_prefix(self, key: str, length: int) -> bytes | None: ...
    def presign_get(self, key: str, expires: int) -> str: ...
    def delete(self, key: str) -> None: ...


class S3ObjectStore:
    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "auto",
    ):
        if (
            urlsplit(endpoint).scheme != "https"
            or not bucket
            or not access_key
            or not secret_key
        ):
            raise ValueError("explicit HTTPS object-store configuration required")
        import boto3
        from botocore.config import Config

        # Explicit credentials prevent environment/instance-profile discovery.
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(
                signature_version="s3v4",
                connect_timeout=3,
                read_timeout=5,
                retries={"max_attempts": 1},
                max_pool_connections=8,
            ),
        )
        self.bucket = bucket

    def presign_put(self, key, content_type, size, checksum, expires):
        params = {
            "Bucket": self.bucket,
            "Key": key,
            "ContentType": content_type,
            "ContentLength": size,
            "ChecksumSHA256": checksum,
            "IfNoneMatch": "*",
        }
        return {
            "url": self.client.generate_presigned_url(
                "put_object", Params=params, ExpiresIn=expires
            ),
            "headers": {
                "Content-Type": content_type,
                "Content-Length": str(size),
                "x-amz-checksum-sha256": checksum,
                "If-None-Match": "*",
            },
        }

    def head(self, key):
        from botocore.exceptions import ClientError

        try:
            result = self.client.head_object(
                Bucket=self.bucket, Key=key, ChecksumMode="ENABLED"
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return {
            "size": result["ContentLength"],
            "content_type": result.get("ContentType"),
            "checksum_sha256": result.get("ChecksumSHA256"),
        }

    def read_prefix(self, key, length):
        from botocore.exceptions import ClientError

        try:
            result = self.client.get_object(
                Bucket=self.bucket, Key=key, Range=f"bytes=0-{length - 1}"
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        body = result["Body"]
        try:
            return body.read(length)
        finally:
            body.close()

    def presign_get(self, key, expires):
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires
        )

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)
