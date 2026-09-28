"""Shared hermetic fakes for attachment host tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fin.host.attachment_store import (
    AttachmentRecord,
    InMemoryAttachmentStore,
    ParsedAttachmentUnit,
)


@dataclass
class _PrepOutcome:
    """The pinned preparation result surface consumed by the graph gate."""

    state: str
    code: str | None = None
    version: int = 1


@dataclass
class _Resolved:
    """The pinned attachment result shape used for text hydration."""

    attachment_id: str
    filename: str
    text: str


class FakeAttachmentService:
    """Script preparation states and resolution results without storage I/O.

    Each preparation request consumes the next scripted outcome; once a
    single outcome remains, it repeats for graph replay. Resolution errors
    are consumed once, then resolution succeeds with the configured text.
    """

    def __init__(
        self,
        script: dict[str, list[Any]],
        *,
        texts: dict[str, str] | None = None,
        filenames: dict[str, str] | None = None,
        resolve_errors: dict[str, list[type[Exception]]] | None = None,
    ) -> None:
        self._script = {attachment_id: list(steps) for attachment_id, steps in script.items()}
        self._texts = texts or {}
        self._filenames = filenames or {}
        self._resolve_errors = {
            attachment_id: list(errors)
            for attachment_id, errors in (resolve_errors or {}).items()
        }
        self.prep_calls: list[tuple[str, str]] = []
        self.resolve_calls: list[tuple[str, str]] = []

    def request_preparation(self, thread_id: str, attachment_id: str) -> _PrepOutcome:
        self.prep_calls.append((thread_id, attachment_id))
        steps = self._script[attachment_id]
        step = steps[0] if len(steps) == 1 else steps.pop(0)
        if isinstance(step, str):
            return _PrepOutcome(state=step)
        state, code = step
        return _PrepOutcome(state=state, code=code)

    def resolve_for_turn(self, *, thread_id: str, attachment_id: str) -> _Resolved:
        self.resolve_calls.append((thread_id, attachment_id))
        errors = self._resolve_errors.get(attachment_id)
        if errors:
            raise errors.pop(0)(attachment_id)
        return _Resolved(
            attachment_id=attachment_id,
            filename=self._filenames.get(attachment_id, "doc.pdf"),
            text=self._texts.get(attachment_id, ""),
        )


class DelegatingStore:
    """Typed read-through base for deliberately corrupted store fakes."""

    def __init__(self, inner: InMemoryAttachmentStore) -> None:
        self._inner = inner

    def get(self, attachment_id: str) -> AttachmentRecord | None:
        return self._inner.get(attachment_id)

    def list_for_thread(self, thread_id: str) -> list[AttachmentRecord]:
        return self._inner.list_for_thread(thread_id)

    def units(
        self, attachment_id: str, version: int
    ) -> tuple[ParsedAttachmentUnit, ...]:
        return self._inner.units(attachment_id, version)

    def index_postings(
        self, attachment_id: str, version: int
    ) -> dict[str, tuple[int, ...]]:
        return self._inner.index_postings(attachment_id, version)
