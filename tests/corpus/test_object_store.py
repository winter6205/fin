"""Contract tests for raw filing bytes and their durable pointers."""

from __future__ import annotations

import concurrent.futures
import hashlib
from dataclasses import dataclass
from typing import Any

import pytest

from fin.corpus.object_store import (
    InMemoryObjectStore,
    ObjectPointer,
    S3CompatibleObjectStore,
    create_object_store,
)
from fin.corpus.object_store_errors import (
    ObjectKeyError,
    ObjectSizeLimitError,
    ObjectStoreNotConfiguredError,
    ObjectStoreUnavailableError,
    UnknownObjectKeyError,
)


def test_in_memory_store_keeps_raw_bytes_and_pointer() -> None:
    store = InMemoryObjectStore()

    pointer = store.put(
        "filings/TEST-2026/filing.pdf",
        b"raw filing",
        content_type="application/pdf",
    )

    assert isinstance(pointer, ObjectPointer)
    assert pointer.size_bytes == len(b"raw filing")
    assert pointer.sha256 == hashlib.sha256(b"raw filing").hexdigest()
    assert store.get(pointer.key) == b"raw filing"
    assert store.get_pointer(pointer.key) == pointer


def test_store_rejects_unknown_keys_and_overflow_without_byte_loss() -> None:
    store = InMemoryObjectStore(max_size_bytes=3)

    with pytest.raises(ObjectSizeLimitError):
        store.put("filings/too-large.pdf", b"1234")
    with pytest.raises(UnknownObjectKeyError):
        store.get("filings/too-large.pdf")
    with pytest.raises(ObjectKeyError):
        store.put("", b"raw")


def test_in_memory_delete_removes_bytes_and_is_idempotent_on_retry() -> None:
    store = InMemoryObjectStore()
    pointer = store.put("filings/a.pdf", b"raw filing")

    assert store.delete(pointer.key) is None
    with pytest.raises(UnknownObjectKeyError):
        store.get(pointer.key)
    with pytest.raises(UnknownObjectKeyError):
        store.get_pointer(pointer.key)
    # Thread deletion is retried, so repeat deletes must be safe no-ops.
    assert store.delete(pointer.key) is None


def test_in_memory_delete_rejects_invalid_keys() -> None:
    store = InMemoryObjectStore()

    with pytest.raises(ObjectKeyError):
        store.delete("")
    with pytest.raises(ObjectKeyError):
        store.delete("   ")
    with pytest.raises(ObjectKeyError):
        store.delete("bad\x00key")


def test_in_memory_store_is_safe_for_parallel_distinct_writes() -> None:
    store = InMemoryObjectStore()

    def write(index: int) -> tuple[str, ObjectPointer]:
        key = f"filings/{index}.pdf"
        payload = f"raw filing {index}".encode()
        return key, store.put(key, payload)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(write, range(32)))

    for key, pointer in results:
        assert store.get(key) == f"raw filing {key.split('/')[1][:-4]}".encode()
        assert store.get_pointer(key) == pointer


@dataclass
class _FakeBody:
    payload: bytes

    def read(self) -> bytes:
        return self.payload


class _FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> None:
        self.put_calls.append(kwargs)
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        payload = self.objects[(kwargs["Bucket"], kwargs["Key"])]
        return {"Body": _FakeBody(payload)}

    def delete_object(self, **kwargs: Any) -> None:
        self.delete_calls.append(kwargs)
        self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)


def test_s3_compatible_store_maps_put_and_get() -> None:
    client = _FakeS3Client()
    store = S3CompatibleObjectStore(client=client, bucket="corpus-raw")

    pointer = store.put(
        "filings/a.pdf",
        b"raw filing",
        content_type="application/pdf",
    )

    assert client.put_calls == [
        {
            "Bucket": "corpus-raw",
            "Key": "filings/a.pdf",
            "Body": b"raw filing",
            "ContentType": "application/pdf",
        }
    ]
    assert store.get(pointer.key) == b"raw filing"


def test_s3_delete_issues_delete_object_and_drops_pointer() -> None:
    client = _FakeS3Client()
    store = S3CompatibleObjectStore(client=client, bucket="corpus-raw")
    pointer = store.put("filings/a.pdf", b"raw filing")

    assert store.delete("filings/a.pdf") is None

    assert client.delete_calls == [
        {"Bucket": "corpus-raw", "Key": "filings/a.pdf"}
    ]
    with pytest.raises(UnknownObjectKeyError):
        store.get("filings/a.pdf")
    with pytest.raises(UnknownObjectKeyError):
        store.get_pointer(pointer.key)


