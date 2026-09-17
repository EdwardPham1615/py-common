"""ObjectStorageClient: lifecycle and the error paths that must not be swallowed."""

from __future__ import annotations

from typing import Any, Self

import pytest
from botocore.exceptions import ClientError

from py_common.config import StorageSettings
from py_common.storage import ObjectStorageClient


class _FakeStream:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    async def read(self) -> bytes:
        return self.data


class _FakeS3:
    """Records calls; raises what the test tells it to."""

    def __init__(self, head_bucket_error: ClientError | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.head_bucket_error = head_bucket_error
        self.stream = _FakeStream(b"payload")

    async def head_bucket(self, **kwargs: Any) -> None:
        self.calls.append(("head_bucket", kwargs))
        if self.head_bucket_error is not None:
            raise self.head_bucket_error

    async def create_bucket(self, **kwargs: Any) -> None:
        self.calls.append(("create_bucket", kwargs))

    async def put_object(self, **kwargs: Any) -> None:
        self.calls.append(("put_object", kwargs))

    async def delete_object(self, **kwargs: Any) -> None:
        self.calls.append(("delete_object", kwargs))

    async def generate_presigned_url(self, **kwargs: Any) -> str:
        self.calls.append(("generate_presigned_url", kwargs))
        return "https://signed.example/object"

    async def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_object", kwargs))
        return {"Body": self.stream}


class _FakeClientCM:
    def __init__(self, client: _FakeS3) -> None:
        self.client = client
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> _FakeS3:
        self.entered += 1
        return self.client

    async def __aexit__(self, *exc: object) -> None:
        self.exited += 1


def _client(fake: _FakeS3) -> tuple[ObjectStorageClient, _FakeClientCM]:
    storage = ObjectStorageClient(StorageSettings(bucket="default-bucket"))
    cm = _FakeClientCM(fake)
    storage._session = type("_S", (), {"client": lambda self, **kw: cm})()  # type: ignore[assignment]
    return storage, cm


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "HeadBucket",
    )


def test_client_before_open_raises_actionable_error() -> None:
    """Wired as a lifespan resource, so 'used before startup' is a real ordering
    bug — it must not surface as AttributeError on None."""
    storage = ObjectStorageClient(StorageSettings())
    with pytest.raises(RuntimeError, match="not open"):
        _ = storage.client


@pytest.mark.asyncio
async def test_open_is_idempotent_and_close_resets() -> None:
    storage, cm = _client(_FakeS3())
    await storage.open()
    await storage.open()
    assert cm.entered == 1  # a second client would leak a connection pool

    await storage.close()
    assert cm.exited == 1
    with pytest.raises(RuntimeError):
        _ = storage.client


@pytest.mark.asyncio
async def test_close_without_open_is_safe() -> None:
    """Shutdown runs even when startup failed."""
    storage, cm = _client(_FakeS3())
    await storage.close()
    assert cm.exited == 0


@pytest.mark.asyncio
async def test_ensure_bucket_creates_only_when_missing() -> None:
    fake = _FakeS3(head_bucket_error=_client_error("404", 404))
    storage, _ = _client(fake)
    async with storage:
        await storage.ensure_bucket()
    assert [name for name, _ in fake.calls] == ["head_bucket", "create_bucket"]


@pytest.mark.asyncio
async def test_ensure_bucket_propagates_permission_errors() -> None:
    """A 403 must not be read as 'missing, so create it'. Swallowing it would turn
    a credentials problem into a confusing create_bucket failure, or worse, hide
    that the caller is pointed at the wrong account."""
    fake = _FakeS3(head_bucket_error=_client_error("403", 403))
    storage, _ = _client(fake)
    async with storage:
        with pytest.raises(ClientError):
            await storage.ensure_bucket()
    assert [name for name, _ in fake.calls] == ["head_bucket"]


@pytest.mark.asyncio
async def test_operations_default_to_configured_bucket_and_accept_override() -> None:
    fake = _FakeS3()
    storage, _ = _client(fake)
    async with storage:
        await storage.put_object("k", b"v")
        await storage.delete_object("k", bucket="other-bucket")

    put = dict(fake.calls[0][1])
    delete = dict(fake.calls[1][1])
    assert put["Bucket"] == "default-bucket"
    assert delete["Bucket"] == "other-bucket"


@pytest.mark.asyncio
async def test_presigned_url_only_sets_content_type_for_put() -> None:
    """Signing a GET with ContentType produces a URL that 403s unless the caller
    happens to send a matching header."""
    fake = _FakeS3()
    storage, _ = _client(fake)
    async with storage:
        await storage.get_presigned_url("k", content_type="image/png")
        await storage.get_presigned_url("k", method="put_object", content_type="image/png")

    assert "ContentType" not in fake.calls[0][1]["Params"]
    assert fake.calls[1][1]["Params"]["ContentType"] == "image/png"


@pytest.mark.asyncio
async def test_get_object_bytes_closes_the_stream() -> None:
    """The body is a streaming response; leaving it open holds a connection."""
    fake = _FakeS3()
    storage, _ = _client(fake)
    async with storage:
        assert await storage.get_object_bytes("k") == b"payload"
    assert fake.stream.closed
