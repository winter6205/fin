"""Pre-effect tool argument validation and bounded, safe correction.

P3 T2/T3: every model-requested tool call is validated against its canonical
:mod:`fin.host.tools.contracts` model BEFORE the domain operation can run.
A rejected call yields value-free feedback (field path, expected shape,
safe constraint), never submitted values, raw kwargs, exception text, stack
traces, credentials, or source content.

:class:`CorrectionTracker` owns the per-run correction loop: the first
invalid logical call offers at most one correction, a second invalid
response for the SAME logical call is the typed terminal
``tool_validation_exhausted`` outcome. Provider tool-call ids are attempt
identities only; a corrected model attempt is correlated to the logical call
it is correcting.

Three boundaries keep the loop honest:

* a logical call that is terminal (``exhausted`` or ``accepted``) NEVER
  absorbs a later attempt — a new invalid call of the same tool is a NEW
  logical call with its own correction budget;
* a TURN (one operator question) scopes the loop: a new turn resets the
  numbering, the corrections and the states, so a second question on the
  same thread never inherits the first question's exhaustion;
* a tracker can be REBUILT from the run's committed ledger facts, so a
  takeover or an LRU eviction resumes the same one-correction bound.
"""

from __future__ import annotations

import threading
import types
from dataclasses import dataclass
from typing import (
    Any,
    Final,
    Literal,
    Mapping,
    Protocol,
    Sequence,
    Union,
    get_args,
    get_origin,
)

from pydantic import BaseModel, ValidationError

from fin.host.tools.contracts import (
    ToolContract,
    UnknownToolContract,
    contract_for,
)

ValidationOutcomeCode = Literal[
    "valid_input_accepted",
    "correction_offered",
    "tool_validation_exhausted",
]

#: Errors keep the declared field path (a submitted key may appear as an
#: extra-field path); every hint below is schema text, never a submitted value.
_TYPE_HINTS: dict[str, str] = {
    "missing": "is required",
    "extra_forbidden": "is not an accepted argument",
    "string_type": "must be a string",
    "string_too_short": "is shorter than the declared minimum length",
    "string_too_long": "is longer than the declared maximum length",
    "int_type": "must be an integer",
    "int_parsing": "must be an integer",
    "float_type": "must be a number",
    "float_parsing": "must be a number",
    "bool_type": "must be a boolean",
    "list_type": "must be a list",
    "dict_type": "must be a JSON object",
    "model_type": "must match the declared object shape",
    "literal_error": "must be the declared literal value",
    "enum": "must be one of the declared values",
    "greater_than": "must be above the declared lower bound",
    "greater_than_equal": "must be at least the declared lower bound",
    "less_than": "must be below the declared upper bound",
    "less_than_equal": "must be at most the declared upper bound",
    "union_tag_invalid": "must match one of the declared shapes",
    "value_error": "is outside the declared contract",
}
_GENERIC_FIELD_HINT = "is not accepted by the declared contract"
_GENERIC_MODEL_HINT = "are outside the declared contract"
_MAX_FEEDBACK_FIELDS = 5


@dataclass(frozen=True)
class ValidationResult:
    """Either normalized arguments or a safe correction message."""

    ok: bool
    tool_name: str
    args: dict[str, Any] | None = None
    feedback: str | None = None
    field_paths: tuple[str, ...] = ()


def validate_tool_arguments(
    name: str, args: Mapping[str, Any]
) -> ValidationResult:
    """Validate ``args`` for ``name`` against the one canonical contract.

    Returns the normalized argument mapping on success, or a safe feedback
    string that names a field path and the expected shape/constraint. Raises
    :class:`~fin.host.tools.contracts.UnknownToolContract` for an unregistered
    tool — an unknown name is a wiring fault, not model feedback.
    """
    # EXIT: an unregistered tool is a WIRING fault, not model input: the
    # call raises instead of answering the model with a correction for a
    # tool the host never declared.
    contract = contract_for(name)
    return _validate_contract(contract, args)


