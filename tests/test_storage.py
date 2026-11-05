"""ObjectStorageClient: lifecycle and the error paths that must not be swallowed."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, Self

import pytest
from botocore.exceptions import ClientError
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

import py_common.storage as storage_module
from py_common.config import StorageSettings
from py_common.storage import ObjectInfo, ObjectStorageClient

_NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


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
        self.head_object_error: ClientError | None = None
        self.pages: list[dict[str, Any]] = []
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

    async def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("head_object", kwargs))
        if self.head_object_error is not None:
            raise self.head_object_error
        return {"ContentLength": 42, "ContentType": "image/png", "LastModified": _NOW}

    def get_paginator(self, operation: str) -> _FakePaginator:
        assert operation == "list_objects_v2"
        return _FakePaginator(self.pages, self.calls)


class _FakePaginator:
    def __init__(
        self, pages: list[dict[str, Any]], calls: list[tuple[str, dict[str, Any]]]
    ) -> None:
        self._pages = pages
        self._calls = calls

    async def paginate(self, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        self._calls.append(("paginate", kwargs))
        for page in self._pages:
            yield page


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


# --- head_object, list_objects, content_length ------------------------------


async def test_head_object_reports_size_and_type_without_a_body() -> None:
    fake = _FakeS3()
    storage, _ = _client(fake)
    async with storage:
        info = await storage.head_object("k", bucket="b")

    assert info == ObjectInfo(key="k", size=42, last_modified=_NOW, content_type="image/png")
    assert fake.calls[0] == ("head_object", {"Bucket": "b", "Key": "k"})


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
async def test_head_object_returns_none_for_a_missing_key(code: str) -> None:
    """Servers disagree on the code a bodiless HEAD 404 carries."""
    fake = _FakeS3()
    fake.head_object_error = _client_error(code, 404)
    storage, _ = _client(fake)
    async with storage:
        assert await storage.head_object("gone") is None


async def test_head_object_does_not_hide_a_failure_as_absence() -> None:
    """None must mean "not there", never "could not ask" -- a denied HEAD read as
    missing would let an oversized upload through the size check."""
    fake = _FakeS3()
    fake.head_object_error = _client_error("403", 403)
    storage, _ = _client(fake)
    async with storage:
        with pytest.raises(ClientError):
            await storage.head_object("k")


def _page(*items: tuple[str, int, datetime]) -> dict[str, Any]:
    return {"Contents": [{"Key": k, "Size": n, "LastModified": t} for k, n, t in items]}


async def test_list_objects_yields_one_list_per_page() -> None:
    fake = _FakeS3()
    fake.pages = [_page(("a", 1, _NOW), ("b", 2, _NOW)), _page(("c", 3, _NOW))]
    storage, _ = _client(fake)
    async with storage:
        pages = [page async for page in storage.list_objects("p/", bucket="b")]

    assert [[o.key for o in page] for page in pages] == [["a", "b"], ["c"]]
    assert pages[0][1] == ObjectInfo(key="b", size=2, last_modified=_NOW)
    assert fake.calls[0] == ("paginate", {"Bucket": "b", "Prefix": "p/"})


async def test_list_objects_older_than_filters_and_skips_emptied_pages() -> None:
    fake = _FakeS3()
    old, new = _NOW - timedelta(days=2), _NOW
    fake.pages = [
        _page(("old1", 1, old), ("new1", 1, new)),
        _page(("new2", 1, new)),
        _page(("old2", 1, old)),
    ]
    storage, _ = _client(fake)
    async with storage:
        pages = [p async for p in storage.list_objects(older_than=_NOW - timedelta(days=1))]

    # The middle page had nothing old enough: not yielded as an empty list.
    assert [[o.key for o in page] for page in pages] == [["old1"], ["old2"]]


async def test_list_objects_on_an_empty_bucket_yields_nothing() -> None:
    fake = _FakeS3()
    fake.pages = [{}]  # S3 omits "Contents" entirely when nothing matches
    storage, _ = _client(fake)
    async with storage:
        assert [p async for p in storage.list_objects()] == []


async def test_list_objects_rejects_a_naive_cutoff() -> None:
    """A naive datetime compared with S3's aware ones raises TypeError deep in the
    loop, after the first page has been fetched; say so at the call instead."""
    storage, _ = _client(_FakeS3())
    async with storage:
        with pytest.raises(ValueError, match="timezone-aware"):
            _ = [p async for p in storage.list_objects(older_than=datetime(2026, 1, 1))]


async def test_presigned_put_can_sign_the_content_length() -> None:
    fake = _FakeS3()
    storage, _ = _client(fake)
    async with storage:
        await storage.get_presigned_url(
            "k", method="put_object", content_type="image/png", content_length=1234
        )
        await storage.get_presigned_url("k", method="put_object")

    assert fake.calls[0][1]["Params"]["ContentLength"] == 1234
    assert "ContentLength" not in fake.calls[1][1]["Params"]


async def test_content_length_on_a_download_url_is_an_error_not_a_noop() -> None:
    storage, _ = _client(_FakeS3())
    async with storage:
        with pytest.raises(ValueError, match="put_object"):
            await storage.get_presigned_url("k", content_length=10)


# --- spans ------------------------------------------------------------------
#
# aiobotocore has no instrumentor (the botocore one patches the synchronous client),
# so without these an S3 call is a hole in every trace that goes through one.


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(storage_module, "_tracer", lambda: provider.get_tracer("t"))
    return exporter


def _every_string(exporter: InMemorySpanExporter) -> str:
    return " ".join(
        [s.name for s in exporter.get_finished_spans()]
        + [str(v) for s in exporter.get_finished_spans() for v in (s.attributes or {}).values()]
    )


async def test_every_operation_gets_a_named_client_span(spans: InMemorySpanExporter) -> None:
    fake = _FakeS3()
    fake.pages = [_page(("a", 1, _NOW))]
    storage, _ = _client(fake)
    async with storage:
        await storage.put_object("k", b"12345")
        await storage.get_object_bytes("k")
        await storage.delete_object("k")
        await storage.head_object("k")
        _ = [p async for p in storage.list_objects("pre/")]
        await storage.get_presigned_url("k")

    finished = spans.get_finished_spans()
    assert [s.name for s in finished] == [
        "s3 PutObject",
        "s3 GetObject",
        "s3 DeleteObject",
        "s3 HeadObject",
        "s3 ListObjectsV2",
        "s3 GeneratePresignedUrl",
    ]
    for span in finished:
        assert span.kind is SpanKind.CLIENT
        assert span.attributes is not None
        assert span.attributes["aws.s3.bucket"] == "default-bucket"
        assert span.attributes["rpc.service"] == "S3"
        assert span.status.status_code is StatusCode.UNSET


async def test_sizes_are_recorded_where_they_are_known(spans: InMemorySpanExporter) -> None:
    storage, _ = _client(_FakeS3())
    async with storage:
        await storage.put_object("k", b"12345")
        await storage.get_object_bytes("k")  # the fake streams b"payload"
        await storage.head_object("k")  # the fake says 42
        await storage.get_presigned_url("k", method="put_object", content_length=99)

    sizes = [
        s.attributes["py_common.s3.size_bytes"] for s in spans.get_finished_spans() if s.attributes
    ]
    assert sizes == [5, 7, 42, 99]


async def test_neither_the_key_nor_the_signed_url_is_ever_recorded(
    spans: InMemorySpanExporter,
) -> None:
    """A presigned URL is a bearer credential, and keys routinely embed user ids.

    The fake returns a URL with a signature in it so there is something to leak.
    """

    class _Signing(_FakeS3):
        async def generate_presigned_url(self, **kwargs: Any) -> str:
            return "https://s3.example/b/k?X-Amz-Signature=TOPSECRET"

    storage, _ = _client(_Signing())
    async with storage:
        url = await storage.get_presigned_url("users/alice@example.com/avatar.png")
        await storage.put_object("users/alice@example.com/avatar.png", b"x")
        await storage.delete_object("users/alice@example.com/avatar.png")

    assert "TOPSECRET" in url  # the caller still gets it
    recorded = _every_string(spans)
    assert "TOPSECRET" not in recorded
    assert "alice@example.com" not in recorded
    assert "https://" not in recorded


async def test_a_failing_call_marks_its_span_as_an_error(spans: InMemorySpanExporter) -> None:
    class _Boom(_FakeS3):
        async def put_object(self, **kwargs: Any) -> None:
            raise _client_error("AccessDenied", 403)

    storage, _ = _client(_Boom())
    async with storage:
        with pytest.raises(ClientError):
            await storage.put_object("k", b"v")

    (span,) = spans.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert [e.name for e in span.events] == ["exception"]


async def test_a_missing_key_is_an_answer_not_an_error(spans: InMemorySpanExporter) -> None:
    fake = _FakeS3()
    fake.head_object_error = _client_error("404", 404)
    storage, _ = _client(fake)
    async with storage:
        assert await storage.head_object("gone") is None

    (span,) = spans.get_finished_spans()
    assert span.status.status_code is StatusCode.UNSET
    assert span.attributes is not None
    assert span.attributes["py_common.s3.found"] is False


async def test_a_listing_is_one_span_that_is_not_current_between_pages(
    spans: InMemorySpanExporter,
) -> None:
    """A generator that held its span current across a yield would make it the
    active span of whatever the consumer does between pages."""
    fake = _FakeS3()
    fake.pages = [_page(("a", 1, _NOW), ("b", 1, _NOW)), _page(("c", 1, _NOW))]
    storage, _ = _client(fake)
    async with storage:
        active_between_pages = [
            trace.get_current_span().get_span_context().is_valid
            async for _page_of_keys in storage.list_objects()
        ]

    assert active_between_pages == [False, False]
    (span,) = spans.get_finished_spans()
    assert span.name == "s3 ListObjectsV2"
    assert span.attributes is not None
    assert (span.attributes["py_common.s3.pages"], span.attributes["py_common.s3.objects"]) == (
        2,
        3,
    )


async def test_stopping_a_listing_early_is_not_an_error(spans: InMemorySpanExporter) -> None:
    fake = _FakeS3()
    fake.pages = [_page(("a", 1, _NOW)), _page(("b", 1, _NOW))]
    storage, _ = _client(fake)
    async with storage:
        listing = storage.list_objects()
        await anext(listing)
        await listing.aclose()  # the consumer has seen enough

    (span,) = spans.get_finished_spans()
    assert span.status.status_code is StatusCode.UNSET