def test_s3_delete_is_idempotent_for_unknown_key() -> None:
    client = _FakeS3Client()
    store = S3CompatibleObjectStore(client=client, bucket="corpus-raw")

    store.delete("filings/never-written.pdf")
    store.delete("filings/never-written.pdf")

    assert client.delete_calls == [
        {"Bucket": "corpus-raw", "Key": "filings/never-written.pdf"},
        {"Bucket": "corpus-raw", "Key": "filings/never-written.pdf"},
    ]


class _NotFoundClient(_FakeS3Client):
    """Gateways that surface a delete of a missing key as a typed 404."""

    def delete_object(self, **kwargs: Any) -> None:
        self.delete_calls.append(kwargs)
        key = (kwargs["Bucket"], kwargs["Key"])
        if key not in self.objects:
            error = KeyError("NotFound")
            error.response = {"Error": {"Code": "NotFound"}}  # type: ignore[attr-defined]
            raise error
        self.objects.pop(key)


def test_s3_delete_treats_not_found_as_success_and_maps_outage() -> None:
    client = _NotFoundClient()
    store = S3CompatibleObjectStore(client=client, bucket="corpus-raw")

    store.delete("filings/absent.pdf")
    assert len(client.delete_calls) == 1

    store.put("filings/a.pdf", b"raw")

    class BrokenDeleteClient(_FakeS3Client):
        def delete_object(self, **kwargs: Any) -> None:
            raise TimeoutError("store down")

    broken = S3CompatibleObjectStore(
        client=BrokenDeleteClient(),
        bucket="corpus-raw",
    )
    with pytest.raises(ObjectStoreUnavailableError):
        broken.delete("filings/a.pdf")


def test_delete_rejects_invalid_keys_for_both_stores() -> None:
    client = _FakeS3Client()
    stores = [
        InMemoryObjectStore(),
        S3CompatibleObjectStore(client=client, bucket="corpus-raw"),
    ]

    for store in stores:
        with pytest.raises(ObjectKeyError):
            store.delete("")
        with pytest.raises(ObjectKeyError):
            store.delete("   ")

    assert client.delete_calls == []


def test_configured_store_builds_client_from_environment(monkeypatch) -> None:
    client = _FakeS3Client()
    calls: dict[str, Any] = {}

    def fake_client(service_name: str, **kwargs: Any) -> _FakeS3Client:
        calls["service_name"] = service_name
        calls["kwargs"] = kwargs
        return client

    monkeypatch.setattr("fin.corpus.object_store.boto3.client", fake_client)

    store = create_object_store(
        environ={
            "FIN_OBJECT_STORE_ENDPOINT": "http://minio:9000",
            "FIN_OBJECT_STORE_BUCKET": "corpus-raw",
            "FIN_OBJECT_STORE_ACCESS_KEY": "minio",
            "FIN_OBJECT_STORE_SECRET_KEY": "secret",
        }
    )

    assert isinstance(store, S3CompatibleObjectStore)
    assert calls == {
        "service_name": "s3",
        "kwargs": {
            "endpoint_url": "http://minio:9000",
            "aws_access_key_id": "minio",
            "aws_secret_access_key": "secret",
        },
    }


def test_unconfigured_store_and_store_outage_are_typed() -> None:
    with pytest.raises(ObjectStoreNotConfiguredError):
        create_object_store(environ={})

    class DownClient:
        def put_object(self, **kwargs: Any) -> None:
            raise TimeoutError("store down")

        def get_object(self, **kwargs: Any) -> dict[str, Any]:
            raise TimeoutError("store down")

        def delete_object(self, **kwargs: Any) -> None:
            raise TimeoutError("store down")

    store = S3CompatibleObjectStore(client=DownClient(), bucket="corpus")
    with pytest.raises(ObjectStoreUnavailableError):
        store.put("filings/a.pdf", b"raw")
    with pytest.raises(ObjectStoreUnavailableError):
        store.get("filings/a.pdf")
    with pytest.raises(ObjectStoreUnavailableError):
        store.delete("filings/a.pdf")