def _validate_contract(
    contract: ToolContract, args: Any
) -> ValidationResult:
    if not isinstance(args, Mapping):
        # EXIT: the arguments did not arrive as an object at all, so there
        # is no field path to name; the model is told the shape it must
        # submit, never what it submitted.
        return ValidationResult(
            ok=False,
            tool_name=contract.name,
            feedback=(
                f"invalid arguments for tool '{contract.name}': arguments "
                "must be a JSON object"
            ),
        )
    try:
        validated = contract.args_model.model_validate(dict(args))
    except ValidationError as exc:
        feedback, paths = _safe_feedback(contract, exc)
        return ValidationResult(
            ok=False,
            tool_name=contract.name,
            feedback=feedback,
            field_paths=paths,
        )
    return ValidationResult(
        ok=True,
        tool_name=contract.name,
        args=validated.model_dump(mode="python"),
    )


def _safe_feedback(
    contract: ToolContract, exc: ValidationError
) -> tuple[str, tuple[str, ...]]:
    """Build value-free feedback from a validation error.

    Pydantic's ``errors()`` carries the submitted input in ``input``/``ctx``;
    only the error *type* is read here, mapped to a fixed hint (or the
    contract's own field note). No message text, ``repr``, or object is
    stringified.
    """
    lines: list[str] = []
    paths: list[str] = []
    seen: set[str] = set()
    for error in exc.errors():
        loc = error.get("loc", ())
        path = _field_path(contract.args_model, loc)
        if not path and error.get("type") == "extra_forbidden":
            # An extra key is not a declared field, but naming it is exactly
            # the field-path feedback the model needs.
            path = next(
                (part for part in loc if isinstance(part, str)), ""
            )
        if path:
            if path in seen:
                continue
            seen.add(path)
        note = contract.field_notes.get(path)
        hint = note() if callable(note) else note
        if hint is None:
            # EXIT: the contract declares no note for this field and the
            # error type is not one the table knows, so the generic hint
            # names the declared contract without naming the value.
            hint = (
                _TYPE_HINTS.get(str(error.get("type")), _GENERIC_FIELD_HINT)
                if path
                else _GENERIC_MODEL_HINT
            )
        lines.append(f"'{path}' {hint}" if path else hint)
        if path:
            paths.append(path)
        if len(lines) >= _MAX_FEEDBACK_FIELDS:
            break
    if not lines:
        # EXIT: the error carried no locatable field at all, so the model
        # is told the object is outside the contract and nothing more.
        lines.append(_GENERIC_MODEL_HINT)
    return (
        f"invalid arguments for tool '{contract.name}': " + "; ".join(lines),
        tuple(paths),
    )


def _field_path(model: type[BaseModel], loc: Sequence[Any]) -> str:
    """The declared field path, stopping before union-branch labels/indices."""
    parts: list[str] = []
    current: type[BaseModel] | None = model
    for part in loc:
        if current is None or not isinstance(part, str):
            break
        field = current.model_fields.get(part)
        if field is None:
            # A union branch label (e.g. ``CompanySourceRef``) or an index.
            break
        parts.append(part)
        current = _nested_model(field.annotation)
    return ".".join(parts)


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        members = [
            arg
            for arg in get_args(annotation)
            if isinstance(arg, type) and issubclass(arg, BaseModel)
        ]
        # ``CompanySourceRef | AttachmentSourceRef`` has no single nested
        # model; stop at the union field and let its field note explain it.
        return members[0] if len(members) == 1 else None
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


@dataclass(frozen=True)
class ToolValidationOutcome:
    """One observed validation outcome for a logical tool call.

    ``schema_rejected`` and ``correction_offered`` are separate facts so the
    ledger (T4) can record both for the first invalid attempt.
    ``model_attempt`` is the tracker's generation — the model-emitted
    attempt this validation looked at — and ``attempt_id`` is the
    provider tool-call id, an attempt identity and NOT a logical one.
    """

    thread_id: str
    run_id: str
    tool_name: str
    logical_call_id: str
    attempt_id: str | None
    model_attempt: int
    outcome: ValidationOutcomeCode
    accepted: bool
    schema_rejected: bool
    correction_offered: bool
    feedback: str | None
    field_paths: tuple[str, ...]
    normalized_args: dict[str, Any] | None


