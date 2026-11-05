"""S3-compatible object storage client (aioboto3)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, BinaryIO, Self

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError
from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Tracer

from py_common.config import StorageSettings

__all__ = ["ObjectInfo", "ObjectStorageClient"]

# S3-compatible servers disagree on how they say "no such key" for a HEAD, which has
# no body to carry an error document: AWS answers a bare 404 (code "404"), others
# send NoSuchKey or NotFound.
_MISSING_KEY_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


def _tracer() -> Tracer:
    # A function and not a module-level tracer: the global provider is installed after
    # this module is imported (setup_tracing runs at start-up), and tests substitute
    # their own.
    return trace.get_tracer("py_common.storage")


@contextmanager
def _s3_span(operation: str, bucket: str, **attributes: Any) -> Iterator[Span]:
    """One client span per storage call, named like the S3 operation.

    botocore has an instrumentor, but it patches the synchronous client only; this
    library talks to S3 through aiobotocore, which that does not reach. So the spans
    are made here, by hand.

    Attributes are deliberately few: the bucket and sizes, never the object key (they
    routinely embed user ids and file names) and never a presigned URL, which is a
    bearer credential.
    """
    with _tracer().start_as_current_span(
        f"s3 {operation}",
        kind=SpanKind.CLIENT,
        attributes={
            "rpc.system": "aws-api",
            "rpc.service": "S3",
            "rpc.method": operation,
            "aws.s3.bucket": bucket,
            **attributes,
        },
    ) as span:
        yield span


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    """What a HEAD or a listing says about one object -- never its body."""

    key: str
    size: int
    last_modified: datetime
    # Listings do not carry it; only :meth:`ObjectStorageClient.head_object` fills it in.
    content_type: str | None = None


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
        bucket = bucket or self.settings.bucket
        # A stream has no length until it is read; only bytes are sized.
        sizes = {"py_common.s3.size_bytes": len(body)} if isinstance(body, bytes) else {}
        with _s3_span("PutObject", bucket, **sizes):
            await self.client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                Metadata=metadata or {},
            )
        return key

    async def delete_object(self, key: str, *, bucket: str | None = None) -> None:
        bucket = bucket or self.settings.bucket
        with _s3_span("DeleteObject", bucket):
            await self.client.delete_object(Bucket=bucket, Key=key)

    async def get_presigned_url(
        self,
        key: str,
        *,
        method: str = "get_object",
        expires_in: int = 3600,
        bucket: str | None = None,
        content_type: str | None = None,
        content_length: int | None = None,
    ) -> str:
        """Sign a URL for ``method`` on ``key``.

        ``content_length`` (``put_object`` only) signs the exact byte count, so S3
        refuses an upload of any other size -- the only way to cap a browser upload,
        since a presigned PUT otherwise accepts whatever arrives. The caller's HTTP
        client must then send that same ``Content-Length``. Passing it with another
        method raises, unlike ``content_type``, which is ignored there: a size limit
        that silently does nothing is worse than none.
        """
        if content_length is not None and method != "put_object":
            raise ValueError("content_length can only be signed for method='put_object'")
        bucket = bucket or self.settings.bucket
        params: dict[str, Any] = {"Bucket": bucket, "Key": key}
        if content_type and method == "put_object":
            params["ContentType"] = content_type
        if content_length is not None:
            params["ContentLength"] = content_length
        # Never records the URL it returns: the signature in it is a credential.
        sizes = {} if content_length is None else {"py_common.s3.size_bytes": content_length}
        with _s3_span("GeneratePresignedUrl", bucket, **sizes) as span:
            span.set_attribute("py_common.s3.presigned_method", method)
            url: str = await self.client.generate_presigned_url(
                ClientMethod=method,
                Params=params,
                ExpiresIn=expires_in,
            )
        return url

    async def get_object_bytes(self, key: str, *, bucket: str | None = None) -> bytes:
        bucket = bucket or self.settings.bucket
        with _s3_span("GetObject", bucket) as span:
            resp = await self.client.get_object(Bucket=bucket, Key=key)
            async with resp["Body"] as stream:
                data: bytes = await stream.read()
            span.set_attribute("py_common.s3.size_bytes", len(data))
            return data

    async def head_object(self, key: str, *, bucket: str | None = None) -> ObjectInfo | None:
        """Size and type of ``key`` without downloading it, or ``None`` when it is not there.

        Only a missing key is ``None``; a permission or network error still raises, so
        "absent" cannot be confused with "could not ask". This is how a service checks
        what a browser actually uploaded to a presigned URL before trusting it.
        """
        bucket = bucket or self.settings.bucket
        with _s3_span("HeadObject", bucket) as span:
            try:
                head = await self.client.head_object(Bucket=bucket, Key=key)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code", "") in _MISSING_KEY_CODES:
                    # An answer, not a failure: the span stays OK, and says what it found.
                    span.set_attribute("py_common.s3.found", False)
                    return None
                raise
            span.set_attribute("py_common.s3.found", True)
            span.set_attribute("py_common.s3.size_bytes", int(head["ContentLength"]))
        return ObjectInfo(
            key=key,
            size=int(head["ContentLength"]),
            last_modified=head["LastModified"],
            content_type=head.get("ContentType"),
        )

    async def list_objects(
        self,
        prefix: str = "",
        *,
        older_than: datetime | None = None,
        bucket: str | None = None,
    ) -> AsyncIterator[list[ObjectInfo]]:
        """Walk a bucket under ``prefix`` one page at a time (at most 1000 keys each).

        Pages, not a list, because a bucket can be large; a caller that wants a cap
        applies it to what it *does* with the keys, not to the listing -- capping the
        listing lets old files still in use crowd the real candidates out of reach.

        ``older_than`` (timezone-aware) keeps only objects last modified before it,
        and pages left empty by that are skipped rather than yielded.
        """
        if older_than is not None and older_than.tzinfo is None:
            raise ValueError("older_than must be timezone-aware")
        bucket = bucket or self.settings.bucket
        paginator = self.client.get_paginator("list_objects_v2")
        # start_span, not start_as_current_span: this is a generator, and a span made
        # current across a `yield` would stay the caller's active span between pages.
        span = _tracer().start_span(
            "s3 ListObjectsV2",
            kind=SpanKind.CLIENT,
            attributes={
                "rpc.system": "aws-api",
                "rpc.service": "S3",
                "rpc.method": "ListObjectsV2",
                "aws.s3.bucket": bucket,
            },
        )
        pages = objects = 0
        try:
            async for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                pages += 1
                infos = [
                    ObjectInfo(
                        key=str(item["Key"]),
                        size=int(item["Size"]),
                        last_modified=item["LastModified"],
                    )
                    for item in page.get("Contents", [])
                    if older_than is None or item["LastModified"] < older_than
                ]
                if infos:
                    objects += len(infos)
                    yield infos
        except Exception as exc:  # not BaseException: a consumer that stops early is no error
            span.record_exception(exc)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            raise
        finally:
            span.set_attribute("py_common.s3.pages", pages)
            span.set_attribute("py_common.s3.objects", objects)
            span.end()
