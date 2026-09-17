"""S3-compatible object storage client (aioboto3)."""

from __future__ import annotations

from types import TracebackType
from typing import Any, BinaryIO, Self

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError

from py_common.config import StorageSettings

__all__ = ["ObjectStorageClient"]


class ObjectStorageClient:
    """Async wrapper around the S3 API — works with SeaweedFS, AWS S3, Ceph RGW, etc.

    Holds a single long-lived S3 client: open it once at startup and close it
    on shutdown (creating a client per operation costs a connection pool + TLS
    handshake each call). Typically wired as a ``LifespanResource``::

        storage = ObjectStorageClient(settings.storage)
        LifespanResource(name="storage", startup=storage.open, shutdown=storage.close)
    """

    def __init__(self, settings: StorageSettings) -> None:
        self.settings = settings
        self._session = aioboto3.Session()
        self._client: Any = None
        self._client_cm: Any = None

    async def open(self) -> None:
        if self._client is not None:
            return
        self._client_cm = self._session.client(
            service_name="s3",
            endpoint_url=self.settings.endpoint_url,
            aws_access_key_id=self.settings.access_key,
            aws_secret_access_key=self.settings.secret_key,
            region_name=self.settings.region,
            config=Config(
                s3={"addressing_style": "path" if self.settings.use_path_style else "virtual"},
                signature_version="s3v4",
            ),
        )
        self._client = await self._client_cm.__aenter__()

    async def close(self) -> None:
        if self._client_cm is not None:
            await self._client_cm.__aexit__(None, None, None)
        self._client = None
        self._client_cm = None

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def client(self) -> Any:
        if self._client is None:
            raise RuntimeError("ObjectStorageClient is not open — call `await client.open()` first")
        return self._client

    async def ensure_bucket(self, bucket: str | None = None) -> None:
        bucket = bucket or self.settings.bucket
        try:
            await self.client.head_bucket(Bucket=bucket)
        except ClientError as exc:
            # Only treat "bucket missing" as creatable; auth/network errors propagate.
            code = exc.response.get("Error", {}).get("Code", "")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in ("404", "NoSuchBucket") or status == 404:
                await self.client.create_bucket(Bucket=bucket)
            else:
                raise

    async def put_object(
        self,
        key: str,
        body: bytes | BinaryIO,
        *,
        content_type: str = "application/octet-stream",
        bucket: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> str:
        await self.client.put_object(
            Bucket=bucket or self.settings.bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            Metadata=metadata or {},
        )
        return key

    async def delete_object(self, key: str, *, bucket: str | None = None) -> None:
        await self.client.delete_object(Bucket=bucket or self.settings.bucket, Key=key)

    async def get_presigned_url(
        self,
        key: str,
        *,
        method: str = "get_object",
        expires_in: int = 3600,
        bucket: str | None = None,
        content_type: str | None = None,
    ) -> str:
        params: dict[str, Any] = {"Bucket": bucket or self.settings.bucket, "Key": key}
        if content_type and method == "put_object":
            params["ContentType"] = content_type
        url: str = await self.client.generate_presigned_url(
            ClientMethod=method,
            Params=params,
            ExpiresIn=expires_in,
        )
        return url

    async def get_object_bytes(self, key: str, *, bucket: str | None = None) -> bytes:
        resp = await self.client.get_object(Bucket=bucket or self.settings.bucket, Key=key)
        async with resp["Body"] as stream:
            data: bytes = await stream.read()
            return data