@dataclass
class _LogicalCall:
    logical_call_id: str
    tool_name: str
    corrections: int = 0
    state: Literal["open", "accepted", "exhausted"] = "open"
    #: Generation in which the correction was offered. Recorded so a
    #: rebuilt tracker can tell a spent correction from an untouched one.
    correction_generation: int | None = None
    #: Generation in which this call already took a correlated attempt. One
    #: model turn consumes at most one correction slot of a logical call.
    correlated_generation: int | None = None
    #: How many times this logical call was dispatched. The execution
    #: attempt number is this counter, never the model attempt: one model
    #: attempt can carry several execution attempts of one logical call.
    execution_attempts: int = 0


class LedgerFactView(Protocol):
    """The committed ledger facts a tracker is rebuilt from.

    The ledger's ``ToolOutcomeFact`` satisfies this shape; the tracker
    reads only the identity, the kind and the model attempt of a fact.
    """

    @property
    def logical_call_id(self) -> str: ...

    @property
    def tool_name(self) -> str: ...

    @property
    def provider_tool_call_id(self) -> str | None: ...

    @property
    def kind(self) -> str: ...

    @property
    def model_attempt(self) -> int: ...

    @property
    def execution_attempt(self) -> int | None: ...


#: The fact kinds that mean the logical call's arguments were accepted and
#: the call is no longer an open correction loop.
_ACCEPTED_FACT_KINDS: Final[frozenset[str]] = frozenset(
    {
        "valid_input_accepted",
        "execution_started",
        "succeeded",
        "known_failed",
        "outcome_unknown",
        "reconciled",
    }
)


