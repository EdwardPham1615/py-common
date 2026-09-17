"""ObjectStorageClient against a real S3 server (MinIO).

The unit tests drive a fake S3 client, which proves the wrapper calls the right
methods with the right arguments. It cannot prove the arguments are the ones a
server accepts — addressing style, signature version, and presigned URL
construction are all decided by botocore and only observable against something
that answers like S3.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from botocore.exceptions import ClientError

from py_common.storage import ObjectStorageClient

pytestmark = pytest.mark.integration


@pytest.fixture
async def storage(storage_settings: object) -> AsyncIterator[ObjectStorageClient]:
    client = ObjectStorageClient(storage_settings)  # type: ignore[arg-type]
    await client.open()
    await client.ensure_bucket()
    try:
        yield client
    finally:
        await client.close()


async def test_ensure_bucket_creates_it(storage_settings: object) -> None:
    """head_bucket 404 then create_bucket, against a server that really answers
    404 — the unit test asserts on a ClientError we constructed ourselves."""
    client = ObjectStorageClient(storage_settings)  # type: ignore[arg-type]
    async with client:
        await client.ensure_bucket()
        # Second call must be a no-op rather than an error: startup runs twice
        # when a deployment restarts a pod.
        await client.ensure_bucket()
        await client.client.head_bucket(Bucket=client.settings.bucket)


async def test_round_trip(storage: ObjectStorageClient) -> None:
    await storage.put_object("hello.txt", b"hello world", content_type="text/plain")
    assert await storage.get_object_bytes("hello.txt") == b"hello world"


async def test_metadata_and_content_type_survive(storage: ObjectStorageClient) -> None:
    await storage.put_object(
        "meta.bin", b"x", content_type="application/pdf", metadata={"owner": "alice"}
    )
    head = await storage.client.head_object(Bucket=storage.settings.bucket, Key="meta.bin")

    assert head["ContentType"] == "application/pdf"
    assert head["Metadata"]["owner"] == "alice"


async def test_delete_removes_the_object(storage: ObjectStorageClient) -> None:
    await storage.put_object("gone.txt", b"bye")
    await storage.delete_object("gone.txt")

    with pytest.raises(ClientError):
        await storage.get_object_bytes("gone.txt")


async def test_presigned_get_url_actually_downloads(storage: ObjectStorageClient) -> None:
    """The test a fake cannot do. A presigned URL is a signature over the request
    the server will recompute — wrong region, wrong addressing style or wrong
    signature version all produce a URL that looks fine and returns 403."""
    await storage.put_object("signed.txt", b"signed content", content_type="text/plain")

    url = await storage.get_presigned_url("signed.txt", expires_in=60)
    async with httpx.AsyncClient(timeout=10) as http:
        resp = await http.get(url)

    assert resp.status_code == 200, resp.text
    assert resp.content == b"signed content"


async def test_presigned_put_url_actually_uploads(storage: ObjectStorageClient) -> None:
    """The upload direction has an extra trap: signing with a ContentType the
    caller then does not send is a 403 the client cannot diagnose."""
    url = await storage.get_presigned_url(
        "uploaded.txt", method="put_object", expires_in=60, content_type="text/plain"
    )
    async with httpx.AsyncClient(timeout=10) as http:
        put = await http.put(
            url, content=b"from the client", headers={"Content-Type": "text/plain"}
        )

    assert put.status_code == 200, put.text
    assert await storage.get_object_bytes("uploaded.txt") == b"from the client"


async def test_path_style_addressing_is_what_reaches_the_server(
    storage: ObjectStorageClient,
) -> None:
    """use_path_style is the classic 'works against AWS, fails against MinIO'
    setting: virtual-host style needs DNS wildcards a self-hosted server does not
    have. The bucket must appear in the path, not the hostname."""
    url = await storage.get_presigned_url("anything.txt", expires_in=60)

    assert f"/{storage.settings.bucket}/" in url


async def test_explicit_bucket_overrides_the_configured_one(
    storage: ObjectStorageClient, storage_settings: object
) -> None:
    other = f"{storage.settings.bucket}-other"
    await storage.ensure_bucket(other)

    await storage.put_object("k", b"in-other", bucket=other)

    assert await storage.get_object_bytes("k", bucket=other) == b"in-other"
    with pytest.raises(ClientError):
        await storage.get_object_bytes("k")  # not in the default bucket
