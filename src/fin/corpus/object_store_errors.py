"""Typed failures for the raw filing object-store boundary."""

from __future__ import annotations


class ObjectStoreError(RuntimeError):
    """Base class for failures while storing or reading raw bytes."""


class ObjectStoreNotConfiguredError(ObjectStoreError):
    """The object-store endpoint or bucket is not configured."""


class ObjectStoreUnavailableError(ObjectStoreError):
    """A configured object store could not complete an operation."""


class ObjectKeyError(ObjectStoreError):
    """An object key is invalid for the object-store contract."""


class UnknownObjectKeyError(ObjectStoreError):
    """The requested object key does not exist."""


class ObjectSizeLimitError(ObjectStoreError):
    """A raw object exceeds the configured size limit."""


__all__ = [
    "ObjectKeyError",
    "ObjectSizeLimitError",
    "ObjectStoreError",
    "ObjectStoreNotConfiguredError",
    "ObjectStoreUnavailableError",
    "UnknownObjectKeyError",
]