class CorrectionTracker:
    """Per-run bounded correction and logical-call correlation.

    Thread-safe. ``begin_model_attempt`` binds every provider tool-call id of
    one model turn to a logical call; ``record_validation`` records the
    outcome. A provider id is an attempt identity, NOT a logical identity, so
    a corrected attempt with a fresh id maps back to the same logical call.

    The tracker is scoped to ONE TURN (one operator question). A new turn id
    resets the correction budget: the previous question's calls are over.
    The logical-call NUMBERING keeps counting across turns, because the
    ledger and the metrics key their facts by that id.
    """

    def __init__(
        self,
        thread_id: str,
        run_id: str,
        *,
        max_corrections: int = 1,
    ) -> None:
        self.thread_id = thread_id
        self.run_id = run_id
        self.max_corrections = max_corrections
        self._lock = threading.Lock()
        self._generation = 0
        self._ordinal = 0
        self._calls: list[_LogicalCall] = []
        self._by_attempt: dict[str, str] = {}
        self._turn_id: str | None = None

    def begin_model_attempt(
        self,
        tool_calls: Sequence[Any],
        *,
        turn_id: str | None = None,
    ) -> dict[str, str]:
        """Bind this model turn's tool calls to logical calls.

        Returns ``{provider_tool_call_id: logical_call_id}``. A call for a
        tool whose earlier logical call offered a correction and is still
        OPEN is the SAME logical call; every other call starts a new one.

        ``turn_id`` scopes the loop to one operator question: a turn id
        that differs from the stored one resets the logical-call
        numbering, the corrections and the states, so a new question starts
        with a fresh correction budget.
        """
        with self._lock:
            self._reset_turn(turn_id)
            self._generation += 1
            bindings: dict[str, str] = {}
            for raw in tool_calls:
                name, attempt_id = _tool_call_identity(raw)
                if name is None or attempt_id is None:
                    continue
                known = self._by_attempt.get(attempt_id)
                if known is not None:
                    bindings[attempt_id] = known
                    continue
                call = self._correlate(name)
                if call is None:
                    call = self._new_call(name)
                else:
                    call.correlated_generation = self._generation
                self._by_attempt[attempt_id] = call.logical_call_id
                bindings[attempt_id] = call.logical_call_id
            return bindings

    def logical_call_for(self, tool_call_id: str) -> str | None:
        """The logical call this attempt id belongs to, if known."""
        with self._lock:
            return self._by_attempt.get(tool_call_id)

    def next_execution_attempt(self, logical_call_id: str) -> int:
        """Allocate the next execution attempt of one logical call.

        The execution attempt counts the DISPATCHES of a logical call
        (1, 2, ...), not the model attempts that produced them: one model
        attempt may carry several execution attempts, and one execution
        attempt belongs to exactly one logical call.
        """
        with self._lock:
            call = self._calls_by_id().get(logical_call_id)
            if call is None:
                # EXIT: an unknown logical call has no counter of its own;
                # the first dispatch of it is its first execution attempt.
                return 1
            call.execution_attempts += 1
            return call.execution_attempts

    def hydrate(self, facts: Sequence[LedgerFactView]) -> None:
        """Rebuild this tracker's state from committed ledger facts.

        A takeover (or an LRU eviction) starts a tracker with no memory of
        the run's correction loop. The ledger's facts carry the logical
        call id, the provider attempt id, the kind and the model attempt,
        so the loop is reconstructed exactly: which provider attempt
        belongs to which logical call, how many corrections that call
        already spent, and whether it is still open. Each logical call's
        ``execution_attempts`` counter is restored to its highest committed
        ``execution_attempt`` so the next dispatch continues past the
        attempts a previous owner already committed.
        """
        with self._lock:
            for fact in facts:
                call = self._calls_by_id().get(fact.logical_call_id)
                if call is None:
                    call = self._restored_call(
                        fact.logical_call_id, fact.tool_name
                    )
                provider_id = fact.provider_tool_call_id
                if provider_id:
                    self._by_attempt[provider_id] = fact.logical_call_id
                execution_attempt = fact.execution_attempt
                if execution_attempt is not None:
                    call.execution_attempts = max(
                        call.execution_attempts, execution_attempt
                    )
                kind = fact.kind
                if kind == "correction_offered":
                    # One committed correction_offered fact per offered
                    # correction: the count is the spent budget.
                    call.corrections += 1
                    call.correction_generation = fact.model_attempt
                elif kind == "correction_exhausted":
                    call.corrections = max(call.corrections, 1)
                    call.state = "exhausted"
                elif kind == "schema_rejected":
                    # EXIT: the rejection alone says nothing about the
                    # budget — the correction_offered / correction_exhausted
                    # fact that follows it carries that state.
                    continue
                elif kind in _ACCEPTED_FACT_KINDS:
                    call.state = "accepted"

    def record_validation(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> ToolValidationOutcome:
        """Validate one attempt and record its correction-loop outcome."""
        result = validate_tool_arguments(tool_name, args)
        with self._lock:
            call = self._call_for(tool_name, tool_call_id)
            if result.ok:
                call.state = "accepted"
                return self._outcome(
                    call,
                    tool_call_id,
                    "valid_input_accepted",
                    accepted=True,
                    schema_rejected=False,
                    correction_offered=False,
                    feedback=None,
                    field_paths=(),
                    normalized_args=result.args,
                )
            if (
                call.state == "open"
                and call.corrections < self.max_corrections
            ):
                call.corrections += 1
                call.correction_generation = self._generation
                return self._outcome(
                    call,
                    tool_call_id,
                    "correction_offered",
                    accepted=False,
                    schema_rejected=True,
                    correction_offered=True,
                    feedback=result.feedback,
                    field_paths=result.field_paths,
                    normalized_args=None,
                )
            call.state = "exhausted"
            return self._outcome(
                call,
                tool_call_id,
                "tool_validation_exhausted",
                accepted=False,
                schema_rejected=True,
                correction_offered=False,
                feedback=None,
                field_paths=result.field_paths,
                normalized_args=None,
            )

    # ----- internals -----

    def _call_for(
        self, tool_name: str, tool_call_id: str | None
    ) -> _LogicalCall:
        if tool_call_id is not None:
            logical_id = self._by_attempt.get(tool_call_id)
            if logical_id is not None:
                return self._calls_by_id()[logical_id]
        call = self._correlate(tool_name)
        if call is None and tool_call_id is None:
            # Convenience for callers that validate an unregistered attempt:
            # reuse the most recent open call for this tool.
            for candidate in reversed(self._calls):
                if candidate.tool_name == tool_name and candidate.state == "open":
                    return candidate
        if call is None:
            call = self._new_call(tool_name)
        if tool_call_id is not None:
            self._by_attempt[tool_call_id] = call.logical_call_id
        return call

    def _calls_by_id(self) -> dict[str, _LogicalCall]:
        return {call.logical_call_id: call for call in self._calls}

    def _reset_turn(self, turn_id: str | None) -> None:
        """Start the correction budget of a new turn.

        A turn is one operator question. The first turn this tracker sees
        is ADOPTED, not applied: a tracker rebuilt from the run's
        committed facts already holds this turn's state. A later, DIFFERENT
        turn id means a new question, so the previous question's calls,
        corrections and states are over.

        The ORDINAL keeps counting instead of restarting: the ledger and
        the metrics key facts and execution attempts by logical call id,
        so two turns must never mint the same id.
        """
        if turn_id is None:
            return
        if self._turn_id is None:
            self._turn_id = turn_id
            return
        if turn_id == self._turn_id:
            return
        self._turn_id = turn_id
        self._calls = []
        self._by_attempt = {}

    def _correlate(self, tool_name: str) -> _LogicalCall | None:
        """The open logical call this attempt is correcting, if any.

        A candidate is a call that is still OPEN, that already spent a
        correction, and that did not take a correlated attempt in this
        model attempt. A terminal call (exhausted or accepted) NEVER
        absorbs a later attempt: a new invalid call of the same tool is a
        NEW logical call with its own bounded correction.
        """
        for call in reversed(self._calls):
            if call.tool_name != tool_name:
                continue
            if call.state != "open":
                continue
            if call.corrections <= 0:
                continue
            if call.correlated_generation == self._generation:
                # One model turn consumes at most one correction slot of a
                # logical call; a sibling call in the same turn is its own.
                continue
            return call
        return None

    def _new_call(self, tool_name: str) -> _LogicalCall:
        self._ordinal += 1
        call = _LogicalCall(
            logical_call_id=f"{tool_name}#{self._ordinal}",
            tool_name=tool_name,
        )
        self._calls.append(call)
        return call

    def _restored_call(
        self, logical_call_id: str, tool_name: str
    ) -> _LogicalCall:
        """A logical call rebuilt from a committed fact.

        The id is the one the ledger already carries, so the rebuilt
        tracker and the ledger keep naming the same call. The ordinal
        counter follows the highest number already in use, so a restored
        call and a fresh one never share an id.
        """
        ordinal = _ordinal_of(logical_call_id)
        if ordinal is not None:
            self._ordinal = max(self._ordinal, ordinal)
        call = _LogicalCall(
            logical_call_id=logical_call_id,
            tool_name=tool_name,
        )
        self._calls.append(call)
        return call

    def _outcome(
        self,
        call: _LogicalCall,
        attempt_id: str | None,
        outcome: ValidationOutcomeCode,
        *,
        accepted: bool,
        schema_rejected: bool,
        correction_offered: bool,
        feedback: str | None,
        field_paths: tuple[str, ...],
        normalized_args: dict[str, Any] | None,
    ) -> ToolValidationOutcome:
        return ToolValidationOutcome(
            thread_id=self.thread_id,
            run_id=self.run_id,
            tool_name=call.tool_name,
            logical_call_id=call.logical_call_id,
            attempt_id=attempt_id,
            model_attempt=self._generation,
            outcome=outcome,
            accepted=accepted,
            schema_rejected=schema_rejected,
            correction_offered=correction_offered,
            feedback=feedback,
            field_paths=field_paths,
            normalized_args=normalized_args,
        )


def _tool_call_identity(raw: Any) -> tuple[str | None, str | None]:
    """``(name, provider_tool_call_id)`` from a ToolCall-like object."""
    if isinstance(raw, Mapping):
        name = raw.get("name")
        attempt_id = raw.get("id") or raw.get("tool_call_id")
    else:
        name = getattr(raw, "name", None)
        attempt_id = getattr(raw, "id", None)
    return (
        str(name) if isinstance(name, str) and name else None,
        str(attempt_id) if isinstance(attempt_id, str) and attempt_id else None,
    )


def _ordinal_of(logical_call_id: str) -> int | None:
    """The numbering a logical call id carries, ``None`` when it has none."""
    _, _, tail = logical_call_id.rpartition("#")
    if not tail.isdigit():
        return None
    return int(tail)


__all__ = [
    "CorrectionTracker",
    "LedgerFactView",
    "ToolValidationOutcome",
    "UnknownToolContract",
    "ValidationOutcomeCode",
    "ValidationResult",
    "validate_tool_arguments",
]
