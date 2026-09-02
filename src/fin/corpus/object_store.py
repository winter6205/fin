"""Raw filing bytes behind a small MinIO/S3-compatible object-store port.

The extracted tree remains the citation source. This module stores only
the original bytes and returns an :class:`ObjectPointer` that can be
persisted as metadata without putting raw filings in Postgres, git, or a
checkpointer.

The in-memory implementation is the default test double. The S3 adapter
accepts the boto3/MinIO-shaped client at the corpus boundary, so tests
can use a fake client and default pytest never needs a live service.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

try:
    import boto3
except ImportError:  # pragma: no cover - exercised only before env sync
    class _MissingBoto3:
        @staticmethod
        def client(*args: Any, **kwargs: Any) -> Any:
            raise ObjectStoreUnavailableError(
                "boto3 is not installed; install the project dependencies "
                "to use a configured S3-compatible object store"
            )

    boto3 = _MissingBoto3()

from fin.corpus.object_store_errors import (
    ObjectKeyError,
    ObjectSizeLimitError,
    ObjectStoreNotConfiguredError,
    ObjectStoreUnavailableError,
    UnknownObjectKeyError,
)


@dataclass(frozen=True)
class ObjectPointer:
    """Stable raw-object metadata suitable for a Postgres pointer row."""

    key: str
    size_bytes: int
    sha256: str
    content_type: str | None = None

    @property
    def size(self) -> int:
        """Compatibility alias for callers that use ``size``."""
        return self.size_bytes


class ObjectStore(Protocol):
    """The narrow raw-byte port used by corpus ingest."""

    def put(
        self,
        key: str,
        payload: bytes,
        *,
        content_type: str | None = None,
    ) -> ObjectPointer: ...

    def get(self, key: str) -> bytes: ...

    def get_pointer(self, key: str) -> ObjectPointer: ...


def _validate_key(key: str) -> None:
    if not isinstance(key, str) or not key.strip() or "\x00" in key:
        raise ObjectKeyError("object key must be a non-empty string")


def _validate_payload(payload: bytes, max_size_bytes: int | None) -> None:
    if not isinstance(payload, bytes):
        raise TypeError("object payload must be bytes")
    if max_size_bytes is not None and len(payload) > max_size_bytes:
        raise ObjectSizeLimitError(
            f"object size {len(payload)} exceeds configured limit "
            f"{max_size_bytes} bytes"
        )


def _pointer(
    key: str, payload: bytes, content_type: str | None
) -> ObjectPointer:
    return ObjectPointer(
        key=key,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        content_type=content_type,
    )


class InMemoryObjectStore:
    """Thread-safe fake object store for tests and local dry runs."""

    def __init__(self, *, max_size_bytes: int | None = None) -> None:
        if max_size_bytes is not None and max_size_bytes < 0:
            raise ValueError("max_size_bytes must be non-negative")
        self._max_size_bytes = max_size_bytes
        self._objects: dict[str, tuple[bytes, ObjectPointer]] = {}
        self._lock = threading.RLock()

    def put(
        self,
        key: str,
        payload: bytes,
        *,
        content_type: str | None = None,
    ) -> ObjectPointer:
        _validate_key(key)
        _validate_payload(payload, self._max_size_bytes)
        pointer = _pointer(key, payload, content_type)
        with self._lock:
            self._objects[key] = (payload, pointer)
        return pointer

    def get(self, key: str) -> bytes:
        _validate_key(key)
        with self._lock:
            item = self._objects.get(key)
            if item is None:
                raise UnknownObjectKeyError(f"unknown object key: {key}")
            return item[0]

    def get_pointer(self, key: str) -> ObjectPointer:
        _validate_key(key)
        with self._lock:
            item = self._objects.get(key)
            if item is None:
                raise UnknownObjectKeyError(f"unknown object key: {key}")
            return item[1]


class S3CompatibleObjectStore:
    """Adapter for a boto3/MinIO-shaped S3-compatible client.

    ``client`` must expose ``put_object`` and ``get_object`` methods with
    the usual ``Bucket``/``Key``/``Body`` keyword arguments. Dependency
    selection and credentials are resolved by :func:`create_object_store`.
    """

    def __init__(
        self,
        *,
        client: Any,
        bucket: str,
        max_size_bytes: int | None = None,
    ) -> None:
        if not isinstance(bucket, str) or not bucket.strip():
            raise ObjectStoreNotConfiguredError(
                "object-store bucket must be a non-empty string"
            )
        if max_size_bytes is not None and max_size_bytes < 0:
            raise ValueError("max_size_bytes must be non-negative")
        self._client = client
        self._bucket = bucket
        self._max_size_bytes = max_size_bytes
        self._pointers: dict[str, ObjectPointer] = {}
        self._lock = threading.RLock()

    def put(
        self,
        key: str,
        payload: bytes,
        *,
        content_type: str | None = None,
    ) -> ObjectPointer:
        _validate_key(key)
        _validate_payload(payload, self._max_size_bytes)
        request: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": payload,
        }
        if content_type is not None:
            request["ContentType"] = content_type
        try:
            self._client.put_object(**request)
        except Exception as exc:
            raise ObjectStoreUnavailableError(
                f"object store put failed for key={key!r}"
            ) from exc
        pointer = _pointer(key, payload, content_type)
        with self._lock:
            self._pointers[key] = pointer
        return pointer

    def get(self, key: str) -> bytes:
        _validate_key(key)
        try:
            response = self._client.get_object(
                Bucket=self._bucket,
                Key=key,
            )
            body = response["Body"]
            payload = body.read() if hasattr(body, "read") else body
        except Exception as exc:
            if _is_missing_object_error(exc):
                raise UnknownObjectKeyError(
                    f"unknown object key: {key}"
                ) from exc
            raise ObjectStoreUnavailableError(
                f"object store get failed for key={key!r}"
            ) from exc
        if not isinstance(payload, bytes):
            raise ObjectStoreUnavailableError(
                f"object store returned a non-bytes body for key={key!r}"
            )
        _validate_payload(payload, self._max_size_bytes)
        return payload

    def get_pointer(self, key: str) -> ObjectPointer:
        _validate_key(key)
        with self._lock:
            known = self._pointers.get(key)
        if known is not None:
            return known
        payload = self.get(key)
        pointer = _pointer(key, payload, None)
        with self._lock:
            self._pointers[key] = pointer
        return pointer


def _is_missing_object_error(exc: Exception) -> bool:
    """Recognize common boto3/MinIO not-found error shapes."""
    if isinstance(exc, KeyError):
        return True
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping):
            code = str(error.get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound", "NoSuchObject"}:
                return True
    return exc.__class__.__name__ in {
        "NoSuchKey",
        "NoSuchObject",
        "NotFound",
    }


def _create_s3_client(
    endpoint: str,
    environ: Mapping[str, str],
) -> Any:
    """Build a boto3 S3 client for MinIO or another S3-compatible service."""
    kwargs: dict[str, str] = {"endpoint_url": endpoint}
    region = environ.get("FIN_OBJECT_STORE_REGION") or environ.get(
        "AWS_DEFAULT_REGION"
    )
    access_key = environ.get("FIN_OBJECT_STORE_ACCESS_KEY") or environ.get(
        "AWS_ACCESS_KEY_ID"
    )
    secret_key = environ.get("FIN_OBJECT_STORE_SECRET_KEY") or environ.get(
        "AWS_SECRET_ACCESS_KEY"
    )
    session_token = environ.get("FIN_OBJECT_STORE_SESSION_TOKEN") or environ.get(
        "AWS_SESSION_TOKEN"
    )
    if region:
        kwargs["region_name"] = region
    if access_key:
        kwargs["aws_access_key_id"] = access_key
    if secret_key:
        kwargs["aws_secret_access_key"] = secret_key
    if session_token:
        kwargs["aws_session_token"] = session_token
    try:
        return boto3.client("s3", **kwargs)
    except Exception as exc:
        raise ObjectStoreUnavailableError(
            f"could not create S3-compatible client for endpoint={endpoint!r}"
        ) from exc


def create_object_store(
    *,
    client: Any | None = None,
    environ: Mapping[str, str] | None = None,
    max_size_bytes: int | None = None,
) -> S3CompatibleObjectStore:
    """Create the configured adapter or raise a typed degraded error.

    Configuration uses ``FIN_OBJECT_STORE_URL`` (or
    ``FIN_OBJECT_STORE_ENDPOINT``) and ``FIN_OBJECT_STORE_BUCKET``.
    ``client`` is injectable so the default test suite stays offline.
    """
    values = os.environ if environ is None else environ
    endpoint = (
        values.get("FIN_OBJECT_STORE_URL")
        or values.get("FIN_OBJECT_STORE_ENDPOINT")
        or ""
    ).strip()
    bucket = (values.get("FIN_OBJECT_STORE_BUCKET") or "").strip()
    if not endpoint or not bucket:
        raise ObjectStoreNotConfiguredError(
            "set FIN_OBJECT_STORE_URL and FIN_OBJECT_STORE_BUCKET "
            "to configure raw object storage"
        )
    if client is None:
        client = _create_s3_client(endpoint, values)
    return S3CompatibleObjectStore(
        client=client,
        bucket=bucket,
        max_size_bytes=max_size_bytes,
    )


S3ObjectStore = S3CompatibleObjectStore
object_store_from_env = create_object_store


__all__ = [
    "InMemoryObjectStore",
    "ObjectPointer",
    "ObjectStore",
    "S3CompatibleObjectStore",
    "S3ObjectStore",
    "create_object_store",
    "object_store_from_env",
]
